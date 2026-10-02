import tempfile

from agridrone import economics, planner, quality, weather
from agridrone.config import load_policy
from agridrone.sim import Farm


def test_satellite_check_corrects_a_drifting_sensor():
    f, d = Farm.create(12, 4), tempfile.mkdtemp()
    cell = (5, 5)
    f.faults[cell] = {"mode": "bias", "start": 0, "bias": 0.15, "frozen": None}
    seen = None
    for _ in range(20):
        f.step(weather.synthetic(f.day + 1, 4))
        raw = f.observe_all()
        out, rep = quality.clean(raw, d, reference=f.satellite_pass() or {})
        seen = ({o["cell"]: o for o in raw}[cell]["moisture"], {o["cell"]: o for o in out}[cell]["moisture"], f.cells[cell].moisture, rep)
    raw_m, fixed_m, truth, rep = seen
    assert cell in set(map(tuple, rep["cells"]))
    assert abs(fixed_m - truth) < abs(raw_m - truth)  # the correction moved the reading toward the truth


def test_satellite_check_leaves_healthy_fields_alone():
    f, d = Farm.create(12, 4), tempfile.mkdtemp()
    flagged = 0
    for _ in range(30):
        f.step(weather.synthetic(f.day + 1, 4))
        _, rep = quality.clean(f.observe_all(), d, reference=f.satellite_pass() or {})
        flagged += rep["reasons"].get("satellite_bias", 0)
    assert flagged <= 0.01 * 144 * 30  # false alarms stay under 1% of sensor-days


def test_blackout_lookahead_widens_the_horizon():
    p = load_policy()
    calm = weather.Weather(0, 20, 10, 3, wind_ms=2, gust_ms=3)
    gale = weather.Weather(0, 20, 10, 3, wind_ms=12, gust_ms=16)
    assert planner.blackout_days([calm, calm], p, "flight") == 0
    assert planner.blackout_days([gale, gale, calm], p, "flight") == 2
    f = Farm.create(12, 1)
    for c in f.cells.values():
        c.moisture = 0.32
    thr = p["thresholds"]
    base = planner.find_targets(f, __import__("agridrone.model", fromlist=["x"]).StressModel(), thr)
    look = planner.find_targets(f, __import__("agridrone.model", fromlist=["x"]).StressModel(), thr, gap_irrigate=2)
    assert len(look) > len(base)  # patches about to dry out during the blackout are treated now


def test_smart_farmer_is_not_counted_as_ai():
    p = load_policy()
    bench = {
        "patches": 100,
        "days": 120,
        "strategies": [
            {"id": "calendar", "name": "c", "yield": 1.0, "water_mm": 600, "sprays": 3},
            {"id": "triggered", "name": "t", "yield": 0.99, "water_mm": 120, "sprays": 0.5},
            {"id": "agent-3", "name": "a", "yield": 0.99, "water_mm": 110, "sprays": 0.5, "flight_hours": 20},
        ],
    }
    rows = {r["id"]: r for r in economics.compare(bench, p, 3)}
    assert rows["triggered"]["fleet_cost"] == 0 and rows["agent-3"]["fleet_cost"] > 0
    assert economics.payback_days(p, bench, 3, "triggered") is None  # same yield, trivially less water: the drones never pay back


def test_sizing_scales_with_farm_size():
    p = load_policy()
    bench = {
        "patches": 576,
        "days": 120,
        "strategies": [
            {"id": s, "name": s, "yield": 0.99, "water_mm": 110, "sprays": 0.6, "flight_hours": 60}
            for s in ("calendar", "triggered", "agent-3")
        ],
    }
    small, big = economics.sizing(p, bench, 20), economics.sizing(p, bench, 2000)
    assert small["drones"] >= 1 and big["drones"] > small["drones"]
