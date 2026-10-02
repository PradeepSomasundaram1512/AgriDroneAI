import math
import random


from agridrone import planner, safety, store, traffic, weather
from agridrone import wind as W
from agridrone.agent import run_cycle
from agridrone.config import load_policy
from agridrone.safety import Mission, mission_energy
from agridrone.sim import Farm
from agridrone import model as M
from agridrone.weather import Weather


def pol(**wind):
    p = load_policy()
    p["wind"] = {**p.get("wind", {}), **wind}
    return p


def targets(n=150, seed=1):
    r = random.Random(seed)
    cells = list({(r.randrange(2, 24), r.randrange(0, 24)) for _ in range(n)})
    return sorted([(r.random(), c, "irrigate") for c in cells], reverse=True)


# ------------------------------------------------------------------ physics
def test_wind_direction_convention_is_meteorological():
    vx, vy = W.Wind(5, 5, 0).vector(10)  # wind FROM the north blows south
    assert abs(vx) < 1e-9 and abs(vy + 5) < 1e-9
    vx, vy = W.Wind(5, 5, 270).vector(10)  # from the west blows east
    assert abs(vx - 5) < 1e-9 and abs(vy) < 1e-9


def test_wind_is_stronger_higher_up():
    w = W.Wind(6, 9, 270)
    assert w.at(10) == 6 and w.at(30) < w.at(50) < w.at(70)
    assert 1.15 < w.at(30) / 6 < 1.3 and 1.4 < w.at(70) / 6 < 1.5


def test_wind_triangle_headwind_tailwind_crosswind():
    a = 8.0  # the physics is independent of the fleet's configured cruise speed
    assert W.ground_speed(1, 0, a, (0, 0)) == a
    assert W.ground_speed(1, 0, a, (3, 0)) == a + 3  # tailwind (air moving east, flying east)
    assert W.ground_speed(1, 0, a, (-3, 0)) == a - 3  # headwind
    assert abs(W.ground_speed(1, 0, a, (0, 4)) - math.sqrt(64 - 16)) < 1e-9  # crosswind: it crabs, ground speed drops
    assert W.ground_speed(1, 0, a, (0, 8)) is None  # crosswind >= airspeed: cannot hold the track
    assert W.ground_speed(1, 0, a, (-7, 0)) is None  # headwind leaves < 25% of airspeed: no headway


def test_a_round_trip_in_wind_always_costs_more_than_in_calm():
    for ang in range(0, 360, 30):
        for speed in (1, 3, 4):
            v = W.Wind(speed, speed, ang).vector(30)
            out, back = W.leg_factor(10, 0, 10, v), W.leg_factor(-10, 0, 10, v)
            assert (out + back) / 2 >= 1.0 - 1e-9  # the headwind leg always costs more than the tailwind leg saves


def test_energy_depends_on_direction_and_calm_matches_the_old_model():
    fleet = pol()["fleet"]
    route = [((10, 0), "irrigate")]
    calm = mission_energy((0, 0), route, fleet)
    assert abs(calm - mission_energy((0, 0), route, fleet, W.Wind(0, 0, 0), 30)) < 1e-9
    west, east = (mission_energy((0, 0), route, fleet, W.Wind(5, 5, a), 30) for a in (270, 90))
    # the leg from the pad east and back: the total is the same either way round, but never below calm
    assert west >= calm and east >= calm
    # an out-and-back route costs the same either way round (headwind and tailwind legs swap), but a triangle does not
    tri = [((10, 0), "via"), ((10, 10), "via")]
    e1, e2 = (mission_energy((0, 0), tri, fleet, W.Wind(3, 3, ang), 30) for ang in (270, 90))
    assert e1 != e2 and min(e1, e2) >= mission_energy((0, 0), tri, fleet)
    assert mission_energy((0, 0), route, fleet, W.Wind(40, 40, 0), 30) == float("inf")  # a gale: no headway


def test_go_no_go_limits_and_spray_drift_rule():
    p = pol()
    assert W.go_no_go(W.Wind(3, 5, 0), p) == (True, True, "")
    ok, spray, why = W.go_no_go(W.Wind(6, 8, 0), p)
    assert ok and not spray and "drift" in why
    ok, spray, why = W.go_no_go(W.Wind(9, 10, 0), p)
    assert not ok and not spray and "flight limit" in why
    assert not W.go_no_go(W.Wind(5, 13, 0), p)[0]  # gusts alone can ground the fleet


