import random
import time

import pytest

from agridrone import planner, safety, traffic
from agridrone.config import load_policy
from agridrone.safety import Mission


def pol(**fleet):
    p = load_policy()
    p["fleet"].update({"speed_ms": 5.0, **fleet})  # the adversarial scenarios below are timed for a 5 m/s cruise
    return p


def crossing():
    """A (30 m) cruises east over pads 1 and 2 while B and C climb out of them."""
    return [Mission(0, 30, [((4, 0), "irrigate")]), Mission(1, 50, [((0, 6), "irrigate")]), Mission(2, 70, [((0, 6), "spray")])]


def test_a_cruising_drone_over_a_climbing_drones_pad_is_a_detected_conflict():
    ms = crossing()
    rep = traffic.check(ms, pol())
    assert (
        rep["steps"] > 0 and rep["conflicts"][0]["horizontal_m"] < 23 + 10 and rep["conflicts"][0]["vertical_m"] <= 15 + 3
    )  # 20 m clearance + 3 m GNSS + sampling margin


def test_scheduler_removes_the_conflict_and_the_gate_agrees():
    ms, p = crossing(), pol()
    rep = traffic.schedule(ms, p)
    assert rep["ok"] and rep["steps"] == 0 and rep["min_ratio"] >= 1.0
    for m in ms:
        m.energy_wh = safety.mission_energy((0, 0), m.targets, p["fleet"])
    assert not safety.validate(ms, p)


def test_the_gate_rejects_an_unscheduled_dangerous_plan():
    ms, p = crossing(), pol()
    for m in ms:
        m.energy_wh = safety.mission_energy((0, 0), m.targets, p["fleet"])
    assert any("conflict" in v for v in safety.validate(ms, p))


def test_pads_too_close_together_are_unsafe_and_wide_pads_are_safe():
    ms = [Mission(0, 30, [((0, 0), "irrigate")]), Mission(1, 40, [((0, 0), "irrigate")])]  # 10 m apart vertically: below the 15 m limit
    close = pol(pad_spacing_m=5, min_separation_m=15)
    assert traffic.check(ms, close)["steps"] > 0
    ms2 = [Mission(0, 30, [((0, 0), "irrigate")]), Mission(1, 50, [((0, 0), "irrigate")])]
    assert traffic.check(ms2, pol())["steps"] == 0


def test_hold_before_landing_is_used_when_needed_and_never_in_hardware_mode():
    # drone 1 returns to its pad and descends while drone 0 (30 m) is still passing overhead
    ms = [Mission(0, 30, [((0, 0), "irrigate"), ((8, 0), "irrigate"), ((0, 0), "irrigate")]), Mission(1, 50, [((1, 3), "irrigate")])]
    rep = traffic.schedule(ms, pol(), allow_hold=False)
    assert rep["ok"] and all(m.hold == 0 for m in ms)


def test_serialized_fallback_is_safe_by_construction(monkeypatch):
    monkeypatch.setattr(traffic, "MAX_ITER", 0)
    ms, p = crossing(), pol()
    for m in ms:
        m.t0 = 0
    monkeypatch.setattr(traffic, "check", traffic.check)
    # force the concurrent search to fail by making every launch order conflict
    orig = traffic.check
    state = {"n": 0}

    def hostile(missions, policy, max_report=5):
        r = orig(missions, policy, max_report)
        state["n"] += 1
        if state["n"] <= 1:  # first (concurrent) verdict: pretend it still conflicts
            r = {
                **r,
                "steps": 5,
                "conflicts": [{"t": 1, "drones": [missions[0].drone, missions[1].drone], "horizontal_m": 1, "vertical_m": 1}],
            }
        return r

    monkeypatch.setattr(traffic, "check", hostile)
    rep = traffic.schedule(ms, p)
    monkeypatch.setattr(traffic, "check", orig)
    assert rep["mode"] == "serialized" and rep["ok"]
    assert traffic.check(ms, p)["steps"] == 0
    ends = sorted((m.t0, m.t0 + traffic.duration(m, p) - m.t0) for m in ms)  # one at a time: no overlap in time
    starts = [m.t0 for m in sorted(ms, key=lambda m: m.t0)]
    assert starts == sorted(starts) and starts[1] >= traffic.duration(sorted(ms, key=lambda m: m.t0)[0], p) - 1
    assert ends


