"""OBD-II services on top of the ELM327 layer.

Every read method returns results keyed by the responding ECU. On a 2001
Expedition that is normally just the PCM (address ``10`` on J1850 PWM).
"""

from __future__ import annotations

from .dtc import decode_dtcs
from .elm327 import Elm327, ElmError, NoData
from .pids import decode_supported

NRC_NAMES = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x22: "conditions not correct",
    0x31: "request out of range",
    0x33: "security access denied",
    0x78: "response pending",
}


class NegativeResponse(ElmError):
    def __init__(self, service: int, code: int):
        self.service = service
        self.code = code
        super().__init__(f"service {service:02X} rejected: {NRC_NAMES.get(code, 'unknown')} (0x{code:02X})")


class Vehicle:
    def __init__(self, elm: Elm327):
        self.elm = elm

    async def service(self, request: bytes, timeout: float = 5.0) -> dict[str, list[bytes]]:
        """Send a request and return each ECU's positive responses (including the response SID)."""
        messages = await self.elm.request(request.hex().upper(), timeout)
        positive: dict[str, list[bytes]] = {}
        negative = None
        for ecu, msgs in messages.items():
            for m in msgs:
                if len(m) >= 3 and m[0] == 0x7F and m[1] == request[0]:
                    negative = m[2]
                elif m and m[0] == request[0] + 0x40:
                    positive.setdefault(ecu, []).append(m)
        if not positive:
            if negative is not None:
                raise NegativeResponse(request[0], negative)
            raise NoData(request.hex().upper())
        return positive

    async def _matching(self, request: bytes, echo_len: int) -> dict[str, bytes]:
        """First response per ECU whose echoed parameter bytes match the request."""
        results = {}
        for ecu, msgs in (await self.service(request)).items():
            for m in msgs:
                if m[1:echo_len] == request[1:echo_len]:
                    results[ecu] = m[echo_len:]
                    break
        if not results:
            raise NoData(request.hex().upper())
        return results

    # --- Mode 01: current data ---

    async def pid(self, pid: int) -> dict[str, bytes]:
        return await self._matching(bytes([0x01, pid]), echo_len=2)

    async def supported_pids(self) -> set[int]:
        return await self._supported(self.pid)

    # --- Mode 02: freeze frame ---

    async def freeze_frame_pid(self, pid: int, frame: int = 0) -> dict[str, bytes]:
        return await self._matching(bytes([0x02, pid, frame]), echo_len=3)

    async def freeze_frame_supported(self) -> set[int]:
        return await self._supported(self.freeze_frame_pid)

    async def _supported(self, read) -> set[int]:
        supported: set[int] = set()
        base = 0x00
        while True:
            try:
                per_ecu = await read(base)
            except (NoData, NegativeResponse):
                break
            for data in per_ecu.values():
                supported |= decode_supported(base, data)
            if base + 0x20 not in supported or base >= 0xE0:
                break
            base += 0x20
        return supported

    # --- Modes 03 / 07 / 04: trouble codes ---

    async def dtcs(self, mode: int = 0x03) -> dict[str, list[str]]:
        """Stored (mode 03) or pending (mode 07) codes."""
        try:
            responses = await self.service(bytes([mode]))
        except NoData:
            return {}
        results = {}
        for ecu, msgs in responses.items():
            # CAN puts a count byte after the SID; legacy protocols do not.
            skip = 2 if self.elm.is_can else 1
            results[ecu] = [code for m in msgs for code in decode_dtcs(m[skip:])]
        return results

    async def clear_dtcs(self) -> None:
        await self.service(bytes([0x04]), timeout=10.0)

    # --- Mode 09: vehicle information ---

    async def vin(self) -> str | None:
        """VIN, or None if the vehicle doesn't report it (common before model year 2005)."""
        try:
            responses = await self.service(bytes([0x09, 0x02]), timeout=10.0)
        except (NoData, NegativeResponse):
            return None
        msgs = next(iter(responses.values()))
        # Legacy: several messages "49 02 <seq> d d d d". CAN: one "49 02 <count> ...".
        raw = b"".join(m[3:] for m in sorted(msgs, key=lambda m: m[2]))
        text = "".join(chr(b) for b in raw if chr(b).isalnum())
        return text[-17:] if len(text) >= 17 else None

    # --- Mode 22: manufacturer enhanced data ---

    async def enhanced(self, did: int) -> dict[str, bytes]:
        return await self._matching(bytes([0x22, did >> 8, did & 0xFF]), echo_len=3)
