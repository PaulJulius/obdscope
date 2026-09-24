"""Web UI tests: the aiohttp app driven end to end against a fake RAV4-style adapter."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from obdscope import web
from obdscope.transport import make_transport

from fakes import CAN_INIT, FakeBleTransport

RESPONSES = {
    **CAN_INIT,
    "ATRV": "12.6V",
    "0104": "7E8 03 41 04 40",
    "0105": "7E8 03 41 05 82",
    "0106": "7E8 03 41 06 82",
    "0107": "7E8 03 41 07 84",
    "010C": "7E8 04 41 0C 1A F8",
    "010D": "7E8 03 41 0D 36",
    "0111": "7E8 03 41 11 33",
    "03": "7E8 04 43 01 01 71",
    "07": "7E8 02 47 00",
    "0A": "7E8 02 4A 00",
    "04": "7E8 01 44",
}


@pytest.fixture
async def client(monkeypatch, tmp_path):
    transports = []

    def fake_or_simulated(simulate, **kwargs):
        # The "BLE adapter" is a scripted fake; simulated vehicles are the real simulator.
        transports.append(make_transport(simulate, **kwargs) if simulate else FakeBleTransport(RESPONSES))
        return transports[-1]

    monkeypatch.setattr(web, "make_transport", fake_or_simulated)
    monkeypatch.setattr(web, "RECORDINGS", tmp_path / "recordings")
    args = SimpleNamespace(address=None, name=None, protocol="0", imperial=True, host="127.0.0.1", simulate=None)
    app = web.build_app(args)
    async with TestClient(TestServer(app)) as c:
        c.dashboard = app[web.DASHBOARD]
        c.transports = transports
        yield c


async def connect(client, source: str | None = None) -> None:
    resp = await client.post("/api/connect", json={"source": source} if source else {})
    assert resp.status == 202
    for _ in range(100):
        if client.dashboard.state == "connected":
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never connected: {client.dashboard.state} {client.dashboard.message}")


async def test_serves_page(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert "<title>veepeak</title>" in await resp.text()


async def test_connect_picks_supported_default_gauges(client):
    await connect(client)
    d = client.dashboard
    assert d.summary["protocol"] == "6"
    assert d.voltage == "12.6V"
    # Bank 2 trims aren't supported by the fake 4-cylinder, so they're left out.
    assert d.gauges == [0x0C, 0x0D, 0x05, 0x04, 0x11, 0x06, 0x07]


async def test_event_stream_sends_state_then_samples(client):
    await connect(client)
    client.dashboard.update_settings(imperial=None, interval=0.2)
    resp = await client.get("/api/events")
    first = json.loads((await resp.content.readline()).decode().removeprefix("data: "))
    assert first["type"] == "state" and first["state"] == "connected"
    while True:
        line = (await resp.content.readline()).decode()
        if line.startswith("data: ") and '"sample"' in line:
            sample = json.loads(line.removeprefix("data: "))
            break
    assert sample["values"][str(0x0C)] == {"text": "1726", "unit": "rpm", "num": 1726.0}
    assert sample["values"][str(0x05)]["unit"] == "°F"
    resp.close()


async def test_trouble_codes_and_clear(client):
    await connect(client)
    report = await (await client.get("/api/dtc")).json()
    assert [c["code"] for c in report["stored"]] == ["P0171"]
    assert report["stored"][0]["ecu"] == "7E8 (engine)"
    assert report["pending"] == [] and report["permanent"] == []

    resp = await client.post("/api/clear-dtc", json={})
    assert resp.status == 400
    resp = await client.post("/api/clear-dtc", json={"confirm": "clear"})
    assert resp.status == 200
    assert "04" in client.transports[0].sent


async def test_reports_require_connection(client):
    resp = await client.get("/api/dtc")
    assert resp.status == 409


async def test_rejects_cross_site_requests(client):
    resp = await client.post("/api/connect", data="{}", headers={"Content-Type": "text/plain"})
    assert resp.status == 415
    resp = await client.get("/api/info", headers={"Host": "evil.example:8765"})
    assert resp.status == 403


async def test_gauge_validation_and_recording(client):
    await connect(client)
    resp = await client.post("/api/gauges", json={"pids": [0x0C, 0xA6]})
    assert resp.status == 400  # odometer isn't supported by this fake car

    resp = await client.post("/api/gauges", json={"pids": [0x0C, 0x0D]})
    assert resp.status == 200 and client.dashboard.gauges == [0x0C, 0x0D]

    resp = await client.post("/api/record", json={"on": True})
    path = (await resp.json())["recording"]
    assert path
    assert (await client.post("/api/gauges", json={"pids": [0x0C]})).status == 400  # locked while recording
    await client.dashboard._sample()
    await client.post("/api/record", json={"on": False})
    lines = open(path).read().splitlines()
    assert lines[0] == "timestamp,Engine RPM (rpm),Vehicle speed (km/h)"
    assert lines[1].split(",")[1:] == ["1726.0", "54"]


async def test_disconnect_closes_transport(client):
    await connect(client)
    await client.post("/api/disconnect", json={})
    assert client.dashboard.state == "disconnected"
    assert client.transports[0].closed


async def test_serve_starts_and_shuts_down(capsys):
    args = SimpleNamespace(
        address=None, name=None, protocol="0", imperial=True, host="127.0.0.1", port=0, no_browser=True, simulate=None,
    )
    task = asyncio.create_task(web.serve(args))
    await asyncio.sleep(0.2)
    assert not task.done(), task.exception() if task.done() else None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "veepeak UI running at http://localhost:0/" in capsys.readouterr().out


async def test_connect_to_simulated_vehicles(client):
    state = client.dashboard.state_event()
    assert [s["id"] for s in state["sources"]] == ["ble", "rav4", "expedition"]
    assert state["source"] == "ble"

    await connect(client, "expedition")
    d = client.dashboard
    assert d.summary["simulated"] and d.summary["protocol"] == "1"
    assert d.summary["device"] == "Simulated 2001 Ford Expedition XLT"
    report = await (await client.get("/api/dtc")).json()
    assert [c["code"] for c in report["stored"]] == ["P0171", "P0174"]
    assert report["freeze_frame"]["trigger"] == "P0171"

    await client.post("/api/disconnect", json={})
    await connect(client, "rav4")
    assert client.dashboard.summary["protocol"] == "6"
    assert client.dashboard.source == "rav4"


async def test_rejects_unknown_source(client):
    resp = await client.post("/api/connect", json={"source": "delorean"})
    assert resp.status == 400
