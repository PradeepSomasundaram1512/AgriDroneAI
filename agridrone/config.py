"""Policy/config loading and validation. The policy file is the human control surface, so a typo in it must fail loudly
(before any cycle runs) instead of silently flying with a nonsensical limit."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "policy.json"
STATE_DIR = ROOT / "state"
REPORT_DIR = ROOT / "reports"
AUTONOMY = ("simulation", "supervised", "autonomous")


class PolicyError(ValueError):
    pass


def validate_policy(p: dict) -> dict:
    """Raises PolicyError listing EVERY problem found; returns the policy if valid."""
    errs = []

    def need(cond, msg):
        if not cond:
            errs.append(msg)

    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    need(p.get("autonomy_level") in AUTONOMY, f"autonomy_level must be one of {AUTONOMY}")
    need(isinstance(p.get("kill_switch"), bool), "kill_switch must be true or false")
    f, fl, th = p.get("field", {}), p.get("fleet", {}), p.get("thresholds", {})
    need(isinstance(f.get("size"), int) and 4 <= f["size"] <= 200, "field.size must be an integer in 4..200")
    need(num(f.get("cell_m")) and f["cell_m"] > 0, "field.cell_m must be > 0")
    need(num(f.get("sensor_fault_rate", 0)) and 0 <= f.get("sensor_fault_rate", 0) <= 0.5, "field.sensor_fault_rate must be in 0..0.5")
    need(isinstance(fl.get("drones"), int) and 1 <= fl["drones"] <= 50, "fleet.drones must be an integer in 1..50")
    need(num(fl.get("battery_wh")) and fl["battery_wh"] > 0, "fleet.battery_wh must be > 0")
    need(
        num(fl.get("min_reserve_pct")) and 10 <= fl["min_reserve_pct"] <= 60,
        "fleet.min_reserve_pct must be in 10..60 (a lower reserve is unsafe, a higher one grounds the fleet)",
    )
    need(num(fl.get("min_separation_m")) and fl["min_separation_m"] >= 5, "fleet.min_separation_m must be >= 5")
    need(isinstance(fl.get("sorties_per_day", 1), int) and 1 <= fl.get("sorties_per_day", 1) <= 10, "fleet.sorties_per_day must be 1..10")
    for k in ("wh_per_cell_move", "wh_per_cell_action"):
        need(num(fl.get(k)) and fl[k] > 0, f"fleet.{k} must be > 0")
    for k in ("speed_ms", "climb_rate_ms", "descent_rate_ms"):
        need(num(fl.get(k, 1)) and fl.get(k, 1) > 0, f"fleet.{k} must be > 0")
    hc, pad = fl.get("horizontal_clearance_m", 20), fl.get("pad_spacing_m", 40)
    need(num(hc) and hc >= 5, "fleet.horizontal_clearance_m must be >= 5")
    need(
        num(pad) and num(hc) and pad >= hc * 1.5,
        "fleet.pad_spacing_m must be at least 1.5x horizontal_clearance_m "
        "(pads closer than that put drones inside each other's safety zone)",
    )
    ch = fl.get("charging", {})
    need(ch.get("mode", "charge") in ("charge", "swap"), "fleet.charging.mode must be 'charge' or 'swap'")
    need(num(ch.get("rate_w", 90)) and ch.get("rate_w", 90) > 0, "fleet.charging.rate_w must be > 0")
    need(num(ch.get("turnaround_min", 45)) and ch.get("turnaround_min", 45) >= 1, "fleet.charging.turnaround_min must be >= 1")
    need(num(ch.get("swap_min", 3)) and ch.get("swap_min", 3) >= 0, "fleet.charging.swap_min must be >= 0")
    wl = {
        **{"max_flight_ms": 6.0, "max_gust_ms": 10.0, "max_spray_ms": 4.5, "shear_exponent": 0.2, "flight_window_factor": 0.75},
        **p.get("wind", {}),
    }
    need(
        num(wl["max_flight_ms"]) and wl["max_flight_ms"] > 0 and num(wl["max_gust_ms"]) and wl["max_gust_ms"] >= wl["max_flight_ms"],
        "wind.max_gust_ms must be >= wind.max_flight_ms > 0",
    )
    need(num(wl["max_spray_ms"]) and 0 < wl["max_spray_ms"] <= wl["max_flight_ms"], "wind.max_spray_ms must be in (0, max_flight_ms]")
    need(num(wl["shear_exponent"]) and 0 <= wl["shear_exponent"] <= 0.5, "wind.shear_exponent must be in 0..0.5")
    need(num(wl["flight_window_factor"]) and 0.3 <= wl["flight_window_factor"] <= 1.0, "wind.flight_window_factor must be in 0.3..1.0")
    air = fl.get("speed_ms", 10.0)
    if num(air) and num(wl["max_flight_ms"]) and num(wl["shear_exponent"]):
        aloft = wl["max_flight_ms"] * (30.0 / 10.0) ** wl["shear_exponent"]  # wind at the lowest flight layer when exactly at the limit
        need(
            aloft <= 0.75 * air + 1e-9,
            f"wind.max_flight_ms {wl['max_flight_ms']} m/s means {aloft:.1f} m/s at 30 m, but a drone cruising at {air} m/s "
            f"only makes headway below {0.75 * air:.1f} m/s: lower the wind limit or raise fleet.speed_ms",
        )
    gp = p.get("gps", {})
    need(num(gp.get("loss_per_flight_hour", 0)) and 0 <= gp.get("loss_per_flight_hour", 0) <= 5, "gps.loss_per_flight_hour must be in 0..5")
    need(isinstance(gp.get("recovery_days", 1), int) and gp.get("recovery_days", 1) >= 0, "gps.recovery_days must be an integer >= 0")
    need(num(gp.get("grace_s", 10)) and gp.get("grace_s", 10) >= 1, "gps.grace_s must be >= 1 second")
    need(
        isinstance(gp.get("min_sats_preflight", 10), int) and gp.get("min_sats_preflight", 10) >= gp.get("min_sats_inflight", 6) >= 4,
        "gps satellites: min_sats_preflight must be >= min_sats_inflight >= 4",
    )
    im = p.get("imagery", {})
    if im.get("enabled"):
        loc = f.get("location", {})
        need(
            num(loc.get("lat")) and -90 <= loc["lat"] <= 90 and num(loc.get("lon")) and -180 <= loc["lon"] <= 180,
            "imagery.enabled needs field.location with a valid lat/lon",
        )
        need(num(im.get("max_age_days", 5)) and im.get("max_age_days", 5) >= 1, "imagery.max_age_days must be >= 1")
    size = f.get("size", 0) if isinstance(f.get("size"), int) else 0
    for c in p.get("no_fly_cells", []):
        need(
            isinstance(c, list) and len(c) == 2 and all(isinstance(v, int) for v in c) and 0 <= c[0] < size and 0 <= c[1] < size,
            f"no_fly_cells entry {c} is not a cell inside the field",
        )
        need(c != [0, 0], "no_fly_cells must not include the home pad (0,0)")
    for k, lo, hi in (
        ("ndvi_stress", 0, 1),
        ("moisture_irrigate", 0.05, 0.5),
        ("pest_spray", 0.1, 1),
        ("psi_drift", 0.05, 2),
        ("min_accuracy", 0.5, 1),
        ("min_recall", 0.5, 1),
    ):
        need(num(th.get(k)) and lo <= th[k] <= hi, f"thresholds.{k} must be in {lo}..{hi}")
    h = p.get("hardware", {})
    if h.get("enabled"):
        o = h.get("origin", {})
        need(
            num(o.get("lat")) and -90 <= o["lat"] <= 90 and num(o.get("lon")) and -180 <= o["lon"] <= 180,
            "hardware.origin needs a valid lat/lon",
        )
        need(len(h.get("links", [])) >= fl.get("drones", 1), "hardware.links needs one link per drone")
        need(p.get("autonomy_level") != "simulation", "hardware.enabled requires autonomy_level supervised/autonomous")
    if errs:
        raise PolicyError("invalid config/policy.json:\n  - " + "\n  - ".join(errs))
    return p


def load_policy(path: Path = POLICY_PATH) -> dict:
    return validate_policy(json.loads(Path(path).read_text()))
