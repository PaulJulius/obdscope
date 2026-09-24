"""Command-line interface: ``obdscope <command>``."""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import time
from contextlib import asynccontextmanager

from . import reports
from .dtc import decode_dtcs, describe
from .elm327 import Elm327, ElmError, NoData
from .obd import NegativeResponse, Vehicle, ecu_name, primary
from .pids import DEFAULT_LIVE, PIDS, format_value, parse_pid
from .simulator import PROFILES
from .transport import TransportError, discover, make_transport

FORD_PWM_PCM_HEADER = "C410F1"  # priority C4, target PCM (10), tester (F1)
CAN_ENGINE_HEADER = "7E0"  # physical request ID of the engine ECU (answers on 7E8)
VEHICLE_HINT = (
    "The adapter is connected but the vehicle's computer isn't answering. Turn the ignition to ON\n"
    "(dash lights up; the engine doesn't need to run). Adapter-only commands still work: `obdscope raw ATRV`."
)


def status(message: str) -> None:
    print(message, file=sys.stderr)


@asynccontextmanager
async def session(args, require_vehicle: bool = True):
    transport = make_transport(args.simulate, address=args.address, name=args.name)
    status(f"Connecting to {'simulated ' + PROFILES[args.simulate].label if args.simulate else 'adapter'}...")
    await transport.connect()
    try:
        status(f"Connected to {transport.device.name or 'adapter'} ({transport.device.address}). Initializing...")
        elm = Elm327(transport, protocol=args.protocol)
        await elm.initialize(connect_vehicle=False)
        try:
            await elm.connect_vehicle()
            status(f"{elm.version}, protocol {elm.protocol}: {elm.protocol_name}\n")
        except ElmError as e:
            if require_vehicle:
                raise ElmError(f"{e}\n{VEHICLE_HINT}") from None
            status(f"{elm.version}, but the vehicle is not responding ({e}).\n{VEHICLE_HINT}\n")
        yield Vehicle(elm)
    finally:
        await transport.close()


# --- Commands ---

async def cmd_scan(args) -> None:
    if args.simulate:
        status("Simulated vehicles don't need scanning; run any other command with --simulate.")
        return
    status(f"Scanning for {args.timeout:.0f}s...")
    for d in await discover(args.timeout):
        marker = "*" if d.likely_obd else " "
        print(f"{marker} {d.rssi:>4} dBm  {d.device.address}  {d.name or '(no name)'}")
    status("\n* = looks like an OBD adapter")


async def cmd_info(args) -> None:
    async with session(args) as v:
        info = await reports.vehicle_info(v)
    print(f"Adapter:     {info['adapter']}")
    print(f"Battery:     {info['voltage']}")
    print(f"Protocol:    {info['protocol']} - {info['protocol_name']}")
    print(f"VIN:         {info['vin'] or 'not reported (mode 09 is uncommon before MY2005)'}")
    if info["obd_standard"]:
        print(f"OBD type:    {info['obd_standard']}")
    for ecu in info["ecus"]:
        print(f"\nECU {ecu['ecu']}:  MIL {'ON' if ecu['mil_on'] else 'off'}, {ecu['dtc_count']} stored code(s)")
        print("Readiness monitors:")
        for m in ecu["monitors"]:
            print(f"  {m['name']:<28} {'complete' if m['complete'] else 'NOT complete'}")
    print("\nSupported mode 01 PIDs: " + " ".join(f"{p:02X}" for p in info["supported"]))


async def cmd_pids(args) -> None:
    async with session(args) as v:
        rows = await reports.all_readings(v, args.imperial)
    for row in rows:
        source = f"  [{row['ecu']}]" if row["ecu"] else ""
        print(f"{row['pid']:02X}  {row['name']:<38} {row['display']}{source}")


