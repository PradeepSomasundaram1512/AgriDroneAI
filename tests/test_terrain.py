import copy

from agridrone import planner, safety, terrain
from agridrone import wind as W
from agridrone.config import load_policy


def pol(**t):
    p = copy.deepcopy(load_policy())
    p["terrain"] = t
    return p


def test_flat_world_changes_nothing():
    p = pol()
    f = p["fleet"]
    route = [((5, 5), "irrigate"), ((9, 3), "irrigate")]
    assert safety.mission_energy((0, 0), route, f, None, 30, 0.2, terrain.from_policy(p)) == safety.mission_energy((0, 0), route, f)


def test_hills_cost_energy_and_descents_are_free():
    p = pol(relief_m=40.0, seed=3)
    t = terrain.from_policy(p)
    a, b = (0, 0), (14, 9)
    up, down = t.climb_m(a, b), t.climb_m(b, a)
    assert up > 0 and down > 0
    assert abs((up - down) - (t.z(*b) - t.z(*a))) < 1e-6  # net climb = elevation difference; ups and downs both counted
    flat = safety.mission_energy((0, 0), [(b, "irrigate")], p["fleet"])
    hilly = safety.mission_energy((0, 0), [(b, "irrigate")], p["fleet"], None, 30, 0.2, t)
    assert hilly > flat


def test_planner_and_gate_agree_on_hill_energy():
    p = pol(relief_m=60.0, seed=5)
    targets = [(1.0 - i * 0.01, (3 + i * 2 % 17, 2 + i * 5 % 19), "irrigate") for i in range(14)]
    ms = planner.plan(targets, p, sorties=1, wind=W.Wind(3, 4, 270))
    assert ms
    ms, _ = safety.repair(ms, p)
    assert safety.validate(ms, p) == []  # the gate recomputes with the same hills: no energy disagreement
    flat = planner.plan(targets, pol(), sorties=1, wind=W.Wind(3, 4, 270))
    assert sum(m.energy_wh for m in ms) >= 0.99 * sum(m.energy_wh for m in flat) * 0.5  # sanity: planned energy is real


def test_obstacle_blocks_low_layers_only():
    p = pol(obstacles=[[5, 5, 45.0]], clearance_m=10.0)
    assert (5, 5) in terrain.blocked(p, 30) and (5, 5) in terrain.blocked(p, 50)
    assert (5, 5) not in terrain.blocked(p, 70)  # 70 - 45 >= 10
    t = [(1.0, (5, 5), "irrigate"), (0.5, (8, 8), "irrigate")]
    ms, _ = safety.repair(planner.plan(t, p, sorties=1), p)
    for m in ms:
        cells = [tuple(c) for c, _ in m.targets]
        if m.altitude_m < 55:
            assert (5, 5) not in cells  # cannot visit a patch under a 45 m obstacle from a low layer
    assert safety.validate(ms, p) == []


def test_obstacle_forces_detour_for_low_drone_not_high():
    p = pol(obstacles=[[6, 3, 60.0]])
    blocked_low, blocked_high = terrain.blocked(p, 30), terrain.blocked(p, 100)
    assert safety.leg_clear((0, 0), (12, 6), blocked_high)
    assert not safety.leg_clear((0, 0), (12, 6), blocked_low)
    hop = safety.connect((0, 0), (12, 6), blocked_low, 24)
    assert hop and len(hop) == 2  # one corner waypoint around the obstacle


def test_elevation_grid_is_used():
    g = [[0.0] * 24 for _ in range(24)]
    for x in range(24):
        for y in range(24):
            g[x][y] = float(x)  # a uniform 1 m per cell slope along x
    t = terrain.from_policy(pol(elevation=g))
    assert abs(t.z(3.5, 2.0) - 3.5) < 1e-9
    assert abs(t.climb_m((0, 5), (10, 5)) - 10.0) < 1e-6 and t.climb_m((10, 5), (0, 5)) == 0.0
