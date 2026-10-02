import random

from agridrone import fleet as F
from agridrone import planner, safety, store
from agridrone.agent import run_cycle
from agridrone.config import load_policy
from agridrone.safety import Mission


def pol(**chg):
    p = load_policy()
    p["fleet"]["charging"] = {"mode": "charge", "rate_w": 90, "turnaround_min": 45, "swap_min": 3, **chg}
    return p


def targets(n=300, seed=1):
    r = random.Random(seed)
    cells = list({(r.randrange(2, 24), r.randrange(0, 24)) for _ in range(n)})
    return sorted([(r.random(), c, "irrigate") for c in cells], reverse=True)


def patches(ms):
    return sum(1 for m in ms for _, a in m.targets if a != "via")


def test_first_sortie_uses_the_charge_in_the_battery_and_later_ones_what_charging_restores():
    f = F.Fleet(pol())
    assert f.budget(0, 0) == 75.0  # 100 Wh pack, 25% reserve
    assert abs(f.budget(0, 1) - 67.5) < 1e-6  # 90 W x 45 min = 67.5 Wh back, the pack could take 75
    assert F.Fleet(pol(rate_w=30)).budget(0, 1) == 22.5  # a slow charger really limits the next flight
    assert F.Fleet(pol(mode="swap")).budget(0, 1) == 75.0  # spare packs: always full
    f.d[0]["soc_wh"] = 60.0
    assert f.budget(0, 0) == 35.0  # a drone that starts the day low can do less


def test_slow_charging_costs_capacity_swap_does_not():
    tg = targets()
    results = {}
    for name, p in (("swap", pol(mode="swap")), ("fast", pol(rate_w=200)), ("default", pol()), ("slow", pol(rate_w=25))):
        results[name] = patches(planner.plan(tg, p, sorties=3, fleet=F.Fleet(p)))
    assert results["swap"] >= results["fast"] >= results["default"] > results["slow"]


def test_planner_output_is_always_energy_feasible_for_every_charging_setup():
    for seed in range(4):
        for p in (pol(), pol(rate_w=20), pol(mode="swap"), pol(turnaround_min=15)):
            f = F.Fleet(p)
            ms = planner.plan(targets(250, seed), p, sorties=3, fleet=f)
            assert f.simulate(ms)["violations"] == []
            assert not safety.validate(ms, p)


def test_simulate_flags_a_plan_that_would_drain_below_the_reserve():
    p = pol(rate_w=20)
    f = F.Fleet(p)
    big = lambda s: Mission(0, 30, [((5, 5), "irrigate")], 70.0, sortie=s)  # 70 Wh each: fine once, not twice at 20 W
    sim = f.simulate([big(0), big(1)])
    assert len(sim["violations"]) == 1 and "sortie 1" in sim["violations"][0] and "safety reserve" in sim["violations"][0]
    assert f.simulate([big(0)])["violations"] == []


def test_soc_timeline_charges_between_flights():
    f = F.Fleet(pol())
    sim = f.simulate([Mission(0, 30, [((5, 5), "irrigate")], 60.0, sortie=0), Mission(0, 30, [((6, 6), "irrigate")], 50.0, sortie=1)])
    a, b = sim["drones"][0]
    assert a["soc_start"] == 100 and a["soc_end"] == 40
    assert abs(b["charged"] - 60) < 1e-6 and b["soc_start"] == 100 and b["soc_end"] == 50  # 40 + 67.5 capped at the 100 Wh pack
    assert sim["used_wh"] == 110 and abs(sim["charged_wh"] - 60) < 1e-6


def test_overnight_charge_refills_and_reports_time_needed():
    f = F.Fleet(pol(rate_w=90))
    f.d[0]["soc_wh"], f.d[1]["soc_wh"] = 40.0, 70.0
    wh, hours = f.overnight()
    assert wh == 90.0 and abs(hours - 60 / 90) < 1e-6 and all(f.d[i]["soc_wh"] == f.cap(i) for i in range(3))


def test_batteries_wear_with_use_and_capacity_shrinks_the_budget():
    p = pol()
    f = F.Fleet(p)
    for _ in range(300):  # 300 days of ~60 Wh flights
        f.overnight()
        sim = f.simulate([Mission(0, 30, [((5, 5), "irrigate")], 60.0)])
        f.commit(sim)
    assert 0.85 < f.d[0]["health"] < 0.95 and f.d[1]["health"] == 1.0
    assert f.cap(0) < 100 and f.budget(0, 0) < 75
    f.d[0]["cycles"] = 1e6
    f.commit(f.simulate([Mission(0, 30, [((5, 5), "irrigate")], 1.0)]))
    assert f.d[0]["health"] == F.MIN_HEALTH


def test_fleet_state_persists_and_survives_a_bigger_fleet(tmp_path):
    p = pol()
    f = F.Fleet(p)
    f.d[1]["soc_wh"], f.d[1]["cycles"] = 55.5, 12.0
    f.save(tmp_path)
    g = F.Fleet.load(p, tmp_path)
    assert g.d[1]["soc_wh"] == 55.5 and g.d[1]["cycles"] == 12.0
    p["fleet"]["drones"] = 5
    assert len(F.Fleet.load(p, tmp_path).d) == 5


def test_agent_charges_overnight_and_tracks_battery_wear_across_days(tmp_path):
    p = load_policy()
    p["field"]["size"] = 16
    last = None
    for _ in range(45):
        last = run_cycle(p, tmp_path)
        assert last["ok"], last
    fleet_state = store.load_json("fleet.json", None, tmp_path)
    assert fleet_state and len(fleet_state["drones"]) == p["fleet"]["drones"]
    assert last["battery_health_min"] <= 1.0 and "overnight_charge_wh" in last and 0 <= last["soc_min_pct"] <= 100
    assert any(
        r.get("charged_between_flights_wh", 0) > 0 or r.get("fleet_energy_wh", 0) >= 0 for r in store.read_jsonl("metrics.jsonl", tmp_path)
    )
