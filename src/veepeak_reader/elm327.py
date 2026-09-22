"""ELM327 command layer: AT commands, error detection and response framing.

Headers are always turned on (ATH1) so every response line identifies the ECU
that sent it. How a line is split into header / data depends on the protocol:

* Legacy protocols (J1850 PWM/VPW, ISO 9141, KWP): 3 header bytes, data, and
  a trailing checksum byte. One line is one complete message.
  ``41 6B 10 41 0C 1A F8 E5`` -> ECU ``10``, data ``41 0C 1A F8``.
* CAN 11-bit: a 3-hex-digit ID followed by an ISO-TP frame whose first byte
  is the PCI. Multi-frame messages are reassembled.
  ``7E8 04 41 0C 1A F8`` -> ECU ``7E8``, data ``41 0C 1A F8``.
* CAN 29-bit: as above but with a 4-byte header.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

log = logging.getLogger(__name__)

PROTOCOLS = {
    "1": "SAE J1850 PWM (41.6 kbaud)",
    "2": "SAE J1850 VPW (10.4 kbaud)",
    "3": "ISO 9141-2",
    "4": "ISO 14230-4 KWP (5 baud init)",
    "5": "ISO 14230-4 KWP (fast init)",
    "6": "ISO 15765-4 CAN (11 bit, 500 kbaud)",
    "7": "ISO 15765-4 CAN (29 bit, 500 kbaud)",
    "8": "ISO 15765-4 CAN (11 bit, 250 kbaud)",
    "9": "ISO 15765-4 CAN (29 bit, 250 kbaud)",
    "A": "SAE J1939 CAN",
}
CAN_11BIT = {"6", "8"}
CAN_29BIT = {"7", "9"}

_ERRORS = {
    "?", "UNABLE TO CONNECT", "CAN ERROR", "BUS ERROR", "BUS BUSY", "FB ERROR",
    "DATA ERROR", "BUFFER FULL", "STOPPED", "LV RESET", "ACT ALERT", "<RX ERROR",
}
Messages = dict[str, list[bytes]]


class Transport(Protocol):
    async def send(self, command: str, timeout: float) -> str: ...
    async def close(self) -> None: ...


class ElmError(Exception):
    pass


class NoData(ElmError):
    """The vehicle did not answer (normal for unsupported requests)."""


class Elm327:
    def __init__(self, transport: Transport, protocol: str = "0"):
        self.transport = transport
        self.requested_protocol = protocol
        self.protocol: str | None = None
        self.version: str | None = None

    @property
    def protocol_name(self) -> str:
        return PROTOCOLS.get(self.protocol or "", f"unknown ({self.protocol})")

    @property
    def is_can(self) -> bool:
        return self.protocol in CAN_11BIT | CAN_29BIT

    async def initialize(self) -> None:
        lines = await self.command("ATZ", timeout=5.0)
        self.version = next((line for line in lines if "ELM" in line.upper()), "unknown")
        for cmd in ("ATE0", "ATL0", "ATS1", "ATH1", f"ATSP{self.requested_protocol}"):
            await self.command(cmd)
        # The first OBD request makes the adapter search for / open the bus.
        await self.command("0100", timeout=20.0)
        self.protocol = (await self.command("ATDPN"))[-1].lstrip("A")

    async def voltage(self) -> str:
        return (await self.command("ATRV"))[-1]

    async def command(self, cmd: str, timeout: float = 5.0) -> list[str]:
        """Send a command and return its cleaned response lines, raising on adapter errors."""
        raw = await self.transport.send(cmd, timeout)
        log.debug(">> %s | << %r", cmd, raw)
        lines = []
        for line in re.split(r"[\r\n]+", raw):
            line = line.strip()
            if not line or line == cmd or line.startswith("SEARCHING"):
                continue
            if line.startswith("BUS INIT"):
                if "ERROR" in line:
                    raise ElmError(f"{cmd}: {line}")
                continue
            if line == "NO DATA":
                raise NoData(cmd)
            if line in _ERRORS or line.startswith("ERR"):
                raise ElmError(f"{cmd}: {line}")
            lines.append(line)
        return lines

    async def request(self, hex_request: str, timeout: float = 5.0) -> Messages:
        """Send an OBD request (hex string) and return messages grouped by responding ECU."""
        return parse_response(await self.command(hex_request, timeout), self.protocol)


def parse_response(lines: list[str], protocol: str | None) -> Messages:
    if protocol in CAN_11BIT:
        return _parse_can(lines, header_tokens=1)
    if protocol in CAN_29BIT:
        return _parse_can(lines, header_tokens=4)
    return _parse_legacy(lines)


def _hex(tokens: list[str], line: str) -> bytes:
    try:
        return bytes.fromhex("".join(tokens))
    except ValueError:
        raise ElmError(f"unexpected response line: {line!r}") from None


def _parse_legacy(lines: list[str]) -> Messages:
    messages: Messages = {}
    for line in lines:
        frame = _hex(line.split(), line)
        if len(frame) < 5:
            raise ElmError(f"response too short (are headers on?): {line!r}")
        messages.setdefault(f"{frame[2]:02X}", []).append(frame[3:-1])
    return messages


def _parse_can(lines: list[str], header_tokens: int) -> Messages:
    frames: dict[str, list[bytes]] = {}
    for line in lines:
        tokens = line.split()
        if len(tokens) <= header_tokens:
            raise ElmError(f"response too short (are headers on?): {line!r}")
        ecu = tokens[header_tokens - 1]
        frames.setdefault(ecu, []).append(_hex(tokens[header_tokens:], line))
    return {ecu: _reassemble(fs) for ecu, fs in frames.items()}


def _reassemble(frames: list[bytes]) -> list[bytes]:
    """Join ISO-TP single/first/consecutive frames into complete messages."""
    messages = []
    buffer: bytearray | None = None
    expected = 0
    for frame in frames:
        kind = frame[0] >> 4
        if kind == 0:
            messages.append(frame[1 : 1 + (frame[0] & 0x0F)])
        elif kind == 1:
            expected = ((frame[0] & 0x0F) << 8) | frame[1]
            buffer = bytearray(frame[2:])
        elif kind == 2 and buffer is not None:
            buffer.extend(frame[1:])
            if len(buffer) >= expected:
                messages.append(bytes(buffer[:expected]))
                buffer = None
    return messages
