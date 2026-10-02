"""Safety gate. Every mission passes through here; the LLM/planner can never bypass it."""
from dataclasses import dataclass, field
import math


@dataclass
class Mission:
    drone: int
    altitude_m: int
    targets: list = field(default_factory=list)  # [(cell, action)]
    energy_wh: float = 0.0


def mission_energy(start, targets, fleet):
    e, cur = 0.0, start
    for cell, _ in targets:
        e += (abs(cell[0] - cur[0]) + abs(cell[1] - cur[1])) * fleet["wh_per_cell_move"] + fleet["wh_per_cell_action"]
        cur = cell
    e += (abs(cur[0] - start[0]) + abs(cur[1] - start[1])) * fleet["wh_per_cell_move"]  # return leg
    return e


def validate(missions, policy):
    """Returns list of violations (empty == safe)."""
    v = []
    if policy.get("kill_switch"):
        return ["kill_switch engaged"]
    fleet, nofly = policy["fleet"], {tuple(c) for c in policy["no_fly_cells"]}
    usable = fleet["battery_wh"] * (1 - fleet["min_reserve_pct"] / 100)
    alts = [m.altitude_m for m in missions]
    if len(set(alts)) != len(alts):
        v.append("altitude layers not unique (collision risk)")
    for a, b in zip(sorted(alts), sorted(alts)[1:]):
        if b - a < fleet["min_separation_m"]:
            v.append(f"vertical separation {b - a}m < {fleet['min_separation_m']}m")
    for m in missions:
        if m.energy_wh > usable:
            v.append(f"drone {m.drone} energy {m.energy_wh:.1f}Wh exceeds usable {usable:.1f}Wh")
        for cell, _ in m.targets:
            if tuple(cell) in nofly:
                v.append(f"drone {m.drone} targets no-fly cell {cell}")
    return v


def repair(missions, policy):
    """Drop offending targets / missions until valid. Returns (missions, dropped_count)."""
    fleet, nofly = policy["fleet"], {tuple(c) for c in policy["no_fly_cells"]}
    usable = fleet["battery_wh"] * (1 - fleet["min_reserve_pct"] / 100)
    dropped = 0
    for m in missions:
        keep = [t for t in m.targets if tuple(t[0]) not in nofly]
        dropped += len(m.targets) - len(keep)
        m.targets = keep
        while m.targets and mission_energy((0, 0), m.targets, fleet) > usable:
            m.targets.pop()
            dropped += 1
        m.energy_wh = mission_energy((0, 0), m.targets, fleet) if m.targets else 0.0
    return [m for m in missions if m.targets], dropped
