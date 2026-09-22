"""Mode 01 / 02 PID definitions and decoders (SAE J1979)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class PidInfo:
    pid: int
    name: str
    unit: str
    decode: Callable[[bytes], float | int | str]


def _word(d: bytes) -> int:
    return d[0] * 256 + d[1]


def _percent(d: bytes) -> float:
    return d[0] * 100 / 255


def _trim(d: bytes) -> float:
    return (d[0] - 128) * 100 / 128


def _temp(d: bytes) -> int:
    return d[0] - 40


def _lambda(d: bytes) -> float:
    return _word(d) * 2 / 65536


FUEL_SYSTEM_STATUS = {
    0x00: "not in use",
    0x01: "open loop (engine cold)",
    0x02: "closed loop",
    0x04: "open loop (load/decel)",
    0x08: "open loop (system fault)",
    0x10: "closed loop (feedback fault)",
}

OBD_STANDARDS = {
    1: "OBD-II (CARB)", 2: "OBD (EPA)", 3: "OBD and OBD-II", 4: "OBD-I", 5: "Not OBD compliant",
    6: "EOBD (Europe)", 7: "EOBD and OBD-II", 8: "EOBD and OBD", 9: "EOBD, OBD and OBD-II",
    10: "JOBD (Japan)", 11: "JOBD and OBD-II", 12: "JOBD and EOBD", 13: "JOBD, EOBD and OBD-II",
}

SECONDARY_AIR = {1: "upstream", 2: "downstream of catalyst", 4: "from outside atmosphere / off", 8: "pump commanded on"}

O2_LABELS = ["B1S1", "B1S2", "B1S3", "B1S4", "B2S1", "B2S2", "B2S3", "B2S4"]


def _fuel_system(d: bytes) -> str:
    banks = [FUEL_SYSTEM_STATUS.get(b, f"0x{b:02X}") for b in d[:2]]
    return f"bank 1: {banks[0]}" + (f"; bank 2: {banks[1]}" if len(banks) > 1 and d[1] else "")


def _o2_present(d: bytes) -> str:
    return " ".join(label for i, label in enumerate(O2_LABELS) if d[0] & (1 << i)) or "none"


PIDS: dict[int, PidInfo] = {
    p.pid: p
    for p in [
        PidInfo(0x03, "Fuel system status", "", _fuel_system),
        PidInfo(0x04, "Calculated engine load", "%", _percent),
        PidInfo(0x05, "Coolant temperature", "°C", _temp),
        PidInfo(0x06, "Short term fuel trim, bank 1", "%", _trim),
        PidInfo(0x07, "Long term fuel trim, bank 1", "%", _trim),
        PidInfo(0x08, "Short term fuel trim, bank 2", "%", _trim),
        PidInfo(0x09, "Long term fuel trim, bank 2", "%", _trim),
        PidInfo(0x0A, "Fuel pressure (gauge)", "kPa", lambda d: d[0] * 3),
        PidInfo(0x0B, "Intake manifold pressure", "kPa", lambda d: d[0]),
        PidInfo(0x0C, "Engine RPM", "rpm", lambda d: _word(d) / 4),
        PidInfo(0x0D, "Vehicle speed", "km/h", lambda d: d[0]),
        PidInfo(0x0E, "Timing advance", "°", lambda d: d[0] / 2 - 64),
        PidInfo(0x0F, "Intake air temperature", "°C", _temp),
        PidInfo(0x10, "Mass air flow", "g/s", lambda d: _word(d) / 100),
        PidInfo(0x11, "Throttle position", "%", _percent),
        PidInfo(0x12, "Secondary air status", "", lambda d: SECONDARY_AIR.get(d[0], f"0x{d[0]:02X}")),
        PidInfo(0x13, "O2 sensors present", "", _o2_present),
        *(
            PidInfo(0x14 + i, f"O2 sensor {label} voltage", "V", lambda d: d[0] / 200)
            for i, label in enumerate(O2_LABELS)
        ),
        PidInfo(0x1C, "OBD standard", "", lambda d: OBD_STANDARDS.get(d[0], f"0x{d[0]:02X}")),
        PidInfo(0x1F, "Run time since engine start", "s", _word),
        PidInfo(0x21, "Distance with MIL on", "km", _word),
        # Wideband (air-fuel ratio) sensors, used by most modern engines for the upstream sensor.
        *(
            PidInfo(base + i, f"Wideband O2 {label} lambda", "λ", _lambda)
            for base in (0x24, 0x34)
            for i, label in enumerate(O2_LABELS)
        ),
        PidInfo(0x2C, "Commanded EGR", "%", _percent),
        PidInfo(0x2D, "EGR error", "%", _trim),
        PidInfo(0x2E, "Commanded evap purge", "%", _percent),
        PidInfo(0x2F, "Fuel tank level", "%", _percent),
        PidInfo(0x30, "Warm-ups since codes cleared", "", lambda d: d[0]),
        PidInfo(0x31, "Distance since codes cleared", "km", _word),
        PidInfo(0x32, "Evap system vapor pressure", "Pa", lambda d: int.from_bytes(d[:2], "big", signed=True) / 4),
        PidInfo(0x33, "Barometric pressure", "kPa", lambda d: d[0]),
        *(
            PidInfo(0x3C + i, f"Catalyst temperature {label}", "°C", lambda d: _word(d) / 10 - 40)
            for i, label in enumerate(["B1S1", "B2S1", "B1S2", "B2S2"])
        ),
        PidInfo(0x42, "Control module voltage", "V", lambda d: _word(d) / 1000),
        PidInfo(0x43, "Absolute load", "%", lambda d: _word(d) * 100 / 255),
        PidInfo(0x44, "Commanded air-fuel equivalence ratio", "λ", lambda d: _word(d) / 32768),
        PidInfo(0x45, "Relative throttle position", "%", _percent),
        PidInfo(0x46, "Ambient air temperature", "°C", _temp),
        PidInfo(0x47, "Absolute throttle position B", "%", _percent),
        PidInfo(0x49, "Accelerator pedal position D", "%", _percent),
        PidInfo(0x4A, "Accelerator pedal position E", "%", _percent),
        PidInfo(0x4C, "Commanded throttle actuator", "%", _percent),
        PidInfo(0x4D, "Time run with MIL on", "min", _word),
        PidInfo(0x4E, "Time since codes cleared", "min", _word),
        PidInfo(0x5A, "Relative accelerator pedal position", "%", _percent),
        PidInfo(0x5C, "Engine oil temperature", "°C", _temp),
        PidInfo(0x5E, "Engine fuel rate", "L/h", lambda d: _word(d) / 20),
        PidInfo(0x61, "Driver demand torque", "%", lambda d: d[0] - 125),
        PidInfo(0x62, "Actual engine torque", "%", lambda d: d[0] - 125),
        PidInfo(0x63, "Engine reference torque", "Nm", _word),
        PidInfo(0xA6, "Odometer", "km", lambda d: int.from_bytes(d[:4], "big") / 10),
    ]
}

ALIASES = {
    "load": 0x04, "coolant": 0x05, "stft1": 0x06, "ltft1": 0x07, "stft2": 0x08, "ltft2": 0x09,
    "map": 0x0B, "rpm": 0x0C, "speed": 0x0D, "timing": 0x0E, "iat": 0x0F, "maf": 0x10,
    "throttle": 0x11, "o2b1s1": 0x14, "o2b1s2": 0x15, "o2b2s1": 0x18, "o2b2s2": 0x19,
    "runtime": 0x1F, "fuel": 0x2F, "baro": 0x33, "cat1": 0x3C, "ambient": 0x46,
    "pedal": 0x49, "fuelrate": 0x5E, "torque": 0x62, "odometer": 0xA6,
}


def parse_pid(text: str) -> int:
    """Accept an alias (``rpm``) or hex PID (``0C`` / ``0x0C``)."""
    key = text.lower()
    if key in ALIASES:
        return ALIASES[key]
    try:
        return int(key, 16)
    except ValueError:
        raise ValueError(f"unknown PID {text!r}; use hex or one of: {', '.join(sorted(ALIASES))}") from None


def decode_supported(base: int, data: bytes) -> set[int]:
    """Decode a 4-byte 'PIDs supported [base+1 .. base+0x20]' bitmask."""
    bits = int.from_bytes(data[:4], "big")
    return {base + i + 1 for i in range(32) if bits & (1 << (31 - i))}


# --- Readiness monitors (PID 01) ---

CONTINUOUS_MONITORS = ["Misfire", "Fuel system", "Comprehensive components"]
SPARK_MONITORS = [
    "Catalyst", "Heated catalyst", "Evaporative system", "Secondary air system",
    "A/C refrigerant", "Oxygen sensor", "Oxygen sensor heater", "EGR system",
]
DIESEL_MONITORS = [
    "NMHC catalyst", "NOx/SCR aftertreatment", None, "Boost pressure",
    None, "Exhaust gas sensor", "PM filter", "EGR/VVT system",
]


@dataclass
class MonitorStatus:
    mil_on: bool
    dtc_count: int
    diesel: bool
    monitors: list[tuple[str, bool]]  # (name, complete) for supported monitors only


def decode_monitor_status(d: bytes) -> MonitorStatus:
    a, b, c, dd = d[:4]
    diesel = bool(b & 0x08)
    monitors = [(name, not b & (0x10 << i)) for i, name in enumerate(CONTINUOUS_MONITORS) if b & (1 << i)]
    names = DIESEL_MONITORS if diesel else SPARK_MONITORS
    monitors += [(name, not dd & (1 << i)) for i, name in enumerate(names) if name and c & (1 << i)]
    return MonitorStatus(mil_on=bool(a & 0x80), dtc_count=a & 0x7F, diesel=diesel, monitors=monitors)


# --- Display ---

def convert(value, unit: str, imperial: bool):
    if not imperial or not isinstance(value, (int, float)):
        return value, unit
    if unit == "°C":
        return value * 9 / 5 + 32, "°F"
    if unit == "km/h":
        return value * 0.621371, "mph"
    if unit == "km":
        return value * 0.621371, "mi"
    if unit == "kPa":
        return value * 0.145038, "psi"
    return value, unit


def format_value(value, unit: str, imperial: bool) -> str:
    value, unit = convert(value, unit, imperial)
    if isinstance(value, float):
        text = f"{value:.3f}" if unit in ("V", "λ") else f"{value:.1f}"
    else:
        text = str(value)
    return f"{text} {unit}".rstrip()
