import io
import json
from datetime import date

import pytest

from agridrone import cli, dashboard, store, verify, weather
from agridrone.config import load_policy
from agridrone.weather import Weather

TODAY = date(2026, 6, 15)


def real_policy():
    p = load_policy()
    p["weather"] = {"source": "open-meteo", "lat": 42.0, "lon": -93.6, "start_doy": 120}
    return p


def table():
    days = [date(2026, 6, d).isoformat() for d in (13, 14, 15, 16, 17, 18)]
    return {d: Weather(float(i), 25.0, 14.0, 4.0 + i / 10, "open-meteo") for i, d in enumerate(days)}


def test_synthetic_weather_is_deterministic_and_physical():
    a, b = weather.synthetic(10, 3), weather.synthetic(10, 3)
    assert a == b and weather.synthetic(11, 3) != a
    for d in range(1, 200):
        w = weather.synthetic(d, 1)
        assert w.rain_mm >= 0 and w.tmax > w.tmin and 0.5 <= w.et0_mm <= 8.0


def test_real_weather_used_when_available_and_cached(tmp_path, monkeypatch):
    monkeypatch.delenv("AGRIDRONE_OFFLINE")
    w, fc, src = weather.get_weather(real_policy(), 1, tmp_path, fetch=lambda *a: table(), today=TODAY)
    assert src == "open-meteo" and w.rain_mm == 2.0 and [x.rain_mm for x in fc] == [3.0, 4.0, 5.0]
    assert (tmp_path / "weather_cache.json").exists()


def test_falls_back_to_cache_then_to_synthetic_when_the_api_is_down(tmp_path, monkeypatch):
    monkeypatch.delenv("AGRIDRONE_OFFLINE")

    def boom(*a):
        raise OSError("network down")

    weather.get_weather(real_policy(), 1, tmp_path, fetch=lambda *a: table(), today=TODAY)  # warm the cache
    w, _, src = weather.get_weather(real_policy(), 1, tmp_path, fetch=boom, today=TODAY)
    assert src == "open-meteo (cached)" and w.rain_mm == 2.0
    w, fc, src = weather.get_weather(real_policy(), 5, tmp_path / "empty", fetch=boom, today=TODAY)
    assert src == "synthetic" and len(fc) == 3  # never raises, never stops the autopilot


def test_offline_flag_forces_synthetic(tmp_path):
    _, _, src = weather.get_weather(real_policy(), 1, tmp_path, fetch=lambda *a: 1 / 0, today=TODAY)
    assert src == "synthetic"


def test_open_meteo_response_is_parsed(monkeypatch):
    body = {
        "daily": {
            "time": ["2026-06-15", "2026-06-16"],
            "precipitation_sum": [1.5, None],
            "temperature_2m_max": [24.0, 26.0],
            "temperature_2m_min": [12.0, 13.0],
            "et0_fao_evapotranspiration": [3.9, None],
        }
    }

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=0: R(json.dumps(body).encode()))
    t = weather._fetch_open_meteo(42.0, -93.6, TODAY)
    assert t["2026-06-15"].rain_mm == 1.5 and t["2026-06-16"].rain_mm == 0.0 and t["2026-06-16"].et0_mm == 0.0


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every module's state/report dir at tmp so the CLI never touches the real repo state."""
    monkeypatch.setattr(store, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(verify, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cli, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    (tmp_path / "docs").mkdir()
    p = load_policy()
    p["field"]["size"] = 8
    monkeypatch.setattr("agridrone.agent.load_policy", lambda: p)
    monkeypatch.setattr("agridrone.dashboard.load_policy", lambda: p)
    return tmp_path


def test_cli_cycle_report_verify_dashboard(sandbox, capsys):
    assert cli.main(["cycle", "-n", "3"]) == 0
    assert cli.main(["verify"]) == 0 and "state OK" in capsys.readouterr().out
    assert cli.main(["report", "weekly"]) == 0 and "Metrics vs targets" in capsys.readouterr().out
    assert cli.main(["report", "weekly", "--write"]) == 0 and list((sandbox / "reports").glob("weekly-*.md"))
    assert cli.main(["dashboard"]) == 0 and (sandbox / "docs" / "dashboard.html").exists()


def test_cli_verify_fails_on_corrupt_state(sandbox, capsys):
    cli.main(["cycle", "-n", "1"])
    (sandbox / "state" / "model.json").write_text("{nope")
    assert cli.main(["verify"]) == 1 and "STATE PROBLEMS" in capsys.readouterr().out


def test_dashboard_translates_audit_events_into_plain_language():
    ev = dashboard._events(
        [
            {"kind": "cycle", "day": 3, "targets": 40, "executed": 12, "dropped_by_safety": 1},
            {"kind": "cycle", "day": 4, "targets": 0, "executed": 0},
            {"kind": "retrain", "promoted": True},
            {"kind": "retrain", "promoted": False},
            {"kind": "model_below_target"},
            {"kind": "incident"},
        ]
    )
    text = " ".join(e["text"] for e in ev)
    assert "the 12 most urgent" in text and "all quiet" in text and "rollback" in text and "Heads-up" in text
    assert "wait for tomorrow" in text and "blocked by safety" in text