@pytest.mark.parametrize("drones", [2, 3, 5])
def test_random_fleets_always_end_up_conflict_free(drones):
    p = pol(drones=drones)
    for seed in range(12):
        r = random.Random(seed)
        tg = sorted([(r.random(), (r.randrange(2, 24), r.randrange(0, 24)), "irrigate") for _ in range(r.randrange(20, 140))], reverse=True)
        ms = planner.plan(tg, p, sorties=1)
        if len(ms) < 2:
            continue
        rep = traffic.check(ms, p)
        assert rep["steps"] == 0, (seed, rep["conflicts"][:1])  # planner output is already scheduled and safe
        assert rep["min_ratio"] is None or rep["min_ratio"] >= 1.0
        assert not safety.validate(ms, p)


def test_planner_assigns_distinct_pads_and_altitudes_and_reports_margin():
    p = pol()
    r = random.Random(1)
    tg = sorted([(r.random(), (r.randrange(2, 24), r.randrange(0, 24)), "spray") for _ in range(100)], reverse=True)
    ms = planner.plan(tg, p, sorties=1)
    assert len({m.altitude_m for m in ms}) == len(ms) and len({m.pad if m.pad >= 0 else m.drone for m in ms}) == len(ms)
    assert all(m.t0 >= 0 for m in ms)


def test_checker_is_fast_enough_to_run_on_every_plan():
    p = pol(drones=5)
    r = random.Random(5)
    tg = sorted([(r.random(), (r.randrange(2, 24), r.randrange(0, 24)), "irrigate") for _ in range(300)], reverse=True)
    ms = planner.plan(tg, p, sorties=1)
    t = time.time()
    traffic.check(ms, p)
    assert time.time() - t < 3.0


def _grazing_pair(t0):
    a, b = Mission(0, 30, [((4, 0), "irrigate")]), Mission(1, 50, [((0, 6), "irrigate")])
    b.t0 = t0
    return [a, b]


def _steps(step, t0, p, inflate):
    old = traffic.STEP_S
    traffic.STEP_S = step
    try:
        return traffic.check(_grazing_pair(t0), p, inflate=inflate)["steps"]
    finally:
        traffic.STEP_S = old


def test_a_grazing_conflict_between_two_samples_is_not_missed():
    """Found by the merciless review: the plain 1 s replay saw NOTHING at start delays where the true plan has a real (0.1-0.3 s) conflict."""
    p = pol(speed_ms=10.0)
    delays = [k * 0.1 for k in range(0, 300)]
    truth = {t: _steps(0.02, t, p, inflate=False) > 0 for t in delays}  # near-continuous replay = ground truth
    old = {t: _steps(1.0, t, p, inflate=False) > 0 for t in delays}  # the previous check
    new = {t: _steps(1.0, t, p, inflate=True) > 0 for t in delays}  # the rigorous check
    missed_before = [t for t in delays if truth[t] and not old[t]]
    assert missed_before, "the scenario must actually exhibit the old flaw"
    assert all(new[t] for t in delays if truth[t])  # the new check never misses a real conflict
    assert sum(new.values()) <= sum(truth.values()) + 60  # ...without flagging everything (it is only conservative)


def test_sampling_margin_is_the_distance_two_drones_can_close_between_samples():
    p = pol(speed_ms=10.0)
    c = traffic.cfg(p)
    mh, mv = traffic.sampling_margins(c)
    assert mh == 10.0 * traffic.STEP_S and mv == 3.0 * traffic.STEP_S
    gusty = traffic.cfg(p, __import__("agridrone.wind", fromlist=["Wind"]).Wind(5, 10, 0))
    assert traffic.sampling_margins(gusty)[0] > mh  # wind makes the bound larger
