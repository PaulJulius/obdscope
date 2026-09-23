"""Scripted stand-ins for the BLE adapter. Responses mimic real ELM327 output with headers on."""

from types import SimpleNamespace


class FakeTransport:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.sent: list[str] = []

    async def send(self, command: str, timeout: float) -> str:
        self.sent.append(command)
        reply = self.responses.get(command, "NO DATA")
        if isinstance(reply, list):  # a scripted sequence; the last reply repeats
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        return reply + "\r\r"

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


class FakeBleTransport(FakeTransport):
    """Drop-in for BleTransport in the web UI tests."""

    def __init__(self, responses: dict[str, str]):
        super().__init__(responses)
        self.device = SimpleNamespace(name="OBDBLE", address="FAKE-ADDRESS")
        self.closed = False

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True
