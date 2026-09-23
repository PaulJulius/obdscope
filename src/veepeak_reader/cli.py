"""Command-line interface: ``veepeak <command>``."""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import time
from contextlib import asynccontextmanager

from . import reports
from .elm327 import Elm327, ElmError, NoData
from .obd import NegativeResponse, Vehicle, ecu_name, primary
from .pids import DEFAULT_LIVE, PIDS, format_value, parse_pid
from .simulator import PROFILES
from .transport import TransportError, discover, make_transport

FORD_PWM_PCM_HEADER = "C410F1"  # priority C4, target PCM (10), tester (F1)
CAN_ENGINE_HEADER = "7E0"  # physical request ID of the engine ECU (answers on 7E8)
VEHICLE_HINT = (
    "The adapter is connected but the vehicle's computer isn't answering. Turn the ignition to ON\n"
    "(dash lights up; the engine doesn't need to run). Adapter-only commands still work: `veepeak raw ATRV`."
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
    parser.add_argument("--address", default=default(None), help="BLE address/UUID of the adapter (see `veepeak scan`)")
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
    parser = argparse.ArgumentParser(prog="veepeak", description="Read OBD-II data via a Veepeak OBDCheck BLE adapter.")
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

    add("dtc", help="stored, pending and permanent trouble codes, plus freeze frame").set_defaults(func=cmd_dtc)

    p = add("clear-dtc", help="clear trouble codes (resets readiness monitors)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_clear)

    p = add("raw", help="send raw ELM327 commands (interactive if none given)")
    p.add_argument("commands", nargs="*")
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_raw)

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
        elm_log = logging.getLogger("veepeak_reader.elm327")
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
