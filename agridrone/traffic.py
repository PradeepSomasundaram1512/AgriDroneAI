"""Air-traffic safety: make sure drones flying at the same time never get too close, in 3D, over time.

Altitude layers alone only protect drones in LEVEL flight. The dangerous moments are climbs and descents: a drone leaving
its pad passes through every layer above it, and a drone cruising overhead may be passing exactly there. So every drone has
its own pad (spaced apart), and this module
  * builds each mission's full 4D trajectory (climb, legs, hover, return, hold, descent) with real speeds,
  * replays all concurrently flying drones second by second and reports every moment two drones are both closer than
    the horizontal clearance AND closer than the vertical clearance (a conflict needs both),
  * schedules departures (stagger) and pre-landing holds until the replay shows zero conflicts,
  * falls back to flying one drone at a time (zero overlap, safe by construction) if no concurrent schedule works.
`validate()` in safety.py runs `check()` on every plan, so an unsafe schedule can never be executed."""

import math
from itertools import combinations

from . import wind as W

DEFAULTS = {
    "speed_ms": 10.0,
    "climb_rate_ms": 3.0,
    "descent_rate_ms": 2.0,
    "dwell_s": 4.0,
    "horizontal_clearance_m": 20.0,
    "pad_spacing_m": 40.0,
}
MIN_GS = 0.25  # fraction of airspeed assumed if a leg is (wrongly) infeasible; the gate rejects such legs anyway
STEP_S = 1.0  # replay time step; conflicts use clearances inflated by what two drones can close between samples (see sampling_margins)
MOVE_S = 6  # how much each scheduling move delays a drone
MAX_ITER = 80
TURNAROUND_PAD_S = 3.0


def cfg(policy, wind=None):
    """Traffic parameters. `wind` (wind.Wind) widens the required horizontal clearance by the GNSS error budget plus a
    gust allowance, and slows/speeds legs by the wind triangle."""
    f = policy["fleet"]
    c = {k: float(f.get(k, v)) for k, v in DEFAULTS.items()}
    c["vertical_clearance_m"] = float(f["min_separation_m"])
    c["cell_m"] = float(policy["field"]["cell_m"])
    c["shear"] = W.limits(policy)["shear_exponent"]
    c["wind"] = wind
    c["horizontal_clearance_m"] += W.clearance_margin(wind or W.CALM, policy)
    return c


def sampling_margins(c):
    """How much further apart than the clearance two drones must be at a SAMPLE for the pair to be safe BETWEEN samples.
    The replay samples every STEP_S seconds; between samples each drone moves at most (speed * STEP_S / 2) horizontally (plus wind) and
    (climb or descent rate * STEP_S / 2) vertically, so the pair closes by at most twice that. Inflating the conflict test by exactly
    this bound guarantees a conflict cannot start and end between two samples (a grazing pass was missed by the plain 1 s check)."""
    gust = c["wind"].gust if c.get("wind") is not None else 0.0
    vmax_h = c["speed_ms"] + 1.5 * gust
    return vmax_h * STEP_S, max(c["climb_rate_ms"], c["descent_rate_ms"]) * STEP_S


def pad_xy(pad, c):
    return (pad * c["pad_spacing_m"], 0.0)


def _segments(m, c):
    """Piecewise-linear trajectory: [(t0, t1, (x,y,z) start, (x,y,z) end)] in metres/seconds from sortie start."""
    px, py = pad_xy(m.pad if m.pad >= 0 else m.drone, c)
    alt, segs, t = float(m.altitude_m), [], 0.0
    segs.append((0.0, float(m.t0), (px, py, 0.0), (px, py, 0.0)))
    t = float(m.t0)
    nt = t + alt / c["climb_rate_ms"]
    segs.append((t, nt, (px, py, 0.0), (px, py, alt)))
    t, cur = nt, (px, py, alt)
    wvec = c["wind"].vector(alt, c["shear"]) if c["wind"] is not None else (0.0, 0.0)

    def leg_time(a, b):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        gs = W.ground_speed(b[0] - a[0], b[1] - a[1], c["speed_ms"], wvec)
        return d / (gs if gs else MIN_GS * c["speed_ms"])

    for cell, action in m.targets:
        nxt = (cell[0] * c["cell_m"], cell[1] * c["cell_m"], alt)
        nt = t + leg_time(cur, nxt)
        segs.append((t, nt, cur, nxt))
        t, cur = nt, nxt
        if action != "via":
            nt = t + c["dwell_s"]
            segs.append((t, nt, cur, cur))
            t = nt
    home = (px, py, alt)
    nt = t + leg_time(cur, home)
    segs.append((t, nt, cur, home))
    t = nt
    if m.hold:
        segs.append((t, t + m.hold, home, home))
        t += m.hold
    nt = t + alt / c["descent_rate_ms"]
    segs.append((t, nt, home, (px, py, 0.0)))
    return segs, nt