async def cmd_live(args) -> None:
    requested = [parse_pid(p) for p in (args.pids or DEFAULT_LIVE)]
    async with session(args) as v:
        supported = await v.supported_pids()
        pids = [p for p in requested if p in supported and p in PIDS]
        skipped = [f"{p:02X}" for p in requested if p not in pids]
        if skipped:
            status(f"Skipping unsupported/undecodable PIDs: {' '.join(skipped)}")
        if not pids:
            raise ElmError("none of the requested PIDs are supported by this vehicle")

        names = [PIDS[p].name for p in pids]
        writer = None
        if args.csv:
            csv_file = open(args.csv, "w", newline="")
            writer = csv.writer(csv_file)
            writer.writerow(["timestamp"] + [f"{PIDS[p].name} ({PIDS[p].unit})" for p in pids])
        status("Legend: " + ", ".join(f"[{i + 1}] {n}" for i, n in enumerate(names)) + "\nCtrl-C to stop.\n")
        try:
            sample = 0
            while args.count is None or sample < args.count:
                started = time.monotonic()
                raw_values, shown = [], []
                for p in pids:
                    try:
                        value = PIDS[p].decode(primary(await v.pid(p)))
                    except (NoData, NegativeResponse):
                        value = ""
                    raw_values.append(value)
                    shown.append(format_value(value, PIDS[p].unit, args.imperial) if value != "" else "-")
                print(time.strftime("%H:%M:%S") + "  " + "  ".join(f"[{i + 1}] {s}" for i, s in enumerate(shown)))
                if writer:
                    writer.writerow([time.time()] + raw_values)
                sample += 1
                await asyncio.sleep(max(0.0, args.interval - (time.monotonic() - started)))
        finally:
            if writer:
                csv_file.close()


BANKS = {1: (0x06, 0x07), 2: (0x08, 0x09)}  # bank -> (short term PID, long term PID)
BAR_WIDTH = 25   # odd, so there is a centre column
BAR_SCALE = 25.0  # % correction at the bar's edge


def _bar(delta: float) -> str:
    half = BAR_WIDTH // 2
    cells = ["·"] * BAR_WIDTH
    cells[half] = "|"
    cells[half + max(-half, min(half, round(delta / BAR_SCALE * half)))] = "#"
    return "".join(cells)


async def cmd_trims(args) -> None:
    """Live fuel trims for leak hunting: block a leak and the demand for fuel drops."""
    async with session(args) as v:
        supported = await v.supported_pids()
        banks = {b: pids for b, pids in BANKS.items() if pids[0] in supported and pids[1] in supported}
        if not banks:
            raise ElmError("this vehicle doesn't report fuel trims")
        context = [p for p in (0x0C, 0x10, 0x05) if p in supported]

        async def read(pid):
            try:
                return PIDS[pid].decode(primary(await v.pid(pid)))
            except (NoData, NegativeResponse):
                return None

        status(
            f"Fuel trim monitor. Watching {len(banks)} bank(s); a reading of 0% means the engine needs no correction.\n"
            f"The first {args.baseline} samples set the baseline, then block a suspected leak and watch for a drop.\n"
            "Ctrl-C to stop.\n"
        )
        history, baseline, lines_drawn = {b: [] for b in banks}, {}, 0
        sample = 0
        while args.count is None or sample < args.count:
            started = time.monotonic()
            totals = {}
            for bank, (stft_pid, ltft_pid) in banks.items():
                stft, ltft = await read(stft_pid), await read(ltft_pid)
                totals[bank] = None if stft is None or ltft is None else (stft, ltft, stft + ltft)
                if totals[bank] and len(history[bank]) < args.baseline:
                    history[bank].append(totals[bank][2])
            readings = {pid: await read(pid) for pid in context}

            if not baseline and all(len(h) >= args.baseline for h in history.values()):
                baseline = {b: sum(h) / len(h) for b, h in history.items()}

            out = []
            if baseline:
                out.append(f"  {'':<8}{'short':>8}{'long':>8}{'total':>9}{'change':>9}   leaner {'':<10} richer")
            else:
                out.append(f"  {'':<8}{'short':>8}{'long':>8}{'total':>9}   measuring baseline ({max(len(h) for h in history.values())}/{args.baseline})")
            drop = 0.0
            for bank, values in totals.items():
                if not values:
                    out.append(f"  bank {bank}   (no data)")
                    continue
                stft, ltft, total = values
                if baseline:
                    delta = total - baseline[bank]
                    drop = min(drop, delta)
                    out.append(f"  bank {bank}  {stft:+7.1f}%{ltft:+7.1f}%{total:+8.1f}%{delta:+8.1f}%   {_bar(delta)}")
                else:
                    out.append(f"  bank {bank}  {stft:+7.1f}%{ltft:+7.1f}%{total:+8.1f}%")
            out.append("  " + "   ".join(
                f"{PIDS[pid].name.split(',')[0].lower()} {format_value(readings[pid], PIDS[pid].unit, args.imperial)}"
                for pid in context if readings.get(pid) is not None
            ))
            if not baseline:
                out.append("  hold steady at idle...")
            elif drop <= -args.threshold:
                out.append(f"  >>> LEANER by {-drop:.1f}% -- whatever you just blocked is (part of) the leak")
            elif drop <= -args.threshold / 2:
                out.append(f"  ... slight drop ({-drop:.1f}%), keep it blocked a moment")
            else:
                out.append("  no change yet -- block the next hose")

            if lines_drawn:
                print(f"\033[{lines_drawn}F", end="")  # redraw in place
            print("\n".join(f"{line:<78}" for line in out))
            lines_drawn = len(out)
            sample += 1
            await asyncio.sleep(max(0.0, args.interval - (time.monotonic() - started)))