def test_gusts_and_gnss_error_widen_the_required_clearance():
    p = pol()
    assert W.clearance_margin(W.CALM, p) == 3.0
    assert W.clearance_margin(W.Wind(5, 12, 0), p) == 3.0 + 0.5 * 12
    assert traffic.cfg(p, W.Wind(5, 12, 0))["horizontal_clearance_m"] > traffic.cfg(p)["horizontal_clearance_m"]


def test_spray_drift_loss_grows_with_wind():
    assert W.spray_efficacy(0) == 1.0 and W.spray_efficacy(2) == 1.0
    assert W.spray_efficacy(4.5) < 0.85 and W.spray_efficacy(30) == 0.3
    f = Farm.create(8, 1)
    c = f.cells[(2, 2)]
    c.pest = 0.6
    f.flight_wind = 0.0
    f.apply((2, 2), "spray")
    calm = c.pest
    c.pest = 0.6
    f.flight_wind = 4.5
    f.apply((2, 2), "spray")
    assert calm < c.pest  # drifting spray kills less


# ------------------------------------------------------------------ weather
def test_synthetic_wind_is_plausible_deterministic_and_does_not_change_rain():
    ws = [weather.synthetic(d, 1) for d in range(1, 361)]
    sp = [w.wind_ms for w in ws]
    assert 3.0 < sum(sp) / len(sp) < 6.0 and max(sp) < 25
    assert all(w.gust_ms >= w.wind_ms and 0 <= w.wind_from_deg < 360 for w in ws)
    assert weather.synthetic(5, 2) == weather.synthetic(5, 2)
    assert [weather.synthetic(d, 3).rain_mm for d in range(1, 8)] == [0.0, 0.0, 21.0, 1.7, 7.3, 0.0, 7.2]  # unchanged by the wind work


def test_open_meteo_wind_columns_are_parsed_and_old_cache_entries_still_load(monkeypatch, tmp_path):
    import io
    import json

    body = {
        "daily": {
            "time": ["2026-06-15"],
            "precipitation_sum": [0.0],
            "temperature_2m_max": [24.0],
            "temperature_2m_min": [12.0],
            "et0_fao_evapotranspiration": [4.0],
            "wind_speed_10m_max": [7.5],
            "wind_gusts_10m_max": [13.0],
            "wind_direction_10m_dominant": [225],
        }
    }

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=0: R(json.dumps(body).encode()) if "wind_speed_unit=ms" in url else 1 / 0
    )
    w = weather._fetch_open_meteo(42.0, -93.6, None)["2026-06-15"]
    assert (w.wind_ms, w.gust_ms, w.wind_from_deg) == (7.5, 13.0, 225.0)
    assert Weather(**{"rain_mm": 1, "tmax": 20, "tmin": 10, "et0_mm": 3, "source": "open-meteo"}).wind_ms == 0.0  # pre-wind cache entry


# ------------------------------------------------------------------ planner + gate
def test_gate_recomputes_energy_and_catches_an_underestimating_plan():
    p = pol()
    m = Mission(0, 30, [((4, 3), "irrigate"), ((3, 5), "irrigate")], energy_wh=1.0, wind=(3.0, 3.0, 270.0))
    assert not [v for v in safety.validate([m], p) if "energy" in v or "headway" in v]  # a genuinely modest route in a light wind passes
    assert 3 < mission_energy((0, 0), m.targets, p["fleet"], W.Wind(3.0, 3.0, 270.0).design(), 30) < 60
    big = Mission(0, 70, [((22, 0), "irrigate"), ((22, 22), "irrigate"), ((0, 22), "irrigate")], energy_wh=1.0, wind=(7.5, 8.0, 270.0))
    assert any("energy" in v or "headway" in v for v in safety.validate([big], p))  # the stored 1 Wh is not trusted


