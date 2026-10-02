import json

from agridrone import briefing, cli
from agridrone.agent import run_cycle
from agridrone.config import load_policy
from agridrone.dashboard import build_data


def data_with(**metrics):
    base = {
        "day": 12,
        "kpi": {
            "healthy_pct": 90,
            "watch": 40,
            "stressed": 6,
            "water": 82.0,
            "chem": 95.0,
            "accuracy": 0.9,
            "recall": 0.9,
            "yield": 0.99,
            "cells": 576,
        },
        "metrics": [
            {
                "day": 12,
                "targets": 30,
                "executed": 24,
                "sorties": 3,
                "wind_ms": 3.1,
                "gust_ms": 5.0,
                "wind_from_deg": 270,
                "grounded_by_wind": False,
                "spray_deferred": 0,
                "gps_losses": 0,
                "drones_grounded": 0,
                "sensors_flagged": 0,
                "sensors_quarantined": 0,
                "soc_min_pct": 28.0,
                "traffic_min_ratio": 1.2,
                "traffic_mode": "concurrent",
                **metrics,
            }
        ],
        "events": [{"text": "something happened"}],
        "weather": {"forecast": [{"rain_mm": 0.0}, {"rain_mm": 2.0}]},
        "satellite": None,
        "fleet": {"mode": "charge", "rate_w": 90, "turnaround_min": 45, "drones": [{"health": 0.99}, {"health": 0.98}]},
        "economics": None,
    }
    return base


def test_briefing_is_plain_and_leads_with_the_farm_status():
    s = briefing.sentences(data_with())
    assert s[0].startswith("Day 12") and "90%" in s[0] and "24 patches in 3 waves" in s[1]
    assert not any("None" in x or "nan" in x.lower() for x in s)


def test_briefing_reports_wind_gps_sensor_and_rain_events():
    s = " ".join(
        briefing.sentences(
            data_with(
                grounded_by_wind=True,
                wind_ms=7.0,
                wind_from_deg=315,
                gps_losses=1,
                gps_patches_deferred=5,
                sensors_flagged=3,
                sensors_quarantined=1,
            )
        )
    )
    assert (
        "stayed on the ground" in s
        and "north-west" in s
        and "lost GPS" in s
        and "5 patches were rescheduled" in s
        and "sensors need maintenance" in s
    )
    d = data_with(spray_deferred=9)
    d["weather"]["forecast"] = [{"rain_mm": 8.0}, {"rain_mm": 6.0}]
    s2 = " ".join(briefing.sentences(d))
    assert "postponed for 9 patches" in s2 and "Rain is forecast" in s2
    assert "waiting to be picked up" in " ".join(briefing.sentences(data_with(drones_grounded=2)))


def test_rules_answer_the_common_questions_from_the_data():
    d = data_with(grounded_by_wind=True, wind_ms=7.0, wind_from_deg=0)
    t, src = briefing.answer("Why was the fleet grounded today?", d)
    assert src == "rules" and "7.0 m/s" in t and "north" in t
    assert "GPS losses so far" in briefing.answer("did any drone lose gps?", d)[0]
    assert "28" in briefing.answer("how are the batteries", d)[0] and "98%" in briefing.answer("battery health", d)[0]
    assert "82% less water" in briefing.answer("how much water did we save", d)[0]
    assert "24 patches" in briefing.answer("how many patches did you treat", d)[0]
    assert "1.2x" in briefing.answer("is it safe, will they collide", d)[0]
    assert "don't know" in briefing.answer("what is the capital of france", d)[0]


def test_money_answer_uses_the_economics_when_present():
    d = data_with()
    d["economics"] = {"rows": [{"id": "agent-3", "profit": 300000}, {"id": "calendar", "profit": 260000}]}
    assert "$300,000" in briefing.answer("is this worth the money?", d)[0]


class FakeClaude:
    def __init__(self, reply="Your farm is fine.", boom=False):
        self.calls, self.reply, self.boom = [], reply, boom
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("network down")
        return type("R", (), {"content": [type("B", (), {"type": "text", "text": self.reply})()]})()


def test_without_a_key_it_answers_from_the_rules_only(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert briefing.ask("why was the fleet grounded?", data_with(grounded_by_wind=True))[1] == "rules"


def test_claude_gets_only_the_facts_and_strict_instructions_and_is_labelled():
    fake = FakeClaude("The wind was too strong.")
    text, src = briefing.ask("why no flights?", data_with(grounded_by_wind=True), client=fake)
    assert (text, src) == ("The wind was too strong.", "claude")
    call = fake.calls[0]
    assert (
        "ONLY the JSON facts" in call["system"] and "Never invent numbers" in call["system"] and "tools" not in call
    )  # read-only: no tools at all
    body = call["messages"][0]["content"]
    assert "FACTS:" in body and "why no flights?" in body and '"grounded_by_wind": true' in body


def test_any_model_failure_or_empty_reply_falls_back_to_the_rules():
    d = data_with(grounded_by_wind=True)
    assert briefing.ask("why grounded?", d, client=FakeClaude(boom=True))[1] == "rules"
    assert briefing.ask("why grounded?", d, client=FakeClaude(reply="  "))[1] == "rules"
    assert len(briefing.ask("x", d, client=FakeClaude("y" * 5000))[0]) <= 1200


def test_real_dashboard_data_produces_a_briefing_and_cli_works(tmp_path, monkeypatch, capsys):
    from agridrone import store

    monkeypatch.setattr(store, "STATE_DIR", tmp_path / "state")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = load_policy()
    p["field"]["size"] = 10
    monkeypatch.setattr("agridrone.dashboard.load_policy", lambda: p)
    for _ in range(5):
        run_cycle(p, tmp_path / "state")
    data = build_data(tmp_path / "state", p)
    assert data["briefing"] and json.dumps(data["briefing"])
    monkeypatch.setattr("agridrone.dashboard.build_data", lambda *a, **k: data)
    assert cli.main(["brief"]) == 0 and capsys.readouterr().out.startswith("- Day")
    assert cli.main(["ask", "how", "many", "patches", "today"]) == 0 and "answered by: rules" in capsys.readouterr().out