async def cmd_dtc(args) -> None:
    async with session(args) as v:
        report = await reports.trouble_codes(v, args.imperial)
    for label, key in (("Stored", "stored"), ("Pending", "pending"), ("Permanent", "permanent")):
        print(f"{label} codes:")
        codes = report[key]
        if codes is None:
            print("  not supported (permanent codes exist on MY2010+ vehicles)")
        elif not codes:
            print("  none")
        for c in codes or []:
            print(f"  {c['code']}  {c['description'] or '(no description)'}  [ECU {c['ecu']}]")
    if ff := report["freeze_frame"]:
        print(f"\nFreeze frame (captured when {ff['trigger']} set):")
        for r in ff["readings"]:
            print(f"  {r['name']:<38} {r['display']}")


async def cmd_clear(args) -> None:
    if not args.yes:
        print(
            "This clears stored codes and freeze-frame data and RESETS all readiness monitors.\n"
            "The vehicle will fail an emissions inspection until the monitors complete again\n"
            "(usually a few days of mixed driving). Ignition should be ON, engine OFF."
        )
        if input("Type 'clear' to continue: ").strip().lower() != "clear":
            print("Cancelled.")
            return
    async with session(args) as v:
        await v.clear_dtcs()
        print("Codes cleared.")


async def cmd_raw(args) -> None:
    # AT commands talk to the adapter alone, so don't insist on reaching the vehicle.
    async with session(args, require_vehicle=False) as v:
        async def run(command: str) -> None:
            reply = await v.elm.transport.send(command.strip().upper(), timeout=args.timeout)
            print("\n".join(line for line in reply.replace("\r", "\n").split("\n") if line.strip()))

        if args.commands:
            for command in args.commands:
                print(f"> {command}")
                await run(command)
            return
        status("Raw ELM327 console. Type AT or OBD commands (e.g. ATRV, 010C, 03). Empty line or Ctrl-D quits.")
        while True:
            try:
                command = await asyncio.to_thread(input, "> ")
            except EOFError:
                break
            if not command.strip():
                break
            try:
                await run(command)
            except TransportError as e:
                print(f"error: {e}")


async def cmd_modules(args) -> None:
    """Ask every module on the bus for its trouble codes.

    Standard mode 03 only covers emissions. Manufacturers use their own service
    for other modules -- mode 13 on older Fords -- physically addressed to each
    one in turn. Reads only.
    """
    async with session(args) as v:
        is_can = v.elm.is_can
        request = args.request or ("03" if is_can else "13")
        expected = int(request[:2], 16) + 0x40
        addresses = range(0x7E0, 0x7E8) if is_can else range(0x00, 0x100)
        status(f"Asking {len(addresses)} addresses for codes with mode {request} "
               f"(about {max(1, round(len(addresses) * 0.4 / 60))} min)...")

        found = 0
        for index, address in enumerate(addresses):
            if not is_can and index and index % 64 == 0:
                status(f"  ...{index}/{len(addresses)}")
            header = f"{address:03X}" if is_can else f"C4{address:02X}F1"
            await v.elm.command(f"ATSH{header}")
            try:
                messages = await v.elm.request(request, timeout=2.0, attempts=1)
            except (NoData, ElmError):
                continue
            for ecu, msgs in messages.items():
                for message in msgs:
                    if not message or message[0] != expected:
                        continue
                    payload = message[2:] if is_can else message[1:]
                    codes = decode_dtcs(payload)
                    found += 1
                    print(f"\nmodule {address:02X} (answers as {ecu})   {payload.hex(' ').upper()}")
                    for code in codes:
                        print(f"  {code}  {describe(code) or '(look this one up: not a generic code)'}")
                    if not codes:
                        print("  no codes stored")
        status(f"\n{found} module(s) answered.")
        if not is_can:
            status("Codes from non-powertrain modules are manufacturer-specific; the letter\n"
                   "(B = body, C = chassis, U = network) tells you which system.")


