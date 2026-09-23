"""Bluetooth Low Energy transport for ELM327-style adapters such as the OBDCheck BLE.

The adapter exposes a UART-like GATT service: commands are written to one
characteristic and responses arrive as notifications on another. Each ELM327
response ends with a ``>`` prompt, which is how we know a reply is complete.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

# Most ELM327 BLE clones (Veepeak included) use the FFF0 service with
# FFF1 = notify (adapter -> us) and FFF2 = write (us -> adapter).
PREFERRED_SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
NAME_HINTS = ("obd", "veepeak", "vlink", "elm")
# Generic Access, Generic Attribute, Device Information: never the data channel.
IGNORED_SERVICES = ("00001800-", "00001801-", "0000180a-")
BLE_CHUNK = 20  # safe payload size for the default ATT MTU


class TransportError(Exception):
    pass


def make_transport(simulate: str | None = None, address: str | None = None, name: str | None = None):
    """The real BLE adapter, or a simulated vehicle when ``simulate`` names a profile."""
    if simulate:
        from .simulator import SimulatedTransport

        return SimulatedTransport(simulate)
    return BleTransport(address=address, name=name)


@dataclass
class FoundDevice:
    device: BLEDevice
    name: str
    rssi: int
    likely_obd: bool


async def discover(timeout: float = 8.0) -> list[FoundDevice]:
    """Scan for BLE devices, strongest signal first."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results = []
    for device, adv in found.values():
        name = device.name or adv.local_name or ""
        likely = any(h in name.lower() for h in NAME_HINTS) or PREFERRED_SERVICE in adv.service_uuids
        results.append(FoundDevice(device, name, adv.rssi, likely))
    return sorted(results, key=lambda d: d.rssi, reverse=True)


class BleTransport:
    def __init__(self, address: str | None = None, name: str | None = None, scan_timeout: float = 8.0):
        self.address = address
        self.name = name
        self.scan_timeout = scan_timeout
        self.device: BLEDevice | None = None
        self._client: BleakClient | None = None
        self._rx = None
        self._tx = None
        self._tx_response = True
        self._buffer = bytearray()
        self._prompt = asyncio.Event()

    async def connect(self) -> None:
        self.device = await self._find_device()
        self._client = BleakClient(self.device)
        await self._client.connect()
        self._rx, self._tx = self._pick_characteristics()
        self._tx_response = "write" in self._tx.properties
        await self._client.start_notify(self._rx, self._on_notify)

    async def close(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(self._rx)
            finally:
                await self._client.disconnect()

    async def stream(self, command: str, seconds: float) -> str:
        """Run a monitoring command (ATMA and friends) for a while, then stop it.

        Monitoring never returns a prompt on its own; any character stops it.
        """
        if not self._client:
            raise TransportError("not connected")
        self._buffer.clear()
        self._prompt.clear()
        await self._write((command + "\r").encode("ascii"))
        await asyncio.sleep(seconds)
        await self._write(b"x")  # stops monitoring; the adapter answers with a prompt
        try:
            await asyncio.wait_for(self._prompt.wait(), 3.0)
        except asyncio.TimeoutError:
            pass
        return self._buffer.decode("ascii", errors="replace").rsplit(">", 1)[0]

    async def send(self, command: str, timeout: float) -> str:
        """Send one command and return everything the adapter replied before the ``>`` prompt."""
        if not self._client:
            raise TransportError("not connected")
        self._buffer.clear()
        self._prompt.clear()
        await self._write((command + "\r").encode("ascii"))
        try:
            await asyncio.wait_for(self._prompt.wait(), timeout)
        except asyncio.TimeoutError:
            raise TransportError(f"timed out waiting for reply to {command!r}") from None
        return self._buffer.decode("ascii", errors="replace").rsplit(">", 1)[0]

    async def _write(self, payload: bytes) -> None:
        for i in range(0, len(payload), BLE_CHUNK):
            await self._client.write_gatt_char(self._tx, payload[i : i + BLE_CHUNK], response=self._tx_response)

    def _on_notify(self, _characteristic, data: bytearray) -> None:
        self._buffer.extend(data)
        if b">" in data:
            self._prompt.set()

    async def _find_device(self) -> BLEDevice:
        if self.address:
            device = await BleakScanner.find_device_by_address(self.address, timeout=self.scan_timeout)
            if not device:
                raise TransportError(f"no BLE device found at {self.address}")
            return device
        candidates = [d for d in await discover(self.scan_timeout) if self._matches(d)]
        if not candidates:
            raise TransportError(
                "no OBD adapter found. Is the ignition on and the adapter's light lit? "
                "Run `veepeak scan` to list nearby devices, then pass --address."
            )
        return candidates[0].device

    def _matches(self, found: FoundDevice) -> bool:
        if self.name:
            return self.name.lower() in found.name.lower()
        return found.likely_obd

    def _pick_characteristics(self):
        services = sorted(
            (s for s in self._client.services if not s.uuid.lower().startswith(IGNORED_SERVICES)),
            key=lambda s: s.uuid.lower() != PREFERRED_SERVICE,
        )
        for service in services:
            rx = next((c for c in service.characteristics if {"notify", "indicate"} & set(c.properties)), None)
            tx = next(
                (c for c in service.characteristics if {"write", "write-without-response"} & set(c.properties)),
                None,
            )
            if rx and tx:
                return rx, tx
        raise TransportError("adapter has no notify/write characteristic pair; is this an ELM327 BLE device?")