def test_the_gate_rejects_a_plan_made_for_wind_above_the_limit():
    p = pol()
    m = Mission(0, 30, [((4, 4), "irrigate")], wind=(9.0, 10.0, 0.0))
    m.energy_wh = mission_energy((0, 0), m.targets, p["fleet"])
    assert any("flight limit" in v for v in safety.validate([m], p))
    sp = Mission(0, 30, [((4, 4), "spray")], wind=(6.0, 7.0, 0.0))
    sp.energy_wh = 10
    assert any("drift" in v for v in safety.validate([sp], p))
    ir = Mission(0, 30, [((4, 4), "irrigate")], wind=(6.0, 7.0, 0.0))
    ir.energy_wh = 10
    assert not any("drift" in v for v in safety.validate([ir], p))


def test_planner_output_always_passes_the_gate_for_any_wind():
    p = load_policy()
    for seed, (speed, ang) in enumerate([(0, 0), (2, 90), (4, 180), (6, 270), (7.5, 45), (7.9, 300)]):
        wind = W.Wind(speed, speed * 1.3, ang)
        ms = planner.plan(targets(200, seed), p, sorties=3, wind=wind)
        ms, _ = safety.repair(ms, p)
        assert not [v for v in safety.validate(ms, p) if "conflict" not in v], (speed, ang)


def test_strong_wind_shrinks_capacity_and_headwind_direction_matters():
    p = load_policy()
    patches = lambda ms: sum(1 for m in ms for _, a in m.targets if a != "via")  # noqa: E731
    calm = patches(planner.plan(targets(300, 2), p, sorties=3, wind=W.Wind(0, 0, 0)))
    windy = patches(planner.plan(targets(300, 2), p, sorties=3, wind=W.Wind(6.5, 8.5, 270)))
    assert windy < calm
    ms = planner.plan(targets(300, 2), p, sorties=1, wind=W.Wind(6.5, 8.5, 270))
    assert all(m.wind == (6.5, 8.5, 270.0) for m in ms)


def test_a_wind_blind_plan_would_breach_the_battery_reserve_and_the_gate_catches_it():
    """A wind-blind planner fills each battery to its limit; apply a real wind to the same routes and some blow the reserve."""
    p = load_policy()
    usable = p["fleet"]["battery_wh"] * (1 - p["fleet"]["min_reserve_pct"] / 100)
    wind = W.Wind(5.0, 5.5, 270)
    breaches = total_calm = total_wind = 0
    for seed in range(4):
        for m in planner.plan(targets(300, seed), p, sorties=1):  # wind-blind
            e = mission_energy((0, 0), m.targets, p["fleet"], wind.design(), m.altitude_m)
            total_calm, total_wind = total_calm + m.energy_wh, total_wind + e
            breaches += e > usable
            blind = Mission(m.drone, m.altitude_m, m.targets, m.energy_wh, wind=wind.as_tuple())
            assert (e > usable) == any("energy" in v or "headway" in v for v in safety.validate([blind], p))  # the gate agrees, always
    assert total_wind > 1.15 * total_calm and breaches > 0


def test_wind_aware_planning_never_exceeds_the_reserve_in_the_same_conditions():
    p = load_policy()
    usable = p["fleet"]["battery_wh"] * (1 - p["fleet"]["min_reserve_pct"] / 100)
    wind = W.Wind(5.0, 5.5, 270)
    for seed in range(4):
        for m in planner.plan(targets(300, seed), p, sorties=1, wind=wind):
            assert mission_energy((0, 0), m.targets, p["fleet"], wind.design(), m.altitude_m) <= usable + 1e-6


def test_spray_targets_are_deferred_not_dropped_when_too_windy():
    p = load_policy()
    f = Farm.create(8, 1)
    f.observe = lambda cell, noise=0.0, _o=f.observe: _o(cell, noise=0.0)
    for i, c in enumerate(f.cells.values()):
        c.pest, c.moisture = (0.8, 0.4) if i % 2 == 0 else (0.0, 0.1)
    deferred = []
    calm = planner.find_targets(f, M.StressModel(), p["thresholds"], spray_ok=True)
    windy = planner.find_targets(f, M.StressModel(), p["thresholds"], spray_ok=False, deferred=deferred)
    assert any(a == "spray" for _, _, a in calm)
    assert not any(a == "spray" for _, _, a in windy) and len(deferred) == sum(1 for _, _, a in calm if a == "spray")
    assert any(a == "irrigate" for _, _, a in windy)  # irrigation carries on


