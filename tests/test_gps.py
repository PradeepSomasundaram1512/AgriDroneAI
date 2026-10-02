import random

from agridrone import fleet as F
from agridrone import gps, planner, store, traffic
from agridrone.agent import run_cycle
from agridrone.config import load_policy


def pol(rate=3.0, **kw):
    p = load_policy()
    p["gps"] = {**p.get("gps", {}), "loss_per_flight_hour": rate, **kw}
    return p


def targets(n=120, seed=3):
    r = random.Random(seed)
    cells = list({(r.randrange(2, 24), r.randrange(0, 24)) for _ in range(n)})
    return sorted([(r.random(), c, "irrigate") for c in cells], reverse=True)


def test_quality_checks_before_and_during_flight():
    p = pol()
    assert gps.preflight_ok(14, 3, p) == (True, "")
    assert not gps.preflight_ok(8, 3, p)[0] and "satellites" in gps.preflight_ok(8, 3, p)[1]
    assert not gps.preflight_ok(14, 2, p)[0] and "3D" in gps.preflight_ok(14, 2, p)[1]
    assert not gps.preflight_ok(None, None, p)[0]
    assert gps.inflight_ok(7, 3, p) and not gps.inflight_ok(5, 3, p) and not gps.inflight_ok(12, 1, p)


def test_only_drones_above_the_one_in_distress_must_return():
    assert gps.must_return(30, [50]) and gps.must_return(50, [70])
    assert not gps.must_return(70, [50]) and not gps.must_return(50, [50])  # equal layer: it is the failing drone itself
    assert not gps.must_return(30, [])


def test_losses_are_deterministic_and_zero_rate_means_none():
    ms = planner.plan(targets(), pol(), sorties=3)
    assert gps.sample_losses(ms, pol(rate=0), 1, 1) == []
    a, b = gps.sample_losses(ms, pol(), 1, 4), gps.sample_losses(ms, pol(), 1, 4)
    assert a == b


def test_the_loss_rate_is_calibrated_to_flight_hours():
    p = pol(rate=0.5)
    ms = planner.plan(targets(), p, sorties=1)
    hours = sum(traffic.duration(m, p) for m in ms) / 3600
    n = sum(len(gps.sample_losses(ms, p, 1, day)) for day in range(1, 801))
    expect = 800 * len(ms) * (1 - 2.718281828 ** (-0.5 * hours / len(ms)))
    assert 0.7 * expect < n < 1.3 * expect


def test_an_outage_cuts_that_drone_cancels_its_later_sorties_and_sends_lower_drones_home():
    p = pol()
    ms = planner.plan(targets(200), p, sorties=3)
    by = {(m.drone, m.sortie): m for m in ms}
    high = by[(2, 0)]  # the 70 m drone
    ev = {"drone": 2, "sortie": 0, "t": 60, "after_patches": 0, "cell": [0, 0], "cut": 0, "altitude": high.altitude_m}
    flown, info = gps.apply_losses(ms, [ev], p)
    keys = {(m.drone, m.sortie) for m in flown}
    assert (2, 0) not in keys and (2, 1) not in keys and (2, 2) not in keys  # down in a field: no more flights today
    assert info["grounded"] == [2] and info["patches_deferred"] > 0
    flown0 = {m.drone: m for m in flown if m.sortie == 0}
    assert all(len(flown0[d].targets) <= len(by[(d, 0)].targets) for d in (0, 1) if d in flown0)  # lower layers were ordered home
    assert (0, 1) in keys and (1, 1) in keys  # later sorties of the healthy drones still fly


def test_a_low_drone_failing_does_not_cut_the_drones_above_it():
    p = pol()
    ms = planner.plan(targets(200), p, sorties=1)
    by = {m.drone: m for m in ms}
    ev = {"drone": 0, "sortie": 0, "t": 60, "after_patches": 0, "cell": [0, 0], "cut": 0, "altitude": by[0].altitude_m}
    flown, _ = gps.apply_losses(ms, [ev], p)
    assert {m.drone for m in flown} == {1, 2} and all(len(m.targets) == len(by[m.drone].targets) for m in flown)


def test_a_drone_down_in_a_field_cannot_lose_gps_again_and_energy_is_scaled():
    p = pol()
    ms = planner.plan(targets(200), p, sorties=3)
    m0 = next(m for m in ms if (m.drone, m.sortie) == (0, 0))
    evs = [
        {"drone": 0, "sortie": 0, "t": 100, "after_patches": 1, "cell": [3, 3], "cut": 2, "altitude": m0.altitude_m},
        {"drone": 0, "sortie": 1, "t": 50, "after_patches": 0, "cell": [0, 0], "cut": 0, "altitude": m0.altitude_m},
    ]
    flown, info = gps.apply_losses(ms, evs, p)
    assert len(info["events"]) == 1 and info["grounded"] == [0]
    cut = next(m for m in flown if (m.drone, m.sortie) == (0, 0))
    assert cut.energy_wh < m0.energy_wh and len(cut.targets) == 2


def test_grounded_drones_get_no_work_until_recovered():
    p = pol()
    f = F.Fleet(p)
    f.today = 10
    f.ground(1, 12)
    assert f.grounded(1) and f.budget(1, 0) == 0.0 and f.d[1]["incidents"] == 1
    ms = planner.plan(targets(200), p, sorties=3, fleet=f)
    assert all(m.drone != 1 for m in ms) and {m.drone for m in ms} == {0, 2}
    f.today = 12
    assert not f.grounded(1) and f.budget(1, 0) > 0


def test_agent_survives_gps_losses_grounds_drones_and_brings_them_back(tmp_path):
    p = pol(rate=2.0)
    p["field"]["size"] = 16
    saw_loss = saw_grounded = False
    for _ in range(60):
        rec = run_cycle(p, tmp_path)
        assert rec["ok"], rec
        saw_loss |= rec["gps_losses"] > 0
        saw_grounded |= rec["drones_grounded"] > 0
    assert saw_loss and saw_grounded
    audit = store.read_jsonl("audit.jsonl", tmp_path)
    assert any(a["kind"] == "gps_loss" for a in audit)
    assert not [a for a in audit if a["kind"] == "safety_block"]
    state = store.load_json("fleet.json", None, tmp_path)
    assert sum(x.get("incidents", 0) for x in state["drones"]) > 0
    assert rec["drones_grounded"] < p["fleet"]["drones"] + 1  # recovery brings them back: never a permanent outage


def test_zero_rate_changes_nothing(tmp_path):
    p = pol(rate=0)
    p["field"]["size"] = 16
    for _ in range(30):
        rec = run_cycle(p, tmp_path)
        assert rec["gps_losses"] == 0 and rec["drones_grounded"] == 0
