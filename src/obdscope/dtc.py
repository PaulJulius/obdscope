"""Diagnostic trouble code decoding."""

from __future__ import annotations

_SYSTEMS = "PCBU"

# A small set of generic codes plus Ford codes common on 1997-2004 trucks.
# Anything not listed still decodes; look it up in a service manual or online.
DESCRIPTIONS = {
    "P0100": "Mass air flow circuit malfunction",
    "P0101": "Mass air flow circuit range/performance",
    "P0102": "Mass air flow circuit low input",
    "P0113": "Intake air temperature circuit high input",
    "P0117": "Coolant temperature circuit low input",
    "P0118": "Coolant temperature circuit high input",
    "P0125": "Insufficient coolant temperature for closed loop",
    "P0128": "Coolant thermostat below regulating temperature",
    "P0171": "System too lean, bank 1",
    "P0172": "System too rich, bank 1",
    "P0174": "System too lean, bank 2",
    "P0175": "System too rich, bank 2",
    "P0300": "Random/multiple cylinder misfire detected",
    **{f"P030{n}": f"Cylinder {n} misfire detected" for n in range(1, 9)},
    "P0320": "Ignition/distributor engine speed input circuit",
    "P0340": "Camshaft position sensor circuit",
    "P0401": "EGR flow insufficient",
    "P0402": "EGR flow excessive",
    "P0420": "Catalyst efficiency below threshold, bank 1",
    "P0430": "Catalyst efficiency below threshold, bank 2",
    "P0440": "Evaporative emission system malfunction",
    "P0442": "Evaporative emission system small leak",
    "P0443": "Evaporative emission purge control valve circuit",
    "P0446": "Evaporative emission vent control circuit",
    "P0455": "Evaporative emission system large leak",
    "P0456": "Evaporative emission system very small leak",
    "P0500": "Vehicle speed sensor malfunction",
    "P0505": "Idle air control system malfunction",
    "P0603": "PCM keep-alive memory test error",
    "P0703": "Brake switch input malfunction",
    "P0715": "Transmission input/turbine speed sensor circuit",
    "P0741": "Torque converter clutch performance / stuck off",
    "P0750": "Shift solenoid A malfunction",
    "P0755": "Shift solenoid B malfunction",
    "P1000": "Ford: OBD-II monitor testing not complete (normal after clearing codes)",
    # Non-powertrain modules report manufacturer-specific codes (B = body,
    # C = chassis, U = network), read with the maker's own service, not mode 03.
    "B1600": "Ford: PATS ignition key transponder signal not received",
    "P1131": "Ford: Lack of upstream O2 switch, sensor indicates lean, bank 1",
    "P1151": "Ford: Lack of upstream O2 switch, sensor indicates lean, bank 2",
}


def decode_dtc(hi: int, lo: int) -> str:
    return f"{_SYSTEMS[hi >> 6]}{(hi >> 4) & 0x03}{hi & 0x0F:X}{lo:02X}"


def decode_dtcs(data: bytes) -> list[str]:
    """Decode packed 2-byte DTCs, skipping the 00 00 padding."""
    return [decode_dtc(data[i], data[i + 1]) for i in range(0, len(data) - 1, 2) if data[i] or data[i + 1]]


def describe(code: str) -> str:
    return DESCRIPTIONS.get(code, "")
