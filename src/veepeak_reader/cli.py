"""Command-line interface: ``veepeak <command>``."""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
import time
from contextlib import asynccontextmanager

from .dtc import decode_dtc, describe
from .elm327 import Elm327, ElmError, NoData
from .obd import NegativeResponse, Vehicle
from .pids import PIDS, decode_monitor_status, format_value, parse_pid
from .transport import BleTransport, TransportError, discover

DEFAULT_LIVE = ["rpm", "speed", "coolant", "load", "throttle", "stft1", "ltft1", "stft2", "ltft2"]
FORD_PWM_PCM_HEADER = "C410F1"  # priority C4, target PCM (10), tester (F1)


def status(message: str) -> None:
    print(message, file=sys.stderr)


@asynccontextmanager
async def session(args):
    transport = BleTransport(address=args.address, name=args.name)
    status("Connecting to adapter...")
    await transport.connect()
    try:
        status(f"Connected to {transport.device.name or 'adapter'} ({transport.device.address}). Initializing...")
        elm = Elm327(transport, protocol=args.protocol)
        await elm.initialize()
        status(f"{elm.version}, protocol {elm.protocol}: {elm.protocol_name}\n")
        yield Vehicle(elm)
    finally:
        await transport.close()


# --- Commands ---

async def cmd_scan(args) -> None:
    status(f"Scanning for {args.timeout:.0f}s...")
    for d in await discover(args.timeout):
        marker = "*" if d.likely_obd else " "
        print(f"{marker} {d.rssi:>4} dBm  {d.device.address}  {d.name or '(no name)'}")
    status("\n* = looks like an OBD adapter")


async def cmd_info(args) -> None:
    async with session(args) as v:
        print(f"Adapter:     {v.elm.version}")
        print(f"Battery:     {await v.elm.voltage()}")
        print(f"Protocol:    {v.elm.protocol} - {v.elm.protocol_name}")
        vin = await v.vin()
        print(f"VIN:         {vin or 'not reported (mode 09 is uncommon before MY2005)'}")
        supported = await v.supported_pids()
        if 0x1C in supported:
            print(f"OBD type:    {PIDS[0x1C].decode(next(iter((await v.pid(0x1C)).values())))}")
        for ecu, data in (await v.pid(0x01)).items():
            ms = decode_monitor_status(data)
            print(f"\nECU {ecu}:  MIL {'ON' if ms.mil_on else 'off'}, {ms.dtc_count} stored code(s)")
            print("Readiness monitors:")
            for name, complete in ms.monitors:
                print(f"  {name:<28} {'complete' if complete else 'NOT complete'}")
        print("\nSupported mode 01 PIDs: " + " ".join(f"{p:02X}" for p in sorted(supported)))


async def cmd_pids(args) -> None:
    async with session(args) as v:
        for pid in sorted(await v.supported_pids()):
            if pid % 0x20 == 0 or pid == 0x01:
                continue  # bitmask / monitor PIDs are shown by `info`
            try:
                per_ecu = await v.pid(pid)
            except (NoData, NegativeResponse):
                continue
            for ecu, data in per_ecu.items():
                info = PIDS.get(pid)
                if info:
                    value = format_value(info.decode(data), info.unit, args.imperial)
                    print(f"{pid:02X}  {info.name:<38} {value}")
                else:
                    print(f"{pid:02X}  {'(undecoded)':<38} {data.hex(' ').upper()}")


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
                        value = PIDS[p].decode(next(iter((await v.pid(p)).values())))
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
        for label, mode in (("Stored", 0x03), ("Pending", 0x07)):
            codes = await v.dtcs(mode)
            print(f"{label} codes:")
            if not any(codes.values()):
                print("  none")
            for ecu, ecu_codes in codes.items():
                for code in ecu_codes:
                    print(f"  {code}  {describe(code)}  (ECU {ecu})")
        await _print_freeze_frame(v, args.imperial)


