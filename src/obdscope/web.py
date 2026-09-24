"""Local web UI: ``veepeak ui``.

One process owns the BLE connection. A background task polls the selected
gauge PIDs and pushes samples to every open browser tab over Server-Sent
Events. Everything else (codes, vehicle info) is a JSON request. An asyncio
lock serializes adapter access, since the ELM327 handles one command at a time.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import time
import webbrowser
from pathlib import Path

from aiohttp import web

from . import reports
from .elm327 import Elm327, ElmError, NoData, Transport
from .obd import NegativeResponse, Vehicle, primary
from .pids import DEFAULT_LIVE, PIDS, convert, display, parse_pid
from .simulator import PROFILES
from .transport import TransportError, make_transport

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
RECORDINGS = Path("recordings")
SOURCES = [{"id": "ble", "label": "OBDCheck BLE adapter"}] + [
    {"id": key, "label": f"Simulated {profile.label}"} for key, profile in PROFILES.items()
]
VOLTAGE_EVERY = 10.0  # seconds between battery voltage refreshes
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")


class NotConnected(Exception):
    pass


class Dashboard:
    def __init__(self, args):
        self.args = args
        self.imperial: bool = args.imperial
        self.interval = 1.0
        self.state = "disconnected"  # disconnected | connecting | connected | error
        self.message = ""
        self.transport: Transport | None = None
        self.vehicle: Vehicle | None = None
        self.summary: dict | None = None
        self.voltage: str | None = None
        self.supported: list[int] = []
        self.gauges: list[int] = []
        self.lock = asyncio.Lock()
        self.clients: set[asyncio.Queue] = set()
        self.poller: asyncio.Task | None = None
        self.recording: tuple[Path, object, csv.writer] | None = None
        self._recorded: list[int] = []
        self.source = args.simulate or "ble"  # what the Connect button uses by default

    # --- Event stream ---

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        queue.put_nowait(self.state_event())
        self.clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.clients.discard(queue)

    def broadcast(self, event: dict | None) -> None:
        for queue in self.clients:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a stalled tab just misses samples

    def state_event(self) -> dict:
        return {
            "type": "state",
            "state": self.state,
            "message": self.message,
            "vehicle": self.summary,
            "voltage": self.voltage,
            "gauges": self.gauges,
            "available": [
                {"pid": p, "name": PIDS[p].name, "unit": convert(0.0, PIDS[p].unit, self.imperial)[1]}
                for p in self.supported
                if p in PIDS
            ],
            "imperial": self.imperial,
            "interval": self.interval,
            "recording": str(self.recording[0]) if self.recording else None,
            "sources": SOURCES,
            "source": self.source,
        }

    def _set_state(self, state: str, message: str = "") -> None:
        self.state, self.message = state, message
        self.broadcast(self.state_event())

    # --- Connection lifecycle ---

    async def connect(self, source: str | None = None) -> None:
        if self.state in ("connecting", "connected"):
            return
        self.source = source or self.source
        simulate = None if self.source == "ble" else self.source
        self._set_state("connecting", "Starting simulator…" if simulate else "Searching for adapter…")
        transport = make_transport(simulate, address=self.args.address, name=self.args.name)
        try:
            await transport.connect()
            self._set_state("connecting", f"Connected to {transport.device.name or 'adapter'}. Talking to vehicle…")
            elm = Elm327(transport, protocol=self.args.protocol)
            await elm.initialize()
            vehicle = Vehicle(elm)
            supported = await vehicle.supported_pids()
            voltage = await elm.voltage()
        except Exception as e:  # BLE stacks raise a variety of types; report them all in the UI
            log.exception("connect failed")
            await transport.close()
            self._set_state("error", str(e) or type(e).__name__)
            return
        self.transport, self.vehicle, self.voltage = transport, vehicle, voltage
        self.supported = sorted(supported)
        self.summary = {
            "device": transport.device.name or transport.device.address,
            "adapter": elm.version,
            "protocol": elm.protocol,
            "protocol_name": elm.protocol_name,
            "simulated": bool(simulate),
        }
        self.gauges = [p for p in map(parse_pid, DEFAULT_LIVE) if p in supported and p in PIDS]
        self.poller = asyncio.create_task(self._poll())
        self._set_state("connected")

    async def disconnect(self, state: str = "disconnected", message: str = "") -> None:
        poller, self.poller = self.poller, None
        if poller and poller is not asyncio.current_task():
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)
        self._stop_recording()
        if self.transport:
            try:
                await self.transport.close()
            except Exception:
                log.exception("error closing transport")
        self.transport = self.vehicle = self.summary = self.voltage = None
        self.supported, self.gauges = [], []
        self._set_state(state, message)

    async def shutdown(self, _app=None) -> None:
        await self.disconnect()
        self.broadcast(None)  # ends every open event stream

    # --- Polling ---

    async def _poll(self) -> None:
        last_voltage = time.monotonic()
        try:
            while True:
                started = time.monotonic()
                if self.gauges:
                    await self._sample()
                if started - last_voltage >= VOLTAGE_EVERY:
                    async with self.lock:
                        self.voltage = await self.vehicle.elm.voltage()
                    last_voltage = started
                    self.broadcast(self.state_event())
                await asyncio.sleep(max(0.05, self.interval - (time.monotonic() - started)))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("polling failed")
            await self.disconnect("error", f"Lost connection: {e or type(e).__name__}")

    async def _sample(self) -> None:
        values, raw = {}, {}
        for pid in list(self.gauges):
            info = PIDS[pid]
            try:
                async with self.lock:
                    value = info.decode(primary(await self.vehicle.pid(pid)))
            except (NoData, NegativeResponse):
                value = None
            except ElmError as e:  # transient bus errors: show a gap rather than drop the connection
                log.warning("PID %02X: %s", pid, e)
                value = None
            raw[pid] = value
            if value is None:
                values[pid] = None
            else:
                text, unit = display(value, info.unit, self.imperial)
                number = convert(value, info.unit, self.imperial)[0]
                values[pid] = {"text": text, "unit": unit, "num": number if isinstance(number, (int, float)) else None}
        self.broadcast({"type": "sample", "t": time.time(), "values": values})
        if self.recording:
            self.recording[2].writerow([f"{time.time():.3f}"] + ["" if raw.get(p) is None else raw[p] for p in self._recorded])

    # --- Settings / recording ---

    def set_gauges(self, pids: list[int]) -> None:
        if self.recording:
            raise ValueError("stop recording before changing gauges")
        unknown = [f"{p:02X}" for p in pids if p not in PIDS or p not in self.supported]
        if unknown:
            raise ValueError(f"unsupported PID(s): {' '.join(unknown)}")
        self.gauges = list(dict.fromkeys(pids))
        self.broadcast(self.state_event())

    def update_settings(self, imperial: bool | None, interval: float | None) -> None:
        if imperial is not None:
            self.imperial = bool(imperial)
        if interval is not None:
            self.interval = min(10.0, max(0.5, float(interval)))
        self.broadcast(self.state_event())

    def start_recording(self) -> None:
        if self.recording:
            return
        if not self.gauges:
            raise ValueError("add at least one gauge first")
        RECORDINGS.mkdir(exist_ok=True)
        path = RECORDINGS / time.strftime("%Y%m%d-%H%M%S.csv")
        f = open(path, "w", newline="")
        writer = csv.writer(f)
        self._recorded = list(self.gauges)
        writer.writerow(["timestamp"] + [f"{PIDS[p].name} ({PIDS[p].unit})" for p in self._recorded])
        self.recording = (path, f, writer)
        self.broadcast(self.state_event())

    def _stop_recording(self) -> None:
        if self.recording:
            self.recording[1].close()
            self.recording = None
            self.broadcast(self.state_event())

    # --- Guarded vehicle access for JSON endpoints ---

    async def run_report(self, fn):
        if not self.vehicle:
            raise NotConnected()
        async with self.lock:
            return await fn(self.vehicle)


# --- HTTP layer ---

DASHBOARD = web.AppKey("dashboard", Dashboard)
LOOPBACK = web.AppKey("loopback", bool)


def _dashboard(request: web.Request) -> Dashboard:
    return request.app[DASHBOARD]


@web.middleware
async def guard(request: web.Request, handler):
    # Reject cross-site requests: a page on another origin can't send a JSON POST
    # without a CORS preflight (which we never approve), and a DNS-rebinding page
    # can't pass the Host check when we're bound to loopback.
    if request.app[LOOPBACK] and request.host.rsplit(":", 1)[0] not in LOOPBACK_HOSTS:
        return web.json_response({"error": "forbidden host"}, status=403)
    if request.method == "POST" and request.content_type != "application/json":
        return web.json_response({"error": "expected application/json"}, status=415)
    try:
        return await handler(request)
    except NotConnected:
        return web.json_response({"error": "not connected to a vehicle"}, status=409)
    except (ValueError, TypeError, KeyError) as e:
        return web.json_response({"error": str(e)}, status=400)
    except (ElmError, TransportError) as e:
        return web.json_response({"error": str(e)}, status=502)


async def index(_request):
    # no-cache: browsers revalidate, so an upgraded page is picked up immediately
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def events(request):
    d = _dashboard(request)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await response.prepare(request)
    queue = d.subscribe()
    try:
        while (event := await queue.get()) is not None:
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
    except ConnectionResetError:
        pass
    finally:
        d.unsubscribe(queue)
    return response


async def connect(request):
    source = (await request.json()).get("source")
    if source is not None and source not in {s["id"] for s in SOURCES}:
        raise ValueError(f"unknown source {source!r}")
    asyncio.create_task(_dashboard(request).connect(source))
    return web.json_response({"ok": True}, status=202)


async def disconnect(request):
    await _dashboard(request).disconnect()
    return web.json_response({"ok": True})


async def gauges(request):
    body = await request.json()
    _dashboard(request).set_gauges([int(p) for p in body["pids"]])
    return web.json_response({"ok": True})


async def settings(request):
    body = await request.json()
    _dashboard(request).update_settings(body.get("imperial"), body.get("interval"))
    return web.json_response({"ok": True})


async def record(request):
    d = _dashboard(request)
    if (await request.json()).get("on"):
        d.start_recording()
    else:
        d._stop_recording()
    return web.json_response({"recording": str(d.recording[0]) if d.recording else None})


async def info(request):
    return web.json_response(await _dashboard(request).run_report(reports.vehicle_info))


async def readings(request):
    d = _dashboard(request)
    return web.json_response(await d.run_report(lambda v: reports.all_readings(v, d.imperial)))


async def dtc(request):
    d = _dashboard(request)
    return web.json_response(await d.run_report(lambda v: reports.trouble_codes(v, d.imperial)))


async def clear_dtc(request):
    if (await request.json()).get("confirm") != "clear":
        raise ValueError('send {"confirm": "clear"} to clear codes')
    await _dashboard(request).run_report(lambda v: v.clear_dtcs())
    return web.json_response({"ok": True})


def build_app(args) -> web.Application:
    app = web.Application(middlewares=[guard])
    app[DASHBOARD] = Dashboard(args)
    app[LOOPBACK] = args.host in ("127.0.0.1", "localhost", "::1")
    app.router.add_get("/", index)
    app.router.add_get("/api/events", events)
    app.router.add_post("/api/connect", connect)
    app.router.add_post("/api/disconnect", disconnect)
    app.router.add_post("/api/gauges", gauges)
    app.router.add_post("/api/settings", settings)
    app.router.add_post("/api/record", record)
    app.router.add_get("/api/info", info)
    app.router.add_get("/api/readings", readings)
    app.router.add_get("/api/dtc", dtc)
    app.router.add_post("/api/clear-dtc", clear_dtc)
    app.on_shutdown.append(app[DASHBOARD].shutdown)
    return app


async def serve(args) -> None:
    app = build_app(args)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()
    url = f"http://{'localhost' if app[LOOPBACK] else args.host}:{args.port}/"
    print(f"veepeak UI running at {url}  (Ctrl-C to stop)", flush=True)
    if not app[LOOPBACK]:
        print("warning: the UI is reachable from your network with no password, including 'clear codes'.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        # Close event streams first so aiohttp's graceful shutdown doesn't wait on them.
        await app[DASHBOARD].shutdown()
        await runner.cleanup()
