"""Greedy priority-weighted, load-balancing mission planner (minimises the longest sortie, not total energy). One altitude layer per drone => separation by construction."""
from .safety import Mission, mission_energy


def find_targets(farm, model, thr):
    """Cells needing action, ranked by severity. Observation noise included (as in the field)."""
    out = []
    for cell in farm.cells:
        o = farm.observe(cell)
        stressed = model.prob(o["ndvi"], o["moisture"], o["pest"]) > 0.5
        if o["pest"] > thr["pest_spray"]:
            out.append((o["pest"] + 0.5, cell, "spray"))
        elif stressed and o["moisture"] < thr["moisture_irrigate"]:
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
