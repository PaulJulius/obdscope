import pytest

from veepeak_reader import simulator


@pytest.fixture(autouse=True)
def no_simulated_latency(monkeypatch):
    monkeypatch.setattr(simulator, "LATENCY_SCALE", 0)
