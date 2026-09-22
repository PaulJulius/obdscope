"""Tests using a scripted fake adapter. Responses mimic real ELM327 output with headers on."""

import pytest

from veepeak_reader.dtc import decode_dtcs
from veepeak_reader.elm327 import Elm327, ElmError, NoData, parse_response
from veepeak_reader.obd import NegativeResponse, Vehicle, ecu_name, primary
from veepeak_reader.pids import PIDS, decode_monitor_status, decode_supported, format_value, parse_pid


class FakeTransport:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.sent: list[str] = []

    async def send(self, command: str, timeout: float) -> str:
        self.sent.append(command)
        return self.responses.get(command, "NO DATA") + "\r\r"

    async def close(self) -> None:
        pass


PWM_INIT = {
    "ATZ": "ATZ\r\r\rELM327 v2.2",
    "ATE0": "ATE0\rOK",
    "ATL0": "OK",
    "ATS1": "OK",
    "ATH1": "OK",
    "ATSP0": "OK",
    "0100": "SEARCHING...\r41 6B 10 41 00 BE 3E B8 11 C9",
    "ATDPN": "A1",
}


# 2019 RAV4-style CAN car: engine (7E8) and transmission (7E9) both answer 0100.
CAN_INIT = {
    **PWM_INIT,
    "0100": "7E9 06 41 00 98 18 80 11\r7E8 06 41 00 BE 3F A8 13",
    "ATDPN": "A6",
}


async def make_vehicle(responses: dict[str, str], init=PWM_INIT) -> tuple[Vehicle, FakeTransport]:
    transport = FakeTransport({**init, **responses})
    elm = Elm327(transport)
    await elm.initialize()
    return Vehicle(elm), transport


async def test_initialize_detects_j1850_pwm():
    v, transport = await make_vehicle({})
    assert v.elm.version == "ELM327 v2.2"
    assert v.elm.protocol == "1"
    assert not v.elm.is_can
    assert transport.sent[:6] == ["ATZ", "ATE0", "ATL0", "ATS1", "ATH1", "ATSP0"]


async def test_initialize_reports_bus_failure():
    transport = FakeTransport({**PWM_INIT, "0100": "SEARCHING...\rUNABLE TO CONNECT"})
    with pytest.raises(ElmError, match="UNABLE TO CONNECT"):
        await Elm327(transport).initialize()


def test_parse_legacy_strips_header_and_checksum():
    assert parse_response(["41 6B 10 41 0C 1A F8 E5"], "1") == {"10": [bytes.fromhex("410C1AF8")]}


def test_parse_can_single_and_multi_frame():
    lines = ["7E8 10 14 49 02 01 31 46 4D", "7E8 21 50 55 31 36 4C 34 31", "7E8 22 4C 41 31 32 33 34 35"]
    [msg] = parse_response(lines, "6")["7E8"]
    assert msg[:3] == bytes.fromhex("490201") and msg[3:] == b"1FMPU16L41LA12345"
    assert parse_response(["18 DA F1 10 03 41 0D 32"], "7") == {"10": [bytes.fromhex("410D32")]}


async def test_supported_pids_stops_when_next_range_unanswered():
    v, _ = await make_vehicle({})
    supported = await v.supported_pids()
    assert {0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0C, 0x1C, 0x20} <= supported
    assert 0x02 not in supported


async def test_rpm_decode():
    v, _ = await make_vehicle({"010C": "41 6B 10 41 0C 1A F8 E5"})
    [data] = (await v.pid(0x0C)).values()
    assert PIDS[0x0C].decode(data) == 1726.0


async def test_stored_dtcs_j1850():
    v, _ = await make_vehicle({"03": "48 6B 10 43 01 71 01 74 00 00 A1"})
    assert await v.dtcs() == {"10": ["P0171", "P0174"]}


async def test_no_dtcs_when_no_data():
    v, _ = await make_vehicle({})
    assert await v.dtcs(0x07) == {}


async def test_stored_dtcs_can_skips_count_byte():
    v, _ = await make_vehicle({"03": "7E8 06 43 02 01 71 01 74"}, init=CAN_INIT)
    assert await v.dtcs() == {"7E8": ["P0171", "P0174"]}


async def test_can_multi_ecu_prefers_engine():
    v, _ = await make_vehicle({"010D": "7E9 03 41 0D 40\r7E8 03 41 0D 41"}, init=CAN_INIT)
    assert v.elm.protocol == "6" and v.elm.is_can
    per_ecu = await v.pid(0x0D)
    assert set(per_ecu) == {"7E8", "7E9"}
    assert primary(per_ecu) == bytes([0x41])
    assert ecu_name("7E9") == "7E9 (transmission)" and ecu_name("7EB") == "7EB"


async def test_can_supported_pids_union_across_ecus():
    v, _ = await make_vehicle({}, init=CAN_INIT)
    supported = await v.supported_pids()
    assert {0x01, 0x0C, 0x1C, 0x20} <= supported


async def test_permanent_dtcs_can():
    v, _ = await make_vehicle({"0A": "7E8 04 4A 01 04 20\r7E9 02 4A 00"}, init=CAN_INIT)
    assert await v.dtcs(0x0A) == {"7E8": ["P0420"], "7E9": []}


async def test_vin_can_multi_frame():
    lines = ["7E8 10 14 49 02 01 32 54 33", "7E8 21 57 46 52 45 56 37 4B", "7E8 22 57 31 32 33 34 35 36"]
    v, _ = await make_vehicle({"0902": "\r".join(lines)}, init=CAN_INIT)
    assert await v.vin() == "2T3WFREV7KW123456"


async def test_vin_j1850_multi_message():
    lines = [
        "49 6B 10 49 02 01 00 00 00 31 00",
        "49 6B 10 49 02 02 46 4D 50 55 00",
        "49 6B 10 49 02 03 31 36 4C 34 00",
        "49 6B 10 49 02 04 31 4C 41 31 00",
        "49 6B 10 49 02 05 32 33 34 35 00",
    ]
    v, _ = await make_vehicle({"0902": "\r".join(lines)})
    assert await v.vin() == "1FMPU16L41LA12345"


async def test_vin_missing_on_older_vehicle():
    v, _ = await make_vehicle({})
    assert await v.vin() is None


async def test_negative_response():
    v, _ = await make_vehicle({"221172": "7F 6B 10 7F 22 31 00"})
    with pytest.raises(NegativeResponse) as exc:
        await v.enhanced(0x1172)
    assert exc.value.code == 0x31


async def test_enhanced_positive_response():
    v, _ = await make_vehicle({"221172": "C4 F1 10 62 11 72 0A 3F 00"})
    assert await v.enhanced(0x1172) == {"10": bytes.fromhex("0A3F")}


async def test_freeze_frame():
    v, _ = await make_vehicle({"020200": "42 6B 10 42 02 00 01 71 00", "020C00": "42 6B 10 42 0C 00 0B B8 00"})
    assert await v.freeze_frame_pid(0x02) == {"10": bytes.fromhex("0171")}
    [rpm] = (await v.freeze_frame_pid(0x0C)).values()
    assert PIDS[0x0C].decode(rpm) == 750.0


async def test_no_data_raises():
    v, _ = await make_vehicle({})
    with pytest.raises(NoData):
        await v.pid(0x0D)


def test_decode_dtcs_all_systems():
    assert decode_dtcs(bytes.fromhex("0300 4123 8456 C789 0000")) == ["P0300", "C0123", "B0456", "U0789"]


def test_decode_supported():
    assert decode_supported(0x00, bytes.fromhex("80000001")) == {0x01, 0x20}


def test_monitor_status():
    ms = decode_monitor_status(bytes.fromhex("81076504"))
    assert ms.mil_on and ms.dtc_count == 1 and not ms.diesel
    assert dict(ms.monitors) == {
        "Misfire": True, "Fuel system": True, "Comprehensive components": True,
        "Catalyst": True, "Evaporative system": False, "Oxygen sensor": True, "Oxygen sensor heater": True,
    }


def test_modern_pid_decoders():
    assert PIDS[0x34].decode(bytes.fromhex("80008000")) == 1.0  # wideband lambda
    assert PIDS[0x3C].decode(bytes.fromhex("1F40")) == 760.0  # catalyst temp, °C
    assert PIDS[0xA6].decode((123456).to_bytes(4, "big")) == 12345.6  # odometer, km
    assert PIDS[0x62].decode(bytes([0x96])) == 25  # actual torque, %


def test_units_and_aliases():
    assert format_value(90, "°C", imperial=True) == "194.0 °F"
    assert format_value(100, "km/h", imperial=False) == "100 km/h"
    assert format_value(0.45, "V", imperial=True) == "0.450 V"
    assert parse_pid("rpm") == 0x0C and parse_pid("0x0d") == 0x0D and parse_pid("2F") == 0x2F
    with pytest.raises(ValueError):
        parse_pid("bogus")
