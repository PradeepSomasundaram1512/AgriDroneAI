"""GPS (GNSS) loss: what the fleet does when a drone loses its position fix.

Without GPS a multirotor cannot navigate home, so the safe response is NOT "return to launch". The policy implemented here:
  1. FREEZE the payload immediately (never spray or water blind),
  2. HOLD for a short grace period in case the fix returns (resume the mission if it does),
  3. otherwise LAND IN PLACE (a controlled descent beats an uncontrolled failsafe),
  4. protect the others: a drone cruising BELOW the descending one could be hit from above, so every drone on a lower
     altitude layer is ordered home at once (they still have GPS, so RTL works),
  5. the landed drone is grounded until a human recovers it (`recovery_days`).
This module holds the pure logic shared by the simulator, the planner's fleet state and the hardware executor.
Pre-flight quality (satellite count, fix type) is checked before takeoff by the executor using `preflight_ok`."""

import math
import random

from . import traffic

DEFAULTS = {
    "loss_per_flight_hour": 0.0,  # simulator: probability rate of a GNSS outage during a flight (0 = none)
    "recovery_days": 1,  # a drone that landed in the field is out until someone recovers it
    "min_sats_preflight": 10,
    "min_sats_inflight": 6,
    "grace_s": 10.0,  # hold this long hoping the fix returns before landing in place
    "poll_s": 1.0,
}


def cfg(policy):
    return {**DEFAULTS, **policy.get("gps", {})}


def preflight_ok(sats, fix, policy):
    """-> (ok, reason). A 3D fix and enough satellites before takeoff (more satellites = smaller position error)."""
    c = cfg(policy)
    if fix is None or sats is None:
        return False, "no GPS information from the vehicle"
    if fix < 3:
        return False, f"GPS fix type {fix} is below a 3D fix"
    if sats < c["min_sats_preflight"]:
        return False, f"only {sats} GPS satellites (need {c['min_sats_preflight']})"
    return True, ""


def inflight_ok(sats, fix, policy):
    c = cfg(policy)
    return fix is not None and sats is not None and fix >= 3 and sats >= c["min_sats_inflight"]


def must_return(my_altitude, emergency_altitudes):
    """A drone must come home if any drone in distress is ABOVE it (a descending drone falls through lower layers)."""
    return any(a > my_altitude for a in emergency_altitudes)


def _arrivals(m, policy):
    """[(waypoint index, seconds after sortie start when its treatment is DONE)] for the REAL waypoints (not detour points).
    Trajectory segments are: ground wait, climb, then per waypoint a leg (+ a hover/dwell if it is a treatment), ..."""
    segs, _ = traffic._segments(m, traffic._cfg_for([m], policy))
    out, idx = [], 2
    for i, (_, action) in enumerate(m.targets):
        leg_end = segs[idx][1]
        idx += 1
        if action != "via":
            out.append((i, segs[idx][1]))  # the dwell segment: the patch is treated when the hover ends
            idx += 1
        else:
            assert leg_end >= 0
    return out


def sample_losses(missions, policy, seed, day):
    """Deterministic (per seed/day/drone/sortie) GNSS outages for the day's missions -> [{drone, sortie, t, after_patches, cell}].
    Independent of the strategy being benchmarked: the same outages hit every strategy that flies the same drone-hours."""
    rate = cfg(policy)["loss_per_flight_hour"]
    events = []
    if rate <= 0:
        return events
    for m in missions:
        if not m.targets:
            continue
        dur = traffic.duration(m, policy)
        r = random.Random(f"gps-{seed}-{day}-{m.drone}-{m.sortie}")
        if r.random() < 1.0 - math.exp(-rate * dur / 3600.0):
            t = r.uniform(0.1 * dur, 0.9 * dur)  # outage somewhere mid-flight
            arr = _arrivals(m, policy)
            done = [i for i, te in arr if te <= t]
            events.append(
                {
                    "drone": m.drone,
                    "sortie": m.sortie,
                    "t": round(t),
                    "after_patches": len(done),
                    "cell": list(m.targets[done[-1]][0]) if done else [0, 0],
                    "cut": (max(done) + 1) if done else 0,
                    "altitude": m.altitude_m,
                }
            )
    return events


def apply_losses(missions, events, policy):
    """Truncate the plan the way the response protocol would. Returns (missions_actually_flown, info).
    - the drone in distress completes only the waypoints before its outage, then lands in place;
    - its later sorties are cancelled (it is on the ground in the field);
    - drones on LOWER layers in the same sortie are ordered home at the outage time;
    energy of a truncated flight is scaled by the share of its timeline flown (a landing/return is cheaper than the full route)."""
    by_key = {(m.drone, m.sortie): m for m in missions}
    cut = {}  # (drone, sortie) -> (waypoint index to cut at, fraction of timeline flown)
    cancelled = set()
    failed, effective = set(), []
    for e in sorted(events, key=lambda e: (e["sortie"], e["t"])):  # a drone already down in a field cannot lose GPS again
        if e["drone"] not in failed:
            failed.add(e["drone"])
            effective.append(e)
    events = effective
    for e in events:
        key = (e["drone"], e["sortie"])
        m = by_key[key]
        cut[key] = (e["cut"], min(1.0, e["t"] / max(1.0, traffic.duration(m, policy))))
        for (d2, s2), m2 in by_key.items():
            if d2 == e["drone"] and s2 > e["sortie"]:
                cancelled.add((d2, s2))
            elif s2 == e["sortie"] and d2 != e["drone"] and must_return(m2.altitude_m, [e["altitude"]]):
                arr = _arrivals(m2, policy)
                done = [i for i, te in arr if te <= e["t"]]
                k2 = (max(done) + 1) if done else 0
                prev = cut.get((d2, s2))
                if prev is None or k2 < prev[0]:
                    cut[(d2, s2)] = (k2, min(1.0, e["t"] / max(1.0, traffic.duration(m2, policy))))
    out, flown_patches, lost_patches = [], 0, 0
    for m in missions:
        key = (m.drone, m.sortie)
        before = sum(1 for _, a in m.targets if a != "via")
        if key in cancelled:
            lost_patches += before
            continue
        if key in cut:
            k, frac = cut[key]
            m = type(m)(**{**m.__dict__, "targets": m.targets[:k], "energy_wh": m.energy_wh * frac})
        after = sum(1 for _, a in m.targets if a != "via")
        flown_patches += after
        lost_patches += before - after
        if m.targets:
            out.append(m)
    return out, {
        "patches_flown": flown_patches,
        "patches_deferred": lost_patches,
        "grounded": sorted(failed),
        "events": events,
    }
