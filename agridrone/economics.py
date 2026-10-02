"""Money view: what a season costs and earns, so the question is "does it pay?" and not just "does it work?".

All prices live in policy.economics and are PLACEHOLDERS to replace with your own (the defaults are plausible mid-western corn numbers).
The agronomic inputs (yield index, water mm, sprays) come from the benchmark/simulation, so every figure inherits the simulation's caveats:
this is a decision aid, not a forecast."""

import math

DEFAULTS = {
    "patch_ha": 0.25,  # one 50 m x 50 m patch
    "crop_value_per_ha": 2200.0,  # revenue at yield index 1.0
    "water_per_mm_ha": 0.30,  # pumping + water, per mm per hectare
    "spray_per_patch": 4.5,  # chemical + application for one patch (0.25 ha)
    "drone_cost": 3500.0,
    "drone_life_years": 4.0,
    "drone_hour_ops": 2.0,  # batteries, wear, electricity per flight hour
    "recovery_cost": 150.0,  # a person recovering a drone that landed in a field
    "operator_per_season": 400.0,  # supervision / maintenance time per season for the fleet
}


def cfg(policy):
    return {**DEFAULTS, **policy.get("economics", {})}


def season(policy, patches, yield_index, water_mm, sprays_per_patch, drones=0, flight_hours=0.0, recoveries=0, days=120):
    """-> dict of money numbers for one season on `patches` patches (water_mm is mm per patch, sprays_per_patch a count)."""
    e = cfg(policy)
    ha = patches * e["patch_ha"]
    revenue = yield_index * e["crop_value_per_ha"] * ha
    water = water_mm * e["water_per_mm_ha"] * ha
    spray = sprays_per_patch * patches * e["spray_per_patch"]
    fleet = (
        drones * e["drone_cost"] / e["drone_life_years"] * (days / 365.0)
        + flight_hours * e["drone_hour_ops"]
        + recoveries * e["recovery_cost"]
    )
    fleet += e["operator_per_season"] if drones else 0.0
    cost = water + spray + fleet
    return {
        "hectares": round(ha, 1),
        "revenue": round(revenue),
        "water_cost": round(water),
        "spray_cost": round(spray),
        "fleet_cost": round(fleet),
        "cost": round(cost),
        "profit": round(revenue - cost),
        "profit_per_ha": round((revenue - cost) / ha, 1),
    }


def compare(bench, policy, drones=3):
    """Economics for each benchmark strategy -> list of dicts (name, profit, ...). `bench` is docs/benchmark.json."""
    patches = bench["patches"]
    rows = []
    for s in bench["strategies"]:
        ai = s["id"] in ("agent-1", "agent-3", "thresholds")
        rows.append(
            {
                "id": s["id"],
                "name": s["name"],
                **season(
                    policy,
                    patches,
                    s["yield"],
                    s["water_mm"],
                    s["sprays"],
                    drones if ai else 0,
                    s.get("flight_hours", 60.0) if ai else 0.0,
                    0,
                    bench["days"],
                ),
            }
        )
    return rows


def payback_days(policy, bench, drones=3):
    """Days of operation until the fleet's purchase price is recovered by its savings over the fixed schedule (None if it never is)."""
    rows = {r["id"]: r for r in compare(bench, policy, drones)}
    e = cfg(policy)
    gain = rows["agent-3"]["profit"] - rows["calendar"]["profit"]  # per season, fleet running cost already deducted
    if gain <= 0:
        return None
    capex = drones * e["drone_cost"]
    per_day = gain / bench["days"]
    return math.ceil(capex / per_day)
