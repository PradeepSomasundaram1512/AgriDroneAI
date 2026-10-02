"""Greedy priority-weighted, load-balancing mission planner (minimises the longest sortie, not total energy). One altitude layer per drone => separation by construction."""
from . import model as M
from .safety import Mission, mission_energy


def find_targets(farm, model, thr):
    """Cells needing action, ranked by severity. Observation noise included (as in the field)."""
    out = []
    obs = [farm.observe(c) for c in farm.cells]
    med = M.median([o["ndvi"] for o in obs])
    for o in obs:
        cell = o["cell"]
        stressed = model.prob(o["ndvi"] - med, o["moisture"], o["pest"]) > 0.5
        # Fail-safe: plain agronomic thresholds ALWAYS act, whatever the model says (a stale model must not stop
        # watering/spraying). The model only adds early detection just inside the thresholds.
        if o["pest"] > thr["pest_spray"] or (stressed and o["pest"] > thr["pest_spray"] - 0.06):
            out.append((o["pest"] + 0.5, cell, "spray"))
        elif o["moisture"] < thr["moisture_irrigate"] or (stressed and o["moisture"] < thr["moisture_irrigate"] + 0.04):
            out.append(((thr["moisture_irrigate"] - o["moisture"]) + 0.3, cell, "irrigate"))
    out.sort(reverse=True)
    return out


def plan(targets, policy):
    fleet = policy["fleet"]
    n = fleet["drones"]
    missions = [Mission(drone=i, altitude_m=30 + i * (fleet["min_separation_m"] + 5)) for i in range(n)]
    pos = [(0, 0)] * n
    usable = fleet["battery_wh"] * (1 - fleet["min_reserve_pct"] / 100)
    for _, cell, action in targets:
        best, best_cost = None, None
        for i, m in enumerate(missions):
            trial = m.targets + [(cell, action)]
            e = mission_energy((0, 0), trial, fleet)
            if e > usable:
                continue
            cost = e  # balance load: pick the drone whose total mission stays shortest (energy ~ flight time)
            if best is None or cost < best_cost:
                best, best_cost = i, cost
        if best is not None:
            missions[best].targets.append((cell, action))
            missions[best].energy_wh = mission_energy((0, 0), missions[best].targets, fleet)
    return [m for m in missions if m.targets]
