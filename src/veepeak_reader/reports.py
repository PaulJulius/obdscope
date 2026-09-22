"""Multi-request vehicle reports shared by the CLI and the web UI.

Each returns plain, JSON-friendly data so callers can print it or serve it.
"""

from __future__ import annotations

from .dtc import decode_dtc, describe
from .elm327 import NoData
from .obd import NegativeResponse, Vehicle, ecu_name, primary
from .pids import PIDS, decode_monitor_status, format_value

_UNAVAILABLE = (NoData, NegativeResponse)


async def vehicle_info(v: Vehicle) -> dict:
    supported = await v.supported_pids()
    obd_standard = None
    if 0x1C in supported:
        try:
            obd_standard = PIDS[0x1C].decode(primary(await v.pid(0x1C)))
        except _UNAVAILABLE:
            pass
    try:
        monitor_status = await v.pid(0x01)
    except _UNAVAILABLE:
        monitor_status = {}
    ecus = []
    for ecu, data in monitor_status.items():
        ms = decode_monitor_status(data)
        ecus.append({
            "ecu": ecu_name(ecu),
            "mil_on": ms.mil_on,
            "dtc_count": ms.dtc_count,
            "monitors": [{"name": name, "complete": complete} for name, complete in ms.monitors],
        })
    return {
        "adapter": v.elm.version,
        "voltage": await v.elm.voltage(),
        "protocol": v.elm.protocol,
        "protocol_name": v.elm.protocol_name,
        "vin": await v.vin(),
        "obd_standard": obd_standard,
        "ecus": ecus,
        "supported": sorted(supported),
    }


async def all_readings(v: Vehicle, imperial: bool) -> list[dict]:
    """One reading of every supported mode 01 PID."""
    rows = []
    for pid in sorted(await v.supported_pids()):
        if pid % 0x20 == 0 or pid in (0x01, 0x41):
            continue  # bitmask / monitor PIDs belong to vehicle_info
        try:
            per_ecu = await v.pid(pid)
        except _UNAVAILABLE:
            continue
        info = PIDS.get(pid)
        for ecu, data in per_ecu.items():
            rows.append({
                "pid": pid,
                "name": info.name if info else "(undecoded)",
                "display": format_value(info.decode(data), info.unit, imperial) if info else data.hex(" ").upper(),
                "ecu": ecu_name(ecu) if len(per_ecu) > 1 else None,
            })
    return rows


async def trouble_codes(v: Vehicle, imperial: bool) -> dict:
    """Stored, pending and permanent codes plus freeze frame. ``permanent`` is None when unsupported."""
    result: dict = {}
    for key, mode in (("stored", 0x03), ("pending", 0x07), ("permanent", 0x0A)):
        codes = await v.dtcs(mode)
        if mode == 0x0A and not codes:
            result[key] = None
            continue
        result[key] = [
            {"code": code, "description": describe(code), "ecu": ecu_name(ecu)}
            for ecu, ecu_codes in codes.items()
            for code in ecu_codes
        ]
    result["freeze_frame"] = await freeze_frame(v, imperial)
    return result


async def freeze_frame(v: Vehicle, imperial: bool) -> dict | None:
    try:
        trigger = primary(await v.freeze_frame_pid(0x02))
    except _UNAVAILABLE:
        return None
    if not any(trigger):
        return None
    readings = []
    for pid in sorted(await v.freeze_frame_supported()):
        info = PIDS.get(pid)
        if not info:
            continue
        try:
            data = primary(await v.freeze_frame_pid(pid))
        except _UNAVAILABLE:
            continue
        readings.append({"pid": pid, "name": info.name, "display": format_value(info.decode(data), info.unit, imperial)})
    return {"trigger": decode_dtc(trigger[0], trigger[1]), "readings": readings}
