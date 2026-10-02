"""Mission planner v2.

* find_targets: rank patches by urgency. Plain agronomic thresholds ALWAYS act (a stale model can't stop watering);
  the model adds early detection; the weather forecast defers irrigation when rain is coming and pre-empts a dry-down.
* plan: multi-drone, multi-sortie (battery swap) planning. Cheapest-insertion builds routes, 2-opt shortens them,
  detour waypoints keep legs out of no-fly zones, and load is balanced so no drone is left idle.
Sortie 0 flies first and gets the most urgent work. Missions flying at the same time use distinct altitude layers."""

import math

from . import model as M
from . import traffic
from . import wind as W
from .safety import Mission, connect, mission_energy

MAX_CONSECUTIVE_MISSES = 25  # stop searching once this many targets in a row fail to fit anywhere


def find_targets(farm, model, thr, forecast=None, obs=None, spray_ok=True, deferred=None):
    """-> [(priority, cell, action)] sorted most urgent first. `obs`: pre-cleaned whole-field observations (else raw sensors)."""
    forecast = forecast or []
    rain_soon = sum(w.rain_mm for w in forecast[:2])
    out = []
    obs = obs if obs is not None else [farm.observe(c) for c in farm.cells]
    med = M.median([o["ndvi"] for o in obs])
    for o in obs:
        cell = o["cell"]
        stressed = model.prob(o["ndvi"] - med, o["moisture"], o["pest"]) > 0.5
        if o["pest"] > thr["pest_spray"] or (stressed and o["pest"] > thr["pest_spray"] - 0.06):
            if spray_ok:
                out.append((o["pest"] + 0.5, cell, "spray"))
            elif deferred is not None:  # too windy to spray without drift: it waits for a calmer day (irrigation is unaffected)
                deferred.append(cell)
            continue
        dry_now = o["moisture"] < thr["moisture_irrigate"]
        # predicted dry-down: tomorrow's loss ~ crop ET / root-zone depth (about 0.012-0.015 per day)
        dry_soon = o["moisture"] - 0.014 < thr["moisture_irrigate"]
        if rain_soon >= 10:  # meaningful rain forecast: only rescue critically dry patches
            if o["moisture"] < thr["moisture_irrigate"] - 0.05:
                out.append(((thr["moisture_irrigate"] - o["moisture"]) + 0.3, cell, "irrigate"))
        elif dry_now or (stressed and o["moisture"] < thr["moisture_irrigate"] + 0.04) or (dry_soon and rain_soon < 5):
            out.append(((thr["moisture_irrigate"] - o["moisture"]) + 0.3, cell, "irrigate"))
    out.sort(reverse=True)
    return out


class _Legs:
    """Cached leg costs. A leg is the cheapest no-fly-safe path between two points: (energy, detour waypoints) or None.
    Route energy is then a sum over legs, so an insertion only needs the 3 legs it changes (O(1), not O(route))."""

    def __init__(self, fleet, nofly, size, wvec=(0.0, 0.0), conn=None):
        """wvec: wind vector (m/s) at THIS drone's altitude: leg energy depends on direction, so costs are per drone.
        conn: detour-geometry cache shared by all drones (geometry does not depend on wind)."""
        self.f, self.nofly, self.size, self.c, self.wvec = fleet, nofly, size, {}, wvec
        self.air = float(fleet.get("speed_ms", 10.0))
        self.conn = conn if conn is not None else {}

    def leg(self, a, b):
        k = (a, b)
        if k not in self.c:
            if k not in self.conn:
                self.conn[k] = connect(a, b, self.nofly, self.size)
            hop = self.conn[k]
            if hop is None:
                self.c[k] = None
            else:
                pts, e = [a] + hop, 0.0
                for p, q in zip(pts, pts[1:]):
                    f = W.leg_factor(q[0] - p[0], q[1] - p[1], self.air, self.wvec)  # headwind costs more, tailwind less
                    if f is None:
                        e = None  # cannot make headway on this leg in this wind
                        break
                    e += math.hypot(q[0] - p[0], q[1] - p[1]) * self.f["wh_per_cell_move"] * f  # true straight-line distance
                self.c[k] = None if e is None else (e, hop[:-1])
        return self.c[k]

    def route_energy(self, route):
        pts, e = [(0, 0)] + [tuple(c) for c, _ in route] + [(0, 0)], len(route) * self.f["wh_per_cell_action"]
        for a, b in zip(pts, pts[1:]):
            l = self.leg(a, b)
            if l is None:
                return float("inf")
            e += l[0]
        return e

    def expand(self, route):
        """Real targets + detour waypoints (action "via") so no leg crosses a no-fly zone."""
        out, cur = [], (0, 0)
        for cell, action in route:
            out += [(h, "via") for h in self.leg(cur, tuple(cell))[1]] + [(tuple(cell), action)]
            cur = tuple(cell)
        return out + [(h, "via") for h in self.leg(cur, (0, 0))[1]]