def _cfg_for(missions, policy):
    """All drones of a sortie share the same sky, so they share the same wind."""
    w = next((m.wind for m in missions if getattr(m, "wind", None)), None)
    return cfg(policy, W.Wind.from_tuple(w) if w else None)


def duration(m, policy):
    return _segments(m, _cfg_for([m], policy))[1]


def _pos(segs, t):
    for a, b, p, q in segs:
        if t <= b:
            u = 0.0 if b == a else (t - a) / (b - a)
            return (p[0] + (q[0] - p[0]) * u, p[1] + (q[1] - p[1]) * u, p[2] + (q[2] - p[2]) * u)
    return segs[-1][3]


def _event(t, a, b, h, v):
    return {"t": round(t), "drones": [a.drone, b.drone], "horizontal_m": round(h, 1), "vertical_m": round(v, 1)}


def check(missions, policy, max_report=5, inflate=True):
    """Replay all missions of ONE sortie (they fly concurrently). -> {conflicts, steps, min_ratio, closest}.
    A conflict = horizontal distance < clearance AND vertical distance < clearance at the same instant.
    min_ratio = smallest max(h/H, v/V) over time (>= 1.0 means every moment was at least as safe as required)."""
    c = _cfg_for(missions, policy)
    ms = [m for m in missions if m.targets]
    built = [_segments(m, c) for m in ms]
    end = max((b[1] for b in built), default=0.0)
    H, V = c["horizontal_clearance_m"], c["vertical_clearance_m"]
    mh, mv = sampling_margins(c) if inflate else (0.0, 0.0)
    Hs, Vs = H + mh, V + mv  # conflict thresholds at a sample (see sampling_margins)
    conflicts, steps, best = [], 0, (float("inf"), None)
    t = 0.0
    while t <= end + STEP_S:
        pos = [_pos(b[0], min(t, b[1])) for b in built]
        for (i, a), (j, b) in combinations(enumerate(pos), 2):
            if a[2] < 1.0 or b[2] < 1.0:  # a drone parked on its pad is not in the air: pads are separated by geometry
                continue
            h, v = math.hypot(a[0] - b[0], a[1] - b[1]), abs(a[2] - b[2])
            ratio = max(h / H, v / V)
            if ratio < best[0]:
                best = (ratio, _event(t, ms[i], ms[j], h, v))
            if h < Hs and v < Vs:
                steps += 1
                if len(conflicts) < max_report:
                    conflicts.append(_event(t, ms[i], ms[j], h, v))
        t += STEP_S
    return {
        "conflicts": conflicts,
        "steps": steps,
        "min_ratio": None if best[1] is None else round(best[0], 2),
        "closest": best[1],
        "duration_s": round(end),
    }


def schedule(missions, policy, allow_hold=True):
    """Assign launch delays (m.t0) and pre-landing holds (m.hold) so the replay shows zero conflicts.
    Returns {'ok': True, 'mode': 'concurrent'|'serialized', ...report}. Mutates the missions."""
    ms = [m for m in missions if m.targets]
    for m in ms:
        m.t0, m.hold = 0, 0
    # launch the highest-altitude drone first: it climbs while the lower ones are still parked
    ms.sort(key=lambda m: -m.altitude_m)
    for k, m in enumerate(ms):
        m.t0 = k * 3
    rep = check(ms, policy)
    it = 0
    while rep["steps"] and it < MAX_ITER:
        it += 1
        a, b = (x for x in ms if x.drone in rep["conflicts"][0]["drones"])
        best = None
        for m, field in ((a, "t0"), (b, "t0"), (a, "hold"), (b, "hold")):
            if field == "hold" and not allow_hold:
                continue
            setattr(m, field, getattr(m, field) + MOVE_S)
            r = check(ms, policy)
            setattr(m, field, getattr(m, field) - MOVE_S)
            score = (r["steps"], sum(x.t0 + x.hold for x in ms))
            if best is None or score < best[0]:
                best = (score, m, field)
        setattr(best[1], best[2], getattr(best[1], best[2]) + MOVE_S)
        rep = check(ms, policy)
    mode = "concurrent"
    if rep["steps"]:  # no safe overlap found: fly one drone at a time. Zero temporal overlap, so safe by construction.
        mode, t = "serialized", 0
        c = _cfg_for(ms, policy)
        for m in sorted(ms, key=lambda x: x.drone):
            m.t0, m.hold = int(t), 0
            t += _segments(m, c)[1] - m.t0 + TURNAROUND_PAD_S
        rep = check(ms, policy)
    rep.update(
        ok=rep["steps"] == 0,
        mode=mode,
        iterations=it,
        max_delay_s=max((m.t0 for m in ms), default=0),
        max_hold_s=max((m.hold for m in ms), default=0),
    )
    return rep
