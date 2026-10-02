from agridrone import economics as E
from agridrone.config import load_policy


def pol(**kw):
    p = load_policy()
    p["economics"] = {**p["economics"], **kw}
    return p


def test_season_arithmetic():
    p = pol(
        patch_ha=1.0,
        crop_value_per_ha=1000.0,
        water_per_mm_ha=1.0,
        spray_per_patch=2.0,
        drone_cost=0.0,
        drone_hour_ops=0.0,
        operator_per_season=0.0,
    )
    s = E.season(p, patches=100, yield_index=0.9, water_mm=10.0, sprays_per_patch=2.0, drones=0)
    assert s["hectares"] == 100.0 and s["revenue"] == 90000 and s["water_cost"] == 1000 and s["spray_cost"] == 400 and s["profit"] == 88600


def test_fleet_cost_includes_amortisation_hours_recoveries_and_the_operator():
    p = pol(drone_cost=3650.0, drone_life_years=1.0, drone_hour_ops=2.0, recovery_cost=100.0, operator_per_season=50.0)
    s = E.season(p, 100, 1.0, 0.0, 0.0, drones=2, flight_hours=10.0, recoveries=1, days=100)
    assert s["fleet_cost"] == round(2 * 3650.0 * 100 / 365 + 20 + 100 + 50)
    assert E.season(p, 100, 1.0, 0.0, 0.0, drones=0)["fleet_cost"] == 0  # no fleet, no fleet costs


def test_compare_covers_every_strategy_and_the_ai_pays_for_its_fleet():
    b = {
        "days": 120,
        "patches": 576,
        "strategies": [
            {"id": "none", "name": "Do nothing", "yield": 0.88, "water_mm": 0, "sprays": 0.0},
            {"id": "calendar", "name": "Fixed schedule", "yield": 1.0, "water_mm": 612, "sprays": 8.0},
            {"id": "agent-3", "name": "AI", "yield": 0.997, "water_mm": 126, "sprays": 0.6, "flight_hours": 50.0},
        ],
    }
    rows = {r["id"]: r for r in E.compare(b, pol())}
    assert rows["none"]["fleet_cost"] == 0 and rows["calendar"]["fleet_cost"] == 0 and rows["agent-3"]["fleet_cost"] > 0
    assert rows["agent-3"]["profit"] > rows["calendar"]["profit"]
    days = E.payback_days(pol(), b)
    assert days is not None and 0 < days < 365


def test_payback_is_none_when_the_ai_does_not_beat_the_schedule():
    b = {
        "days": 120,
        "patches": 100,
        "strategies": [
            {"id": "calendar", "name": "Fixed", "yield": 1.0, "water_mm": 10, "sprays": 0.0},
            {"id": "agent-3", "name": "AI", "yield": 0.9, "water_mm": 10, "sprays": 0.0, "flight_hours": 50.0},
        ],
    }
    assert E.payback_days(pol(), b) is None