async def cmd_sniff(args) -> None:
    """Listen to the bus and report which modules are talking.

    Standard OBD-II only reaches the powertrain. Other modules (ABS, restraints,
    body) share the bus on many vehicles, and monitoring shows their addresses
    without transmitting anything.
    """
    async with session(args, require_vehicle=False) as v:
        await v.elm.command("ATH1")
        status(f"Listening for {args.seconds:.0f}s (nothing is transmitted)...")
        lines = await v.elm.monitor(args.seconds)
        if not lines:
            status("No traffic seen. Some modules only talk when something asks them to.")
            return

        senders: dict[str, dict] = {}
        for line in lines:
            tokens = line.split()
            if len(tokens) < 4 or not all(len(t) == 2 and _is_hex_byte(t) for t in tokens):
                continue
            header, payload = tokens[:3], tokens[3:-1]
            key = " ".join(header)
            entry = senders.setdefault(key, {"count": 0, "modes": set(), "sample": " ".join(payload)})
            entry["count"] += 1
            if payload:
                entry["modes"].add(payload[0])
        print(f"{len(lines)} frames from {len(senders)} address(es)\n")
        print(f"{'header':<10} {'count':>6}  {'from':<6} {'to':<6} {'first bytes':<24} services")
        for key, entry in sorted(senders.items(), key=lambda kv: -kv[1]["count"]):
            priority, target, source = key.split()
            print(f"{key:<10} {entry['count']:>6}  {source:<6} {target:<6} {entry['sample'][:24]:<24} "
                  f"{' '.join(sorted(entry['modes'])[:6])}")
        print("\n'from' is the module that sent the frame. To talk to one directly, set the header\n"
              "to its address, e.g. `obdscope raw ATSHC4<from>F1 <request>`.")


def _is_hex_byte(token: str) -> bool:
    return all(c in "0123456789ABCDEFabcdef" for c in token)


async def cmd_probe(args) -> None:
    start, end = int(args.start, 16), int(args.end, 16)
    async with session(args) as v:
        header = args.header
        if header == "auto":
            header = {"1": FORD_PWM_PCM_HEADER, "6": CAN_ENGINE_HEADER, "8": CAN_ENGINE_HEADER}.get(v.elm.protocol)
        if header:
            await v.elm.command(f"ATSH{header}")
            status(f"Using header {header}")
        status(f"Probing mode 22 DIDs {start:04X}-{end:04X}...")
        found = 0
        for did in range(start, end + 1):
            try:
                for ecu, data in (await v.enhanced(did)).items():
                    print(f"{did:04X}  ECU {ecu_name(ecu)}  {data.hex(' ').upper()}")
                    found += 1
            except NoData:
                pass
            except NegativeResponse as e:
                if e.code == 0x11:
                    raise ElmError(f"{e}; this ECU doesn't accept mode 22 with this header") from None
                if e.code != 0x31:  # "out of range" just means unsupported
                    print(f"{did:04X}  {e}")
        status(f"\n{found} DID(s) responded.")


async def cmd_ui(args) -> None:
    from .web import serve  # aiohttp is only needed for the UI

    await serve(args)


# --- Entry point ---