def test_traffic_demands_more_clearance_in_gusty_air_and_still_ends_safe():
    p = load_policy()
    r = random.Random(4)
    tg = sorted([(r.random(), (r.randrange(2, 24), r.randrange(0, 24)), "irrigate") for _ in range(120)], reverse=True)
    ms = planner.plan(tg, p, sorties=1, wind=W.Wind(6.0, 11.0, 270))
    rep = traffic.check(ms, p)
    assert rep["steps"] == 0 and (rep["min_ratio"] is None or rep["min_ratio"] >= 1.0)


# ------------------------------------------------------------------ the autopilot
def stormy(monkeypatch, **kw):
    def fake(policy, day, state_dir=None, **_):
        w = Weather(0.0, 20.0, 10.0, 4.0, "synthetic", **kw)
        return w, [w, w, w], "synthetic"

    monkeypatch.setattr("agridrone.weather.get_weather", fake)


def test_agent_grounds_the_fleet_in_a_gale_and_reports_it(tmp_path, monkeypatch):
    p = load_policy()
    p["field"]["size"] = 16
    stormy(monkeypatch, wind_ms=14.0, gust_ms=20.0, wind_from_deg=270.0)
    for _ in range(3):
        rec = run_cycle(p, tmp_path)
        assert rec["ok"] and rec["grounded_by_wind"] and rec["executed"] == 0 and rec["sorties"] == 0
    assert any(a["kind"] == "grounded_by_wind" for a in store.read_jsonl("audit.jsonl", tmp_path))


def test_agent_defers_spraying_but_keeps_irrigating_in_a_moderate_wind(tmp_path, monkeypatch):
    p = load_policy()
    p["field"]["size"] = 16
    stormy(monkeypatch, wind_ms=8.0, gust_ms=10.0, wind_from_deg=270.0)  # x0.75 flight window = 6 m/s: above spray, below flight limit
    flown = 0
    for _ in range(70):
        rec = run_cycle(p, tmp_path)
        assert rec["ok"] and not rec["grounded_by_wind"] and rec["wind_ms"] == 6.0
        flown += rec["executed"]
    farm = store.load_farm(p["field"]["seed"], 16, tmp_path)
    assert flown > 0  # it keeps flying (watering) in a wind that stops spraying
    assert farm.chem_used == 0 and farm.water_used > 0  # ...and not one patch was sprayed in a wind above the drift limit
    assert all(a["kind"] != "safety_block" for a in store.read_jsonl("audit.jsonl", tmp_path))


def test_a_normal_synthetic_season_runs_with_wind_and_never_hits_a_safety_block(tmp_path):
    p = load_policy()
    p["field"]["size"] = 16
    grounded = 0
    for _ in range(80):
        rec = run_cycle(p, tmp_path)
        assert rec["ok"], rec
        grounded += rec["grounded_by_wind"]
    assert grounded < 40  # not grounded half the time
    assert not [a for a in store.read_jsonl("audit.jsonl", tmp_path) if a["kind"] == "safety_block"]


def test_planner_and_gate_agree_exactly_even_for_awkward_unrounded_wind():
    """Found by the season benchmark: the wind stored on a mission is rounded, and planning with the unrounded value let a plan exceed
    its battery budget by 0.04 Wh, which the gate (correctly) refused. Both must use identical numbers."""
    from agridrone import fleet as F

    p = load_policy()
    for seed in range(8):
        f = F.Fleet(p)
        f.d[0]["health"] = 0.993
        f.d[0]["soc_wh"] = f.cap(0)
        wind = W.Wind(3.8123456, 6.4789123, 239.04321)
        ms = planner.plan(targets(300, seed), p, sorties=3, fleet=f, wind=wind)
        assert all(m.energy_wh <= f.budget(m.drone, m.sortie) + 1e-9 for m in ms)
        before = [m.energy_wh for m in ms]
        ms, _ = safety.repair(ms, p)
        assert [m.energy_wh for m in ms] == before  # repair recomputes the same energy: nothing drifts
        assert f.simulate(ms)["violations"] == []
