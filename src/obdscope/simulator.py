"""Simulated OBDCheck BLE adapter plugged into a simulated vehicle.

``SimulatedTransport`` is a drop-in for ``BleTransport``: it answers the same
ELM327 text protocol the real adapter does (AT commands, echo, headers,
J1850 PWM and CAN framing, SEARCHING..., NO DATA, negative responses), so
everything above it (parsing, the CLI, the web UI) runs unchanged.

The vehicle repeats a two-minute drive cycle: idle, city speed, highway,
slow down, stop. Sensor values follow from that: RPM from speed and gear,
MAF from RPM and load, the engine warming up from a cold start, O2 sensors
switching once in closed loop, and so on. Each profile has its own protocol,
supported PIDs and trouble codes:

* ``expedition``: 2001 Ford Expedition XLT, 5.4L V8, J1850 PWM, one PCM.
  Check engine light on for P0171/P0174 (lean, both banks), with long term
  fuel trims high at idle and closer to normal at speed, the pattern of a
  vacuum leak. If codes are cleared, P0171 comes back as pending after a
  minute, because the "leak" is still there.
* ``rav4``: 2019 Toyota RAV4 Adventure, 2.5L 4-cylinder, 11-bit CAN, with
  engine (7E8) and transmission (7E9) ECUs. No stored codes, pending P0456
  (tiny evap leak), wideband upstream O2 sensor, VIN available.

The mode 22 identifiers are invented so that ``probe`` has something to find.
They are not real Ford or Toyota identifiers.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable

from .elm327 import PROTOCOLS

# Scales simulated adapter/bus latency; tests set this to 0.
LATENCY_SCALE = 1.0
ELM_VERSION = "ELM327 v2.2"
CYCLE = 120.0  # seconds per drive cycle
# (time in cycle, speed km/h) waypoints, linearly interpolated.
SPEED_PROFILE = [(0, 0), (20, 0), (35, 72), (70, 72), (80, 105), (100, 105), (115, 0), (120, 0)]
AMBIENT = 20.0  # °C


# --- Vehicle state ---

@dataclass
class DriveState:
    t: float
    speed: float        # km/h
    accel: float        # km/h per second
    rpm: float
    throttle: float     # % of travel, 0 = closed
    load: float         # %
    coolant: float      # °C
    iat: float
    maf: float          # g/s
    map: float          # kPa
    timing: float       # ° BTDC
    closed_loop: bool
    decel_cut: bool
    stft: tuple[float, float]
    ltft: tuple[float, float]


@dataclass
class Profile:
    key: str
    label: str
    protocol: str                       # ELM327 protocol number
    ecus: dict[str, set[int]]           # ECU -> supported mode 01 PIDs (bitmask PIDs added automatically)
    engine_ecu: str
    vin: str | None
    idle_rpm: float
    displacement: float                 # litres
    gears: list[tuple[float, float]]    # (top speed of gear km/h, km/h per 1000 rpm)
    banks: int
    o2_present: int                     # PID 13 bitmap
    lean_leak: bool                     # vacuum leak: high long term trims at idle
    stored: list[str]
    pending: list[str]
    permanent: list[str] | None         # None = mode 0A not supported (pre-2010)
    returns_after_clear: list[str]      # pending codes that reappear a minute after clearing
    monitors: int                       # PID 01 byte C: supported non-continuous monitors
    incomplete: int                     # PID 01 byte D
    freeze_pids: set[int]               # mode 02 PIDs captured with the freeze frame
    enhanced: dict[str, dict[int, Callable[[DriveState], bytes]]]  # physical header -> DID -> encoder
    distance_since_clear: float         # km
    minutes_since_clear: float
    # Codes held by modules that aren't the powertrain, keyed by address. Read with
    # mode 13 (Ford's service on this bus), not standard mode 03.
    module_dtcs: dict[str, list[str]] = field(default_factory=dict)
    # Periodic chatter as (priority, target, source, payload) hex; invented, but shaped
    # like real traffic: several modules besides the powertrain share the bus.
    chatter: list[tuple[str, str, str, str]] = field(default_factory=list)
    distance_with_mil: float = 0.0
    fuel_level: float = 60.0            # % at start


def _word(v: float) -> bytes:
    v = int(round(max(0, min(65535, v))))
    return bytes([v >> 8, v & 0xFF])


def _byte(v: float) -> int:
    return int(round(max(0, min(255, v))))


EXPEDITION = Profile(
    key="expedition",
    label="2001 Ford Expedition XLT",
    protocol="1",
    ecus={"10": {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0C, 0x0D, 0x0E, 0x0F,
                 0x10, 0x11, 0x13, 0x14, 0x15, 0x18, 0x19, 0x1C}},
    engine_ecu="10",
    vin="1FMSIMEXP01LA0001",  # this truck does report a VIN, unusually for its year
    idle_rpm=640,
    displacement=5.4,
    gears=[(20, 9), (40, 17), (65, 30), (999, 50)],
    banks=2,
    o2_present=0x33,  # B1S1 B1S2 B2S1 B2S2
    lean_leak=True,
    stored=["P0171", "P0174"],
    pending=[],
    permanent=None,
    returns_after_clear=["P0171"],
    monitors=0xE5,    # catalyst, evap, O2 sensor, O2 heater, EGR
    incomplete=0x04,  # evap not complete
    freeze_pids={0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0C, 0x0D, 0x0F, 0x10, 0x11},
    enhanced={
        "C410F1": {
            0x1101: lambda s: bytes([_byte(s.coolant + 40)]),
            0x1105: lambda s: _word(s.rpm * 4),
            0x1172: lambda s: _word((s.coolant * 0.9 + 12 + 40) * 8),
            0x11B0: lambda s: bytes([0x02 if s.closed_loop else 0x01]),
        },
    },
    module_dtcs={"60": ["B1600", "C1284"], "28": [], "40": []},
    chatter=[
        ("3D", "60", "10", "05 20 1A 00"),   # powertrain
        ("3D", "60", "40", "10 04 00 00"),   # restraints-like module
        ("3D", "60", "28", "41 01 80 00"),   # brakes-like module
        ("3D", "60", "60", "23 00 11 00"),   # cluster-like module
    ],
    distance_since_clear=2917,
    minutes_since_clear=0,
    distance_with_mil=412,
    fuel_level=48,
)

RAV4 = Profile(
    key="rav4",
    label="2019 Toyota RAV4 Adventure",
    protocol="6",
    ecus={
        "7E8": {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11,
                0x13, 0x15, 0x1C, 0x1F, 0x21, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x34, 0x3C,
                0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x49, 0x4A, 0x4C, 0x4D, 0x4E},
        "7E9": {0x01, 0x0D},
    },
    engine_ecu="7E8",
    vin="2T3SIMRAV4KW00001",
    idle_rpm=700,
    displacement=2.5,
    gears=[(15, 8), (25, 12), (40, 18), (55, 25), (70, 32), (85, 40), (100, 48), (999, 55)],
    banks=1,
    o2_present=0x03,  # B1S1 B1S2
    lean_leak=False,
    stored=[],
    pending=["P0456"],
    permanent=[],
    returns_after_clear=[],
    monitors=0xE5,
    incomplete=0x04,
    freeze_pids=set(),
    enhanced={
        "7E0": {
            0x1001: lambda s: _word(s.rpm),
            0x1004: lambda s: bytes([_byte(s.coolant + 40)]),
            0x1028: lambda s: _word(s.maf * 100),
        },
        "7E1": {
            0x1021: lambda s: bytes([_byte(s.coolant * 0.85 + 10 + 40)]),
            0x1024: lambda s: bytes([next(i + 1 for i, (top, _) in enumerate(RAV4.gears) if s.speed <= top)]),
        },
    },
    distance_since_clear=15820,
    minutes_since_clear=31000,
    fuel_level=62,
)

PROFILES = {p.key: p for p in (RAV4, EXPEDITION)}


class VehicleModel:
    def __init__(self, profile: Profile, rng: random.Random):
        self.p = profile
        self.rng = rng

    def noise(self, amount: float) -> float:
        return self.rng.uniform(-amount, amount)

    def state(self, t: float) -> DriveState:
        p = self.p
        speed = _speed(t)
        accel = speed - _speed(t - 1)
        if speed > 0:
            speed = max(0.0, speed + 1.2 * math.sin(t / 4))
        coolant = AMBIENT + (90 - AMBIENT) * (1 - math.exp(-t / 60))
        if coolant > 85:
            coolant += 2 * math.sin(t / 15)  # thermostat cycling
        decel_cut = accel < -1 and speed > 20

        if accel > 0.5:
            throttle = min(70, 22 + accel * 6)
        elif decel_cut or speed == 0:
            throttle = 0.0
        else:
            throttle = 6 + speed * 0.12
        throttle = max(0.0, throttle + self.noise(0.8) * (throttle > 0))

        if speed == 0:
            rpm = p.idle_rpm + (150 if coolant < 50 else 0) + self.noise(15)
        else:
            per_krpm = next(ratio for top, ratio in p.gears if speed <= top)
            rpm = max(p.idle_rpm, speed / per_krpm * 1000 + (300 if accel > 0.5 else 0)) + self.noise(20)

        if speed == 0:
            load = 21 + self.noise(1)
        elif accel > 0.5:
            load = 40 + throttle * 0.7
        elif decel_cut:
            load = 8 + self.noise(1)
        else:
            load = 24 + speed * 0.12 + self.noise(1.5)

        maf = p.displacement * rpm / 120 * 1.18 * load / 100
        closed_loop = coolant >= 45 and not decel_cut
        if closed_loop:
            stft = tuple(3 * math.sin(t * 2.1 + b) + self.noise(1.5) for b in range(2))
        else:
            stft = (0.0, 0.0)
        if p.lean_leak:
            base = 3 + 12 * max(0.0, min(1.0, (20 - maf) / 14))
            ltft = (base, base + 1.5)
        else:
            ltft = (1.6, 0.8)

        if speed == 0:
            timing = 12 + self.noise(1.5)
        elif accel > 0.5:
            timing = 18 + self.noise(2)
        else:
            timing = 30 + self.noise(2)
        return DriveState(
            t=t, speed=speed, accel=accel, rpm=rpm, throttle=throttle, load=load,
            coolant=coolant, iat=AMBIENT + 8 + (6 if speed == 0 else 0), maf=maf,
            map=25 + load * 0.75, timing=timing, closed_loop=closed_loop, decel_cut=decel_cut,
            stft=stft, ltft=ltft,
        )


def _speed(t: float) -> float:
    c = t % CYCLE
    for (t0, v0), (t1, v1) in zip(SPEED_PROFILE, SPEED_PROFILE[1:]):
        if t0 <= c <= t1:
            return v0 + (v1 - v0) * (c - t0) / (t1 - t0)
    return 0.0


# --- The simulated adapter ---

class SimulatedTransport:
    simulated = True

    def __init__(self, profile: str | Profile, clock: Callable[[], float] = time.monotonic, seed: int | None = None):
        self.profile = PROFILES[profile] if isinstance(profile, str) else profile
        self.device = SimpleNamespace(name=f"Simulated {self.profile.label}", address=f"SIM-{self.profile.key.upper()}")
        self._clock = clock
        self._t0 = clock()
        self.model = VehicleModel(self.profile, random.Random(seed))
        self.stored = list(self.profile.stored)
        self.pending = list(self.profile.pending)
        self.incomplete = self.profile.incomplete
        self.cleared_at: float | None = None
        self.freeze = self._capture_freeze_frame() if self.profile.stored else None
        self._reset()

    # Transport interface

    async def stream(self, command: str, seconds: float) -> str:
        """Bus monitoring (ATMA): replay this vehicle's periodic chatter."""
        if not command.upper().startswith("ATMA"):
            return "?\r\r"
        if LATENCY_SCALE:
            await asyncio.sleep(seconds * LATENCY_SCALE)
        lines = []
        for round_ in range(max(1, int(seconds * 2))):
            for priority, target, source, payload in self.profile.chatter:
                frame = bytes.fromhex((priority + target + source + payload).replace(" ", ""))
                lines.append(self._hex(frame + bytes([_j1850_crc(frame)])))
        return "\r".join(lines) + "\r\r"

    async def connect(self) -> None:
        await asyncio.sleep(0.8 * LATENCY_SCALE)  # scanning and GATT setup

    async def close(self) -> None:
        pass

    async def send(self, command: str, timeout: float) -> str:
        text = command.strip()
        # The adapter echoes characters as they arrive, so ATE0 itself is still echoed.
        echo, nl = self.echo, "\r\n" if self.linefeeds else "\r"
        reply = self._handle(text.upper().replace(" ", ""))
        if LATENCY_SCALE:
            await asyncio.sleep(self._latency * LATENCY_SCALE)
        return (text + nl if echo else "") + nl.join(reply.split("\r")) + nl + nl

    # Adapter state

    def _reset(self) -> None:
        self.echo, self.linefeeds, self.spaces, self.headers = True, False, True, False
        self.requested_protocol = "0"
        self.header: str | None = None
        self.connected = False
        self._latency = 0.0

    @property
    def t(self) -> float:
        return self._clock() - self._t0

    @property
    def is_can(self) -> bool:
        return self.profile.protocol in ("6", "7", "8", "9")

    def _handle(self, cmd: str) -> str:
        self._latency = 0.01
        if not cmd:
            return "?"
        if cmd.startswith("AT"):
            return self._at(cmd[2:])
        return self._obd(cmd)

    def _at(self, cmd: str) -> str:
        if cmd in ("Z", "WS"):
            self._reset()
            self._latency = 0.5
            return f"\r\r{ELM_VERSION}"
        if cmd == "I":
            return ELM_VERSION
        if cmd == "D":
            self.spaces, self.headers, self.header = True, False, None
            return "OK"
        flags = {"E": "echo", "L": "linefeeds", "S": "spaces", "H": "headers"}
        if len(cmd) == 2 and cmd[0] in flags and cmd[1] in "01":
            setattr(self, flags[cmd[0]], cmd[1] == "1")
            return "OK"
        if cmd.startswith(("SP", "TP")):
            proto = cmd[2:].removeprefix("A") or "0"
            if proto not in "0123456789ABC" or len(proto) != 1:
                return "?"
            self.requested_protocol, self.connected = proto, False
            return "OK"
        if cmd == "DPN":
            if self.requested_protocol == "0":
                return "A" + (self.profile.protocol if self.connected else "0")
            return self.requested_protocol
        if cmd == "DP":
            name = PROTOCOLS.get(self.profile.protocol) if self.connected else "Automatic"
            return f"AUTO, {name}" if self.requested_protocol == "0" else PROTOCOLS.get(self.requested_protocol, "?")
        if cmd == "RV":
            return f"{14.2 + self.model.noise(0.1):.1f}V"  # engine running: alternator voltage
        if cmd.startswith("SH"):
            value = cmd[2:]
            if len(value) not in (3, 6) or not _is_hex(value):
                return "?"
            self.header = value
            return "OK"
        if cmd in _ACCEPTED_AT or (cmd.startswith(_AT_WITH_HEX_ARG) and _is_hex(cmd.lstrip("STCRAFMWI"))):
            return "OK"
        return "?"

    def _obd(self, cmd: str) -> str:
        if not _is_hex(cmd) or len(cmd) < 2:
            return "?"
        if len(cmd) % 2:
            cmd = cmd[:-1]  # trailing response-count digit
        request = bytes.fromhex(cmd)

        prefix = ""
        if not self.connected:
            if self.requested_protocol not in ("0", self.profile.protocol):
                self._latency = 1.5
                return "UNABLE TO CONNECT"
            if self.requested_protocol == "0":
                prefix = "SEARCHING...\r"
                self._latency = 2.0
            self.connected = True

        state = self.model.state(self.t)
        responses = {ecu: self._respond(ecu, request, state) for ecu in self._targets(request)}
        responses = {ecu: msgs for ecu, msgs in responses.items() if msgs}
        self._latency += 0.03 if self.is_can else 0.06
        if not responses:
            self._latency += 0.1  # adapter waits out its timeout
            return prefix + "NO DATA"
        lines = [line for ecu, msgs in responses.items() for msg in msgs for line in self._format(ecu, msg, request)]
        return prefix + "\r".join(lines)

    def _targets(self, request: bytes) -> list[str]:
        ecus = list(self.profile.ecus)
        if self.is_can:
            header = self.header or "7DF"
            if header == "7DF":
                return ecus if request[0] != 0x22 else []  # enhanced data only on physical addressing
            physical = f"{int(header, 16) + 8:03X}"
            return [physical] if physical in ecus else []
        header = self.header or "616AF1"
        if header == "616AF1":
            return ecus if request[0] != 0x22 else []
        # Physical addressing, "C4 <module> F1": the powertrain or another module.
        if len(header) == 6 and header.startswith("C4") and header.endswith("F1"):
            module = header[2:4]
            if module in ecus or module in self.profile.module_dtcs:
                return [module]
        return []

    # Vehicle responses: each returns complete messages (response SID first)

    def _respond(self, ecu: str, request: bytes, state: DriveState) -> list[bytes]:
        mode, rest = request[0], request[1:]
        engine = ecu == self.profile.engine_ecu
        if ecu not in self.profile.ecus:  # a module that only does manufacturer diagnostics
            if mode == 0x13 and not rest:
                return _legacy_dtc_messages(0x53, self.profile.module_dtcs.get(ecu, []))
            return [bytes([0x7F, mode, 0x11])]
        if mode == 0x13 and not rest and not self.is_can:
            return _legacy_dtc_messages(0x53, self._dtcs()[0])
        if mode == 0x01 and len(rest) == 1:
            data = self._pid(ecu, rest[0], state)
            return [bytes([0x41, rest[0]]) + data] if data is not None else []
        if mode == 0x02 and len(rest) >= 1 and engine:
            data = self._freeze_pid(rest[0])
            return [bytes([0x42, rest[0], 0x00]) + data] if data is not None else []
        if mode in (0x03, 0x07, 0x0A):
            return self._dtc_messages(mode, engine)
        if mode == 0x04:
            if engine:
                self._clear()
            return [b"\x44"]
        if mode == 0x09 and engine and self.profile.vin and rest in (b"\x00", b"\x02"):
            if rest == b"\x00":
                return [b"\x49\x00" + _bitmask(0x00, {0x02})]
            return [b"\x49\x02\x01" + self.profile.vin.encode()]
        if mode == 0x22 and len(rest) == 2:
            dids = self.profile.enhanced.get(self.header or "", {})
            did = (rest[0] << 8) | rest[1]
            if did in dids:
                return [b"\x62" + rest + dids[did](state)]
            return [bytes([0x7F, 0x22, 0x31])]
        if not self.is_can:
            return []  # legacy ECUs ignore what they don't support
        return [bytes([0x7F, mode, 0x11])] if engine else []

    def _pid(self, ecu: str, pid: int, state: DriveState) -> bytes | None:
        supported = self.profile.ecus[ecu]
        if pid % 0x20 == 0:
            if pid and pid not in _with_bitmask_pids(supported):
                return None
            return _bitmask(pid, supported)
        if pid not in supported:
            return None
        if ecu != self.profile.engine_ecu:
            return self._transmission_pid(pid, state)
        return self._engine_pid(pid, state)

    def _transmission_pid(self, pid: int, s: DriveState) -> bytes | None:
        if pid == 0x01:
            return bytes([0x00, 0x04, 0x00, 0x00])
        if pid == 0x0D:
            return bytes([_byte(s.speed)])
        return None

    def _engine_pid(self, pid: int, s: DriveState) -> bytes | None:
        p, noise = self.profile, self.model.noise
        trim = lambda v: _byte(v * 128 / 100 + 128)
        pct = lambda v: _byte(v * 255 / 100)
        stored, pending = self._dtcs()
        mil = bool(stored)
        if pid == 0x01:
            return bytes([(0x80 if mil else 0) | len(stored), 0x07, p.monitors, self.incomplete])
        if pid == 0x03:
            status = 0x01 if s.coolant < 45 else 0x04 if s.decel_cut else 0x02
            return bytes([status, status if p.banks == 2 else 0])
        if pid == 0x04:
            return bytes([pct(s.load)])
        if pid == 0x05:
            return bytes([_byte(s.coolant + 40)])
        if pid in (0x06, 0x08):
            return bytes([trim(s.stft[(pid - 0x06) // 2])])
        if pid in (0x07, 0x09):
            return bytes([trim(s.ltft[(pid - 0x07) // 2])])
        if pid == 0x0B:
            return bytes([_byte(s.map)])
        if pid == 0x0C:
            return _word(s.rpm * 4)
        if pid == 0x0D:
            return bytes([_byte(s.speed)])
        if pid == 0x0E:
            return bytes([_byte((s.timing + 64) * 2)])
        if pid == 0x0F:
            return bytes([_byte(s.iat + 40)])
        if pid == 0x10:
            return _word(s.maf * 100)
        if pid in (0x11, 0x47):
            return bytes([pct(14 + s.throttle * 0.85 + (5 if pid == 0x47 else 0))])
        if pid == 0x13:
            return bytes([p.o2_present])
        if 0x14 <= pid <= 0x1B:
            return bytes([_byte(self._narrowband(pid, s) * 200), 0xFF])
        if pid == 0x1C:
            return b"\x01"
        if pid == 0x1F:
            return _word(s.t)
        if pid == 0x21:
            return _word(p.distance_with_mil if mil else 0)
        if pid == 0x2E:
            return bytes([pct(25 + noise(3) if s.closed_loop and s.speed > 0 else 0)])
        if pid == 0x2F:
            return bytes([pct(max(5, p.fuel_level - s.t * 0.002))])
        if pid == 0x30:
            return bytes([0 if self.cleared_at is not None else 18])
        if pid == 0x31:
            return _word(self._since_clear(p.distance_since_clear, 0.015))
        if pid == 0x33:
            return b"\x65"
        if pid == 0x34:
            lam = 0.95 if s.coolant < 45 else 1.99 if s.decel_cut else 1.0 + 0.01 * math.sin(s.t * 3)
            current = max(-128, min(127, (lam - 1) * 4))
            return _word(lam * 32768) + _word((current + 128) * 256)
        if pid == 0x3C:
            return _word((100 + 480 * (1 - math.exp(-s.t / 80)) + s.load * 2 + 40) * 10)
        if pid == 0x41:
            return bytes([0x00, 0x07, p.monitors, self.incomplete])
        if pid == 0x42:
            return _word((14.2 + noise(0.1)) * 1000)
        if pid == 0x43:
            return _word(s.load * 1.1 * 255 / 100)
        if pid == 0x44:
            return _word((0.95 if s.coolant < 45 else 1.0) * 32768)
        if pid == 0x45:
            return bytes([pct(s.throttle)])
        if pid == 0x46:
            return bytes([_byte(AMBIENT + 40)])
        if pid == 0x49:
            return bytes([pct(15 + s.throttle * 0.8)])
        if pid == 0x4A:
            return bytes([pct((15 + s.throttle * 0.8) / 2 + 7.5)])
        if pid == 0x4C:
            return bytes([pct(s.throttle * 0.9)])
        if pid == 0x4D:
            return _word(0)
        if pid == 0x4E:
            return _word(self._since_clear(p.minutes_since_clear, 1 / 60))
        return None

    def _narrowband(self, pid: int, s: DriveState) -> float:
        upstream = (pid - 0x14) % 4 == 0
        if s.decel_cut:
            return 0.05
        if not s.closed_loop:
            return 0.45 if s.t < 15 else 0.78  # heating up, then running rich
        if upstream:
            return 0.45 + 0.4 * math.sin(2 * math.pi * 1.2 * s.t + pid)
        return 0.65 + 0.05 * math.sin(s.t / 3)

    def _since_clear(self, base: float, per_second: float) -> float:
        """A counter that clearing codes resets (distance, time since cleared)."""
        if self.cleared_at is None:
            return base + per_second * self.t
        return per_second * (self.t - self.cleared_at)

    # Trouble codes

    def _dtcs(self) -> tuple[list[str], list[str]]:
        pending = list(self.pending)
        if self.cleared_at is not None and self.t - self.cleared_at > 60:
            pending += [c for c in self.profile.returns_after_clear if c not in pending]
        return self.stored, pending

    def _dtc_messages(self, mode: int, engine: bool) -> list[bytes]:
        stored, pending = self._dtcs()
        if mode == 0x0A:
            if self.profile.permanent is None:
                return []
            codes = self.profile.permanent
        else:
            codes = stored if mode == 0x03 else pending
        if not engine:
            codes = []
        sid = mode + 0x40
        if self.is_can:
            return [bytes([sid, len(codes)]) + b"".join(_encode_dtc(c) for c in codes)]
        return _legacy_dtc_messages(sid, codes)

    def _clear(self) -> None:
        self.stored, self.pending, self.freeze = [], [], None
        self.incomplete = self.profile.monitors  # every monitor must run again
        self.cleared_at = self.t

    def _capture_freeze_frame(self) -> dict[int, bytes]:
        warm_idle = self.model.state(CYCLE * 6 - 2)
        frame = {pid: self._engine_pid(pid, warm_idle) for pid in self.profile.freeze_pids}
        frame[0x02] = _encode_dtc(self.profile.stored[0])
        return frame

    def _freeze_pid(self, pid: int) -> bytes | None:
        frame = self.freeze or {}
        if pid == 0x02:
            return frame.get(0x02, b"\x00\x00")
        if pid % 0x20 == 0:
            return _bitmask(pid, set(frame) | {0x02}) if pid == 0 else None
        return frame.get(pid)

    # Output formatting

    def _format(self, ecu: str, message: bytes, request: bytes) -> list[str]:
        if self.is_can:
            frames = _isotp_frames(message)
            if self.headers:
                return [f"{ecu} {self._hex(f)}" if self.spaces else f"{ecu}{self._hex(f)}" for f in frames]
            if len(frames) == 1:
                return [self._hex(message)]
            lines = [f"{len(message):03X}"]
            data = message
            lines.append(f"0: {self._hex(data[:6])}")
            for i, start in enumerate(range(6, len(data), 7), start=1):
                lines.append(f"{i % 16:X}: {self._hex(data[start : start + 7])}")
            return lines
        if not self.headers:
            return [self._hex(message)]
        physical = request[0] == 0x22
        header = bytes([0xC4, 0xF1, int(ecu, 16)]) if physical else bytes([0x41, 0x6B, int(ecu, 16)])
        frame = header + message
        return [self._hex(frame + bytes([_j1850_crc(frame)]))]

    def _hex(self, data: bytes) -> str:
        return (" " if self.spaces else "").join(f"{b:02X}" for b in data)


# --- Helpers ---

# AT commands the simulated adapter accepts without modelling them.
_ACCEPTED_AT = {"AL", "NL", "PC", "BI", "CAF0", "CAF1", "CFC0", "CFC1", "M0", "M1", "R0", "R1", "V0", "V1",
                "AT0", "AT1", "AT2"}
_AT_WITH_HEX_ARG = ("ST", "CRA", "CF", "CM", "SW", "TA", "WM", "IIA")


def _is_hex(text: str) -> bool:
    return all(c in "0123456789ABCDEF" for c in text)


def _legacy_dtc_messages(sid: int, codes: list[str]) -> list[bytes]:
    """Three codes per message, zero padded, as legacy buses report them."""
    packed = b"".join(_encode_dtc(c) for c in codes)
    chunks = [packed[i : i + 6] for i in range(0, len(packed), 6)] or [b""]
    return [bytes([sid]) + chunk.ljust(6, b"\x00") for chunk in chunks]


def _encode_dtc(code: str) -> bytes:
    system = "PCBU".index(code[0])
    value = (system << 14) | (int(code[1]) << 12) | int(code[2:], 16)
    return bytes([value >> 8, value & 0xFF])


def _with_bitmask_pids(supported: set[int]) -> set[int]:
    """Add the 'PIDs supported' entries (00, 20, 40...) implied by the highest PID."""
    top = max(supported)
    return supported | {base for base in range(0x20, top, 0x20)}


def _bitmask(base: int, supported: set[int]) -> bytes:
    pids = _with_bitmask_pids(supported)
    bits = 0
    for i in range(32):
        if base + i + 1 in pids:
            bits |= 1 << (31 - i)
    return bits.to_bytes(4, "big")


def _isotp_frames(message: bytes) -> list[bytes]:
    if len(message) <= 7:
        return [(bytes([len(message)]) + message).ljust(8, b"\x00")]
    frames = [bytes([0x10 | (len(message) >> 8), len(message) & 0xFF]) + message[:6]]
    for seq, start in enumerate(range(6, len(message), 7), start=1):
        frames.append((bytes([0x20 | (seq & 0x0F)]) + message[start : start + 7]).ljust(8, b"\x00"))
    return frames


def _j1850_crc(data: bytes) -> int:
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF
