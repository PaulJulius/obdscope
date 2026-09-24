"""The simulated adapter and vehicles, driven through the real ELM327/OBD layers."""

import pytest

from obdscope import reports
from obdscope.cli import main
from obdscope.elm327 import Elm327, ElmError, NoData
from obdscope.obd import NegativeResponse, Vehicle, primary
from obdscope.pids import PIDS, decode_monitor_status
from obdscope.simulator import PROFILES, SimulatedTransport

# Plausible ranges for decoded values (metric units), used to sanity-check every sample.
RANGES = {
    0x04: (0, 100), 0x05: (15, 100), 0x06: (-10, 10), 0x07: (-5, 20), 0x08: (-10, 10), 0x09: (-5, 20),
    0x0B: (20, 101), 0x0C: (500, 4500), 0x0D: (0, 110), 0x0E: (0, 40), 0x0F: (15, 40), 0x10: (1, 130),
    0x11: (10, 80), 0x14: (0, 1), 0x15: (0, 1), 0x18: (0, 1), 0x19: (0, 1), 0x1F: (0, 65535),
    0x21: (0, 65535), 0x2E: (0, 100), 0x2F: (5, 100), 0x30: (0, 255), 0x31: (0, 65535), 0x33: (95, 105),
    0x34: (0.9, 2.0), 0x3C: (100, 900), 0x42: (13.5, 15), 0x43: (0, 110), 0x44: (0.9, 1.1), 0x45: (0, 80),
    0x46: (15, 25), 0x47: (10, 90), 0x49: (10, 80), 0x4A: (10, 50), 0x4C: (0, 80), 0x4D: (0, 0),
    0x4E: (0, 65535),
}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


async def open_vehicle(profile: str, protocol: str = "0") -> tuple[Vehicle, SimulatedTransport, Clock]:
    clock = Clock()
    transport = SimulatedTransport(profile, clock=clock, seed=1)
    elm = Elm327(transport, protocol=protocol)
    await elm.initialize()
    return Vehicle(elm), transport, clock


async def value(v: Vehicle, pid: int):
    return PIDS[pid].decode(primary(await v.pid(pid)))


@pytest.mark.parametrize("key, protocol", [("expedition", "1"), ("rav4", "6")])
async def test_initializes_with_auto_protocol(key, protocol):
    v, transport, _ = await open_vehicle(key)
    assert v.elm.protocol == protocol
    assert v.elm.version == "ELM327 v2.2"
    assert await v.elm.voltage() in ("14.1V", "14.2V", "14.3V")


@pytest.mark.parametrize("key", ["expedition", "rav4"])
async def test_supported_pids_match_profile(key):
    v, _, _ = await open_vehicle(key)
    profile = PROFILES[key]
    expected = set().union(*profile.ecus.values())
    supported = await v.supported_pids()
    assert expected <= supported
    assert {p for p in supported if p % 0x20} == expected  # only bitmask PIDs added


@pytest.mark.parametrize("key", ["expedition", "rav4"])
async def test_every_pid_stays_plausible_through_the_drive_cycle(key):
    v, _, clock = await open_vehicle(key)
    pids = [p for p in await v.supported_pids() if p in RANGES]
    for t in range(0, 480, 7):
        clock.t = t
        for pid in pids:
            low, high = RANGES[pid]
            got = await value(v, pid)
            assert low <= got <= high, f"{key} PID {pid:02X} = {got} at t={t}"


async def test_drive_cycle_physics():
    v, _, clock = await open_vehicle("expedition")
    clock.t = 5
    assert await value(v, 0x0D) == 0
    cold = await value(v, 0x05)
    assert "open loop (engine cold)" in await value(v, 0x03)

    clock.t = 600 + 50  # warm, cruising at city speed
    assert 65 <= await value(v, 0x0D) <= 80
    assert await value(v, 0x05) > cold + 50
    assert "closed loop" in await value(v, 0x03)
    cruise_ltft = await value(v, 0x07)

    clock.t = 600 + 10  # warm idle
    assert 600 <= await value(v, 0x0C) <= 700
    idle_ltft = await value(v, 0x07)
    assert idle_ltft > 10 and idle_ltft > cruise_ltft + 5  # vacuum leak signature


async def test_expedition_codes_clear_and_come_back():
    v, transport, clock = await open_vehicle("expedition")
    assert await v.dtcs(0x03) == {"10": ["P0171", "P0174"]}
    assert await v.dtcs(0x0A) == {}  # 2001: no permanent codes service
    assert decode_monitor_status(primary(await v.pid(0x01))).mil_on
    freeze = await reports.freeze_frame(v, imperial=False)
    assert freeze["trigger"] == "P0171"
    assert {r["pid"] for r in freeze["readings"]} >= {0x05, 0x07, 0x0C}

    clock.t = 30
    await v.clear_dtcs()
    assert await v.dtcs(0x03) == {"10": []}
    status = decode_monitor_status(primary(await v.pid(0x01)))
    assert not status.mil_on and status.dtc_count == 0
    assert not any(complete for name, complete in status.monitors if name == "Catalyst")
    assert await reports.freeze_frame(v, imperial=False) is None
    assert await v.dtcs(0x07) == {"10": []}

    clock.t = 30 + 61  # the leak is still there
    assert await v.dtcs(0x07) == {"10": ["P0171"]}


async def test_expedition_reports_vin():
    v, _, _ = await open_vehicle("expedition")
    assert await v.vin() == "1FMSIMEXP01LA0001"


