import pytest


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Tests never touch the network: weather falls back to the deterministic generator."""
    monkeypatch.setenv("AGRIDRONE_OFFLINE", "1")