def _add_global_options(parser: argparse.ArgumentParser, with_defaults: bool) -> None:
    """Global options, accepted before or after the command name.

    Subcommands get copies whose defaults are SUPPRESS, so an option given
    before the command isn't overwritten by the subcommand's default.
    """
    default = (lambda value: value) if with_defaults else (lambda value: argparse.SUPPRESS)
    parser.add_argument("--address", default=default(None), help="BLE address/UUID of the adapter (see `obdscope scan`)")
    parser.add_argument("--name", default=default(None), help="match adapter by (partial) BLE name instead of auto-detect")
    parser.add_argument(
        "--simulate", choices=sorted(PROFILES), default=default(None),
        help="use a simulated vehicle instead of the adapter: " + ", ".join(
            f"{key} = {profile.label}" for key, profile in sorted(PROFILES.items())
        ),
    )
    parser.add_argument(
        "--protocol", default=default("0"),
        help="ELM327 protocol number: 0 = auto (default), 1 = J1850 PWM (2001 Ford), 6 = CAN 11-bit 500k (2019 RAV4)",
    )
    parser.add_argument("--metric", dest="imperial", action="store_false", default=default(True), help="show metric units")
    parser.add_argument("--log", metavar="FILE", default=default(None), help="append every raw command/response to FILE")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="obdscope", description="Read OBD-II data via a Veepeak OBDCheck BLE adapter.")
    _add_global_options(parser, with_defaults=True)
    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, with_defaults=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, parents=[common], **kwargs)

    p = add("scan", help="list nearby BLE devices")
    p.add_argument("--timeout", type=float, default=8.0)
    p.set_defaults(func=cmd_scan)

    add("info", help="adapter, protocol, VIN, MIL and readiness monitors").set_defaults(func=cmd_info)
    add("pids", help="read every supported mode 01 PID once").set_defaults(func=cmd_pids)

    p = add("live", help="poll PIDs continuously")
    p.add_argument("pids", nargs="*", help=f"aliases or hex PIDs (default: {' '.join(DEFAULT_LIVE)})")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    p.add_argument("--count", type=int, help="stop after N samples")
    p.add_argument("--csv", metavar="FILE", help="also write samples to a CSV file")
    p.set_defaults(func=cmd_live)

    p = add("trims", help="live fuel trim monitor for hunting vacuum leaks")
    p.add_argument("--interval", type=float, default=0.5, help="seconds between samples")
    p.add_argument("--baseline", type=int, default=8, help="samples used to set the baseline")
    p.add_argument("--threshold", type=float, default=5.0, help="%% drop that counts as finding the leak")
    p.add_argument("--count", type=int, help="stop after N samples")
    p.set_defaults(func=cmd_trims)

    add("dtc", help="stored, pending and permanent trouble codes, plus freeze frame").set_defaults(func=cmd_dtc)

    p = add("clear-dtc", help="clear trouble codes (resets readiness monitors)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_clear)

    p = add("raw", help="send raw ELM327 commands (interactive if none given)")
    p.add_argument("commands", nargs="*")
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_raw)

    p = add("modules", help="ask every module on the bus for its trouble codes")
    p.add_argument("--request", help="service to send (default: 13 on older buses, 03 on CAN)")
    p.set_defaults(func=cmd_modules)

    p = add("sniff", help="listen to bus traffic and list the modules that are talking")
    p.add_argument("--seconds", type=float, default=10.0, help="how long to listen")
    p.set_defaults(func=cmd_sniff)

    p = add("probe", help="sweep a range of manufacturer (mode 22) data identifiers")
    p.add_argument("start", help="first DID, hex (e.g. 1100)")
    p.add_argument("end", help="last DID, hex (e.g. 11FF)")
    p.add_argument(
        "--header", default="auto",
        help=(
            f"ATSH header to use; 'auto' = {FORD_PWM_PCM_HEADER} (Ford PCM) on J1850 PWM, "
            f"{CAN_ENGINE_HEADER} (engine ECU) on 11-bit CAN; 'none' to leave default"
        ),
    )
    p.set_defaults(func=cmd_probe)

    p = add("ui", help="open the web dashboard in your browser")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--host", default="127.0.0.1",
        help="interface to listen on; 0.0.0.0 lets a phone on the same Wi-Fi connect (no password!)",
    )
    p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    p.set_defaults(func=cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "header", None) == "none":
        args.header = None
    if args.log:
        handler = logging.FileHandler(args.log)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        elm_log = logging.getLogger("obdscope.elm327")
        elm_log.addHandler(handler)
        elm_log.setLevel(logging.DEBUG)
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        pass
    except (ElmError, TransportError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