async def test_rav4_vin_codes_and_two_ecus():
    v, _, _ = await open_vehicle("rav4")
    assert await v.vin() == "2T3SIMRAV4KW00001"
    assert await v.dtcs(0x03) == {"7E8": [], "7E9": []}
    assert await v.dtcs(0x07) == {"7E8": ["P0456"], "7E9": []}
    assert await v.dtcs(0x0A) == {"7E8": [], "7E9": []}
    assert set(await v.pid(0x0D)) == {"7E8", "7E9"}
    assert await reports.freeze_frame(v, imperial=False) is None


async def test_wrong_protocol_fails_like_real_adapter():
    with pytest.raises(ElmError, match="UNABLE TO CONNECT"):
        await open_vehicle("rav4", protocol="1")
    v, transport, _ = await open_vehicle("expedition", protocol="1")
    assert v.elm.protocol == "1"


async def test_enhanced_data_needs_physical_header():
    v, _, _ = await open_vehicle("rav4")
    with pytest.raises(NoData):
        await v.enhanced(0x1001)
    await v.elm.command("ATSH7E0")
    rpm = int.from_bytes((await v.enhanced(0x1001))["7E8"], "big")
    assert 500 < rpm < 4500
    with pytest.raises(NegativeResponse) as exc:
        await v.enhanced(0x1002)
    assert exc.value.code == 0x31
    await v.elm.command("ATSH7E1")
    assert set(await v.enhanced(0x1021)) == {"7E9"}

    ford, _, _ = await open_vehicle("expedition")
    await ford.elm.command("ATSHC410F1")
    assert set(await ford.enhanced(0x1105)) == {"10"}


async def test_raw_adapter_formatting():
    transport = SimulatedTransport("rav4", clock=Clock(), seed=1)
    assert await transport.send("ATZ", 5) == "ATZ\r\r\rELM327 v2.2\r\r"
    assert await transport.send("ATE0", 5) == "ATE0\rOK\r\r"  # ATE0 itself is echoed
    assert await transport.send("ATBOGUS", 5) == "?\r\r"
    assert (await transport.send("0902", 5)).startswith("SEARCHING...\r014\r0: 49 02 01 32 54 33\r1: ")
    await transport.send("ATH1", 5)
    await transport.send("ATS0", 5)
    assert (await transport.send("010D", 5)).startswith("7E803410D")

    ford = SimulatedTransport("expedition", clock=Clock(), seed=1)
    await ford.send("ATH1", 5)
    line = (await ford.send("0105", 5)).split("\r")[2]
    assert line.startswith("41 6B 10 41 05 ")  # header, data, J1850 CRC
    assert len(line.split()) == 7


def test_cli_against_simulators(capsys, tmp_path):
    assert main(["pids", "--simulate", "expedition"]) == 0
    out = capsys.readouterr().out
    assert "Long term fuel trim, bank 2" in out and "O2 sensor B2S1 voltage" in out

    csv_path = tmp_path / "drive.csv"
    assert main(["--simulate", "rav4", "live", "rpm", "cat1", "--count", "2", "--interval", "0", "--csv", str(csv_path)]) == 0
    assert "Catalyst temperature B1S1" in csv_path.read_text().splitlines()[0]
    assert len(csv_path.read_text().splitlines()) == 3

    assert main(["--simulate", "rav4", "probe", "1000", "1030"]) == 0
    probe = capsys.readouterr().out
    assert "1001  ECU 7E8 (engine)" in probe and "1028" in probe

    assert main(["--simulate", "expedition", "clear-dtc", "--yes"]) == 0
    assert "Codes cleared." in capsys.readouterr().out

    assert main(["--simulate", "rav4", "--protocol", "1", "info"]) == 1
    assert "UNABLE TO CONNECT" in capsys.readouterr().err


def test_trim_monitor(capsys):
    assert main(["--simulate", "expedition", "trims", "--count", "10", "--interval", "0", "--baseline", "3"]) == 0
    out = capsys.readouterr().out
    assert "bank 1" in out and "bank 2" in out
    assert "measuring baseline" in out and "change" in out
    assert "no change yet" in out or "slight drop" in out

    # A 4-cylinder reports bank 1 only.
    assert main(["--simulate", "rav4", "trims", "--count", "5", "--interval", "0", "--baseline", "2"]) == 0
    rav4 = capsys.readouterr().out
    assert "bank 1" in rav4 and "bank 2" not in rav4


def test_sniff_lists_modules(capsys):
    assert main(["--simulate", "expedition", "sniff", "--seconds", "2"]) == 0
    out = capsys.readouterr().out
    assert "frames from 4 address(es)" in out
    for source in ("10", "40", "28", "60"):  # powertrain plus three other modules
        assert f" {source}     " in out

    # The RAV4 profile has no chatter defined, so there is nothing to report.
    assert main(["--simulate", "rav4", "sniff", "--seconds", "1"]) == 0
    assert "No traffic seen" in capsys.readouterr().err


def test_modules_sweep_reads_non_powertrain_codes(capsys):
    assert main(["--simulate", "expedition", "modules"]) == 0
    out = capsys.readouterr().out
    assert "module 10" in out and "P0171" in out          # powertrain, via mode 13
    assert "module 60" in out and "96 00 52 84 00 00" in out
    assert "B1600  Ford: PATS" in out                      # a body code, decoded
    assert "C1284" in out
    assert "module 28" in out and "no codes stored" in out
