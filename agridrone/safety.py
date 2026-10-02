"""Safety gate. Every mission passes through here; the LLM/planner can never bypass it.

Checks: kill switch, battery reserve, no-fly cells AND the straight flight legs between waypoints (a drone must not
cross a no-fly zone on its way to a legal target), vertical separation between drones that fly at the same time."""

from dataclasses import dataclass, field

from . import traffic

PAD = 0.3  # lateral safety margin around a no-fly cell, in cell widths


@dataclass
class Mission:
    drone: int
    altitude_m: int
    targets: list = field(default_factory=list)  # [(cell, action)]; action "via" = detour waypoint, no treatment
    energy_wh: float = 0.0
    sortie: int = 0  # missions with the same sortie index fly concurrently
    t0: int = 0  # launch delay within the sortie (s), set by traffic.schedule
    hold: int = 0  # loiter above the pad before descending (s), set by traffic.schedule
    pad: int = -1  # launch pad index (default: the drone's own index)


def mission_energy(start, targets, fleet):
    e, cur = 0.0, start
    for cell, action in targets:
        e += (abs(cell[0] - cur[0]) + abs(cell[1] - cur[1])) * fleet["wh_per_cell_move"] + (
            0.0 if action == "via" else fleet["wh_per_cell_action"]
        )
        cur = cell
    e += (abs(cur[0] - start[0]) + abs(cur[1] - start[1])) * fleet["wh_per_cell_move"]  # return leg
    return e


def leg_clear(a, b, nofly):
    """True if the straight leg a->b stays outside every (padded) no-fly cell."""
    if not nofly:
        return True
    steps = int(max(abs(a[0] - b[0]), abs(a[1] - b[1])) * 6) + 1
    for i in range(steps + 1):
        x = a[0] + (b[0] - a[0]) * i / steps
        y = a[1] + (b[1] - a[1]) * i / steps
        for cx, cy in nofly:
            if abs(x - cx) <= 0.5 + PAD and abs(y - cy) <= 0.5 + PAD:
                return False
    return True


def _clusters(nofly):
    """Connected groups (8-neighbourhood) of no-fly cells -> list of bounding boxes (x0, y0, x1, y1)."""
    left, boxes = set(nofly), []
    while left:
        stack = [left.pop()]
        grp = list(stack)
        while stack:
            x, y = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in left:
                        left.remove(n)
                        stack.append(n)
                        grp.append(n)
        xs, ys = [g[0] for g in grp], [g[1] for g in grp]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def connect(a, b, nofly, size):
    """Waypoints (excluding a, including b) to fly a -> b without entering a no-fly zone; None if impossible.
    Tries the direct leg, then one detour waypoint around the corners of each no-fly cluster."""
    if tuple(a) == tuple(b) or leg_clear(a, b, nofly):
        return [tuple(b)]
    best = None
    for x0, y0, x1, y1 in _clusters(nofly):
        for vx, vy in ((x0 - 1, y0 - 1), (x1 + 1, y0 - 1), (x1 + 1, y1 + 1), (x0 - 1, y1 + 1)):
            v = (min(max(vx, 0), size - 1), min(max(vy, 0), size - 1))
            if v in nofly or not leg_clear(a, v, nofly) or not leg_clear(v, b, nofly):
                continue
            d = abs(a[0] - v[0]) + abs(a[1] - v[1]) + abs(v[0] - b[0]) + abs(v[1] - b[1])
            if best is None or d < best[0]:
                best = (d, v)
    return [best[1], tuple(b)] if best else None


def validate(missions, policy):
    """Returns list of violations (empty == safe)."""
    v = []
    if policy.get("kill_switch"):
        return ["kill_switch engaged"]
    fleet, nofly = policy["fleet"], {tuple(c) for c in policy["no_fly_cells"]}
    usable = fleet["battery_wh"] * (1 - fleet["min_reserve_pct"] / 100)
    for s in sorted({m.sortie for m in missions}):  # only drones airborne together must be separated
        alts = [m.altitude_m for m in missions if m.sortie == s]
        if len(set(alts)) != len(alts):
            v.append(f"sortie {s}: altitude layers not unique (collision risk)")
        for a, b in zip(sorted(alts), sorted(alts)[1:]):
            if b - a < fleet["min_separation_m"]:
                v.append(f"sortie {s}: vertical separation {b - a}m < {fleet['min_separation_m']}m")
        # time-resolved 3D check: catches climbs/descents/overflights that altitude layers alone cannot
        rep = traffic.check([m for m in missions if m.sortie == s], policy, max_report=3)
        for c in rep["conflicts"]:
            v.append(
                f"sortie {s}: drones {c['drones'][0]} and {c['drones'][1]} conflict at t={c['t']}s "
                f"({c['horizontal_m']} m apart sideways, {c['vertical_m']} m vertically)"
            )
    for m in missions:
        if m.energy_wh > usable:
            v.append(f"drone {m.drone} energy {m.energy_wh:.1f}Wh exceeds usable {usable:.1f}Wh")
        pts = [(0, 0)] + [tuple(c) for c, _ in m.targets] + [(0, 0)]
        for cell, _ in m.targets:
            if tuple(cell) in nofly:
                v.append(f"drone {m.drone} targets no-fly cell {cell}")
        for a, b in zip(pts, pts[1:]):
            if not leg_clear(a, b, nofly):
                v.append(f"drone {m.drone} leg {a}->{b} crosses a no-fly zone")
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
