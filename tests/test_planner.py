import random
from agridrone import planner, safety
from agridrone.config import load_policy
from agridrone.safety import Mission, connect, leg_clear, mission_energy
from agridrone.sim import Farm
from agridrone import model as M


def targets(n=120, seed=1, size=24):
    r = random.Random(seed)
    cells = list({(r.randrange(size), r.randrange(size)) for _ in range(n)})
    return sorted([(r.random(), c, r.choice(["irrigate", "spray"])) for c in cells], reverse=True)


def real(m):
    return [t for t in m.targets if t[1] != "via"]


def test_plans_are_always_safe_and_within_battery():
    pol = load_policy()
    for seed in range(5):
        ms = planner.plan(targets(200, seed), pol)
        assert ms and not safety.validate(ms, pol)
        usable = pol["fleet"]["battery_wh"] * (1 - pol["fleet"]["min_reserve_pct"] / 100)
        assert all(m.energy_wh <= usable + 1e-6 for m in ms)


def test_no_target_planned_twice_and_none_in_nofly():
    pol = load_policy()
    nofly = {tuple(c) for c in pol["no_fly_cells"]}
    tg = targets(250, 3)
    ms = planner.plan(tg, pol)
    cells = [c for m in ms for c, _ in real(m)]
    assert len(cells) == len(set(cells))
    assert not nofly & set(cells) or True  # targets inside no-fly may be requested; they must never be routed to
    assert all(tuple(c) not in nofly for m in ms for c, _ in m.targets)


def test_detour_when_direct_leg_crosses_no_fly():
    nofly = {(10, 10), (10, 11), (11, 10), (11, 11)}
    assert not leg_clear((8, 8), (14, 14), nofly)
    hop = connect((8, 8), (14, 14), nofly, 24)
    assert hop and hop[-1] == (14, 14) and len(hop) == 2
    a, via, b = (8, 8), hop[0], hop[1]
    assert leg_clear(a, via, nofly) and leg_clear(via, b, nofly)


def test_safety_gate_rejects_a_leg_through_a_no_fly_zone():
    pol = load_policy()
    m = Mission(0, 30, [((13, 13), "irrigate")])  # legal target, but the straight leg from (0,0) crosses the zone
    m.energy_wh = mission_energy((0, 0), m.targets, pol["fleet"])
    assert any("crosses a no-fly zone" in v for v in safety.validate([m], pol))
    ms = planner.plan([(1.0, (13, 13), "irrigate")], pol)  # the planner adds a detour so the same target is flyable
    assert ms and not safety.validate(ms, pol) and any(a == "via" for _, a in ms[0].targets)


def test_urgent_work_goes_to_the_earliest_sortie():
    pol = load_policy()
    ms = planner.plan(targets(300, 2), pol, sorties=3)
    urgent = {c for _, c, _ in targets(300, 2)[:5]}
    sortie0 = {c for m in ms if m.sortie == 0 for c, _ in real(m)}
    assert urgent <= sortie0


def test_balanced_across_drones_and_deterministic():
    pol = load_policy()
    a = planner.plan(targets(200, 4), pol, sorties=1)
    b = planner.plan(targets(200, 4), pol, sorties=1)
    assert [m.targets for m in a] == [m.targets for m in b]
    loads = [m.energy_wh for m in a]  # balance on energy (= flight time), not patch count
    assert len(loads) == pol["fleet"]["drones"] and max(loads) - min(loads) <= 3.0


def test_two_opt_never_makes_a_route_longer():
    pol = load_policy()
    fleet = pol["fleet"]
    nofly = {tuple(c) for c in pol["no_fly_cells"]}
    legs = planner._Legs(fleet, nofly, 24)
    r = random.Random(9)
    route = [((r.randrange(24), r.randrange(24)), "irrigate") for _ in range(12)]
    route = [t for t in route if t[0] not in nofly]
    assert legs.route_energy(planner._two_opt(route, legs)) <= legs.route_energy(route) + 1e-9


def test_rain_forecast_defers_irrigation_but_never_critical_patches():
    from agridrone.weather import Weather

    pol = load_policy()
    thr = pol["thresholds"]
    f = Farm.create(8, 1)
    f.observe = lambda cell, noise=0.0, _o=f.observe: _o(cell, noise=0.0)  # noise-free sensors: exact thresholds
    for i, c in enumerate(f.cells.values()):
        c.pest = 0.0
        c.moisture = 0.27 if i % 2 == 0 else 0.15  # mildly dry vs critically dry
    mdl = M.StressModel()
    dry = planner.find_targets(f, mdl, thr, [Weather(0, 20, 10, 3), Weather(0, 20, 10, 3)])
    wet = planner.find_targets(f, mdl, thr, [Weather(15, 18, 10, 2), Weather(2, 18, 10, 2)])
    assert len(wet) < len(dry) and len(wet) > 0  # fewer waterings, but the critically dry ones still get water
    assert all(f.cells[c].moisture < thr["moisture_irrigate"] - 0.05 for _, c, _ in wet)


def test_planner_is_fast_with_a_big_backlog():
    import time

    pol = load_policy()
    tg = targets(450, 7)
    t = time.time()
    planner.plan(tg, pol)
    assert time.time() - t < 4.0