async def _print_freeze_frame(v: Vehicle, imperial: bool) -> None:
    try:
        trigger = next(iter((await v.freeze_frame_pid(0x02)).values()))
    except (NoData, NegativeResponse):
        return
    if not any(trigger):
        return
    print(f"\nFreeze frame (captured when {decode_dtc(trigger[0], trigger[1])} set):")
    for pid in sorted(await v.freeze_frame_supported()):
        info = PIDS.get(pid)
        if not info:
            continue
        try:
            data = next(iter((await v.freeze_frame_pid(pid)).values()))
        except (NoData, NegativeResponse):
            continue
        print(f"  {info.name:<38} {format_value(info.decode(data), info.unit, imperial)}")


async def cmd_clear(args) -> None:
    if not args.yes:
        print(
            "This clears stored codes and freeze-frame data and RESETS all readiness monitors.\n"
            "The truck will fail an emissions inspection until the monitors complete again\n"
            "(usually a few days of mixed driving). Ignition should be ON, engine OFF."
        )
        if input("Type 'clear' to continue: ").strip().lower() != "clear":
            print("Cancelled.")
            return
    async with session(args) as v:
        await v.clear_dtcs()
        print("Codes cleared.")


async def cmd_raw(args) -> None:
    async with session(args) as v:
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
            header = FORD_PWM_PCM_HEADER if v.elm.protocol == "1" else None
        if header:
            await v.elm.command(f"ATSH{header}")
            status(f"Using header {header}")
        status(f"Probing mode 22 DIDs {start:04X}-{end:04X}...")
        found = 0
        for did in range(start, end + 1):
            try:
                for ecu, data in (await v.enhanced(did)).items():
                    print(f"{did:04X}  ECU {ecu}  {data.hex(' ').upper()}")
                    found += 1
            except NoData:
                pass
            except NegativeResponse as e:
                if e.code != 0x31:  # "out of range" just means unsupported
                    print(f"{did:04X}  {e}")
        status(f"\n{found} DID(s) responded.")


# --- Entry point ---

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="veepeak", description="Read OBD-II data via a Veepeak OBDCheck BLE adapter.")
    parser.add_argument("--address", help="BLE address/UUID of the adapter (see `veepeak scan`)")
    parser.add_argument("--name", help="match adapter by (partial) BLE name instead of auto-detect")
    parser.add_argument(
        "--protocol", default="0",
        help="ELM327 protocol number: 0 = auto (default), 1 = J1850 PWM (2001 Ford), 6 = CAN 11-bit 500k",
    )
    parser.add_argument("--metric", dest="imperial", action="store_false", help="show metric units")
    parser.add_argument("--log", metavar="FILE", help="append every raw command/response to FILE")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="list nearby BLE devices")
    p.add_argument("--timeout", type=float, default=8.0)
    p.set_defaults(func=cmd_scan)

    sub.add_parser("info", help="adapter, protocol, VIN, MIL and readiness monitors").set_defaults(func=cmd_info)
    sub.add_parser("pids", help="read every supported mode 01 PID once").set_defaults(func=cmd_pids)

    p = sub.add_parser("live", help="poll PIDs continuously")
    p.add_argument("pids", nargs="*", help=f"aliases or hex PIDs (default: {' '.join(DEFAULT_LIVE)})")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    p.add_argument("--count", type=int, help="stop after N samples")
    p.add_argument("--csv", metavar="FILE", help="also write samples to a CSV file")
    p.set_defaults(func=cmd_live)

    sub.add_parser("dtc", help="stored and pending trouble codes, plus freeze frame").set_defaults(func=cmd_dtc)

    p = sub.add_parser("clear-dtc", help="clear trouble codes (resets readiness monitors)")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("raw", help="send raw ELM327 commands (interactive if none given)")
    p.add_argument("commands", nargs="*")
    p.add_argument("--timeout", type=float, default=5.0)
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("probe", help="sweep a range of manufacturer (mode 22) data identifiers")
    p.add_argument("start", help="first DID, hex (e.g. 1100)")
    p.add_argument("end", help="last DID, hex (e.g. 11FF)")
    p.add_argument(
        "--header", default="auto",
        help=f"ATSH header to use; 'auto' = {FORD_PWM_PCM_HEADER} (Ford PCM) on J1850 PWM, 'none' to leave default",
    )
    p.set_defaults(func=cmd_probe)
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