def _two_opt(route, legs):
    """Shorten a route by reversing segments while energy (incl. detours) drops."""
    best, bc, improved = list(route), legs.route_energy(route), True
    while improved and len(best) > 2:
        improved = False
        for i in range(len(best) - 1):
            for j in range(i + 1, len(best)):
                cand = best[:i] + best[i : j + 1][::-1] + best[j + 1 :]
                cc = legs.route_energy(cand)
                if cc < bc - 1e-9:
                    best, bc, improved = cand, cc, True
    return best


def plan(targets, policy, charge=None, sorties=None, allow_hold=True, fleet=None, wind=None):
    """targets: [(priority, cell, action)]. fleet: optional fleet.Fleet (battery state + charging); else every sortie starts full.
    charge: legacy {drone: battery %} for sortie 0. -> [Mission]."""
    fleet_cfg, size = policy["fleet"], policy["field"]["size"]
    fleet_state = fleet
    fleet = fleet_cfg
    nofly = {tuple(c) for c in policy["no_fly_cells"]}
    n, ns = fleet["drones"], sorties or fleet.get("sorties_per_day", 1)
    charge, act_e = charge or {}, fleet["wh_per_cell_action"]
    if (
        wind is not None
    ):  # the wind stored on every Mission is rounded: plan with exactly those numbers so planner and gate agree to the last bit
        wind = W.Wind.from_tuple(wind.as_tuple())
    design = wind.design() if wind is not None else None  # plan energy against sustained wind + part of the gust margin
    shear, conn = W.limits(policy)["shear_exponent"], {}
    alt = [30 + i * (fleet["min_separation_m"] + 5) for i in range(n)]
    legs = [
        _Legs(fleet, nofly, size, design.vector(alt[i], shear) if design else (0.0, 0.0), conn) for i in range(n)
    ]  # per drone: altitude

    def usable(i, s):
        if fleet_state is not None:  # rechargeable fleet: what each battery can really spend on this sortie
            return fleet_state.budget(i, s)
        return fleet_cfg["battery_wh"] * ((charge.get(i, 100) if s == 0 else 100) / 100 - fleet_cfg["min_reserve_pct"] / 100)

    routes = {(s, i): [] for s in range(ns) for i in range(n)}
    energy = dict.fromkeys(routes, 0.0)
    misses = 0
    for _, cell, action in targets:
        if misses >= MAX_CONSECUTIVE_MISSES:  # fleet is full: lower-priority targets would only burn CPU, defer them
            break
        cell, placed = tuple(cell), False
        for s in range(ns):  # most urgent work goes to the earliest sortie
            best = None
            for i in range(n):
                r, cap = routes[(s, i)], usable(i, s)
                pts = [(0, 0)] + [c for c, _ in r] + [(0, 0)]
                for pos in range(len(r) + 1):  # cheapest insertion: only legs prev->cell, cell->next change
                    prev, nxt = pts[pos], pts[pos + 1]
                    lg = legs[i]
                    l1, l2, old = lg.leg(prev, cell), lg.leg(cell, nxt), lg.leg(prev, nxt)
                    if l1 is None or l2 is None or old is None:
                        continue
                    e = energy[(s, i)] + l1[0] + l2[0] - old[0] + act_e
                    if e <= cap and (best is None or e < best[0]):
                        best = (e, i, pos)
            if best:
                e, i, pos = best
                routes[(s, i)].insert(pos, (cell, action))
                energy[(s, i)] = e  # balance: the drone whose sortie stays shortest wins
                placed = True
                break
        misses = 0 if placed else misses + 1  # (a target that fits nowhere is simply deferred to tomorrow)
    missions = []
    for (s, i), r in routes.items():
        if not r:
            continue
        r = _two_opt(r, legs[i])
        ex = legs[i].expand(r)
        missions.append(
            Mission(
                drone=i,
                altitude_m=alt[i],
                targets=ex,
                energy_wh=mission_energy((0, 0), ex, fleet, design, alt[i], shear),
                sortie=s,
                wind=wind.as_tuple() if wind is not None else None,
            )
        )
    missions = sorted(missions, key=lambda m: (m.sortie, m.drone))
    for m in missions:
        m.pad = m.drone  # each drone launches and lands on its own pad
    for k in sorted({m.sortie for m in missions}):  # drones of one sortie fly together: schedule departures, prove no conflicts
        traffic.schedule([m for m in missions if m.sortie == k], policy, allow_hold=allow_hold)
    return missions
