"""Self-calibrating action thresholds.

A fixed trigger ("water below 0.30") is a guess about a crop. If the real crop starts to suffer at 0.38 the fleet waters too late; at 0.26
it wastes water. The data to find the real stress point is already collected every day: when a patch is stressed, its greenness falls
behind the rest of the field. So we fit a HINGE to (soil moisture -> greenness anomaly): flat above a knee, falling below it. The knee is
where the crop starts to suffer; the trigger is set just above it (a small safety margin). Pests are handled the same way on the other side.

Safety: a calibrated threshold is only used once there is enough varied data and the fit clearly beats "no relationship", it is smoothed
over days, and it is clamped to a band around the policy default, so bad data can nudge the fleet but never switch it off or flood the farm.
Greenness is lagging and noisy, so this is an estimate, reported with its sample size and fit quality, never silently trusted."""

from .store import load_json, save_json

CAP = 900  # rows kept (FIFO)
MIN_ROWS = 200
MIN_SIDE = 25  # rows needed on each side of a candidate knee
MIN_R2 = 0.02  # the hinge must explain at least this much more variance than a flat line
SMOOTH = 0.3  # weight of a new estimate against the running one
BAND = {"moisture_irrigate": (-0.06, 0.12), "pest_spray": (-0.20, 0.20)}  # allowed distance from the policy default
KNEE_GRID = {"moisture": [i / 100 for i in range(18, 47)], "pest": [i / 100 for i in range(15, 76)]}


def fit_hinge(x, y, grid, below=True):
    """Best knee k for y ~ a + b*t with t = max(0, k-x) (below=True) or max(0, x-k). -> (k, r2, n_stressed) or None."""
    n = len(x)
    if n < MIN_ROWS:
        return None
    my = sum(y) / n
    sst = sum((v - my) ** 2 for v in y)
    if sst <= 1e-12:
        return None
    best = None
    for k in grid:
        t = [max(0.0, k - v) if below else max(0.0, v - k) for v in x]
        stressed = sum(1 for v in t if v > 0)
        if stressed < MIN_SIDE or n - stressed < MIN_SIDE:
            continue
        mt = sum(t) / n
        vt = sum((v - mt) ** 2 for v in t)
        if vt <= 1e-12:
            continue
        b = sum((tv - mt) * (yv - my) for tv, yv in zip(t, y)) / vt
        if b >= 0:  # stress must make the crop LESS green than its neighbours
            continue
        sse = sum((yv - (my + b * (tv - mt))) ** 2 for tv, yv in zip(t, y))
        r2 = 1 - sse / sst
        if best is None or r2 > best[1]:
            best = (k, r2, stressed)
    return best if best and best[1] >= MIN_R2 else None


def update(policy, rows, state_dir=None):
    """rows: [(ndvi_anomaly, moisture, pest, label)] from today's cleaned sample. Returns the calibrated thresholds dict (maybe empty)
    and whether it changed meaningfully. Persists the buffer and the running estimate."""
    cfg = policy.get("calibration", {})
    if not cfg.get("enabled"):
        return {}, False
    buf = (load_json("calib_rows.json", [], state_dir) + [[round(r[1], 3), round(r[2], 3), round(r[0], 3)] for r in rows])[-CAP:]
    save_json("calib_rows.json", buf, state_dir)
    prev = load_json("calibrated.json", {}, state_dir)
    thr, out = policy["thresholds"], dict(prev)
    # moisture: use patches without a pest problem, so pests do not masquerade as drought
    dry = [r for r in buf if r[1] < 0.3]
    fit_m = fit_hinge([r[0] for r in dry], [r[2] for r in dry], KNEE_GRID["moisture"], below=True)
    wet = [r for r in buf if r[0] > 0.34]
    fit_p = fit_hinge([r[1] for r in wet], [r[2] for r in wet], KNEE_GRID["pest"], below=False)
    for key, fit, margin, default in (
        ("moisture_irrigate", fit_m, cfg.get("margin_moisture", 0.015), thr["moisture_irrigate"]),
        ("pest_spray", fit_p, -cfg.get("margin_pest", 0.02), thr["pest_spray"]),
    ):
        if fit is None:
            continue
        lo, hi = BAND[key]
        target = min(max(fit[0] + margin, default + lo), default + hi)
        out[key] = round((1 - SMOOTH) * prev.get(key, default) + SMOOTH * target, 4)
        out[key + "_fit"] = {"knee": fit[0], "r2": round(fit[1], 3), "n": len(buf)}
    changed = any(abs(out.get(k, 0) - prev.get(k, 0)) > 0.01 for k in ("moisture_irrigate", "pest_spray") if k in out)
    save_json("calibrated.json", out, state_dir)
    return {k: v for k, v in out.items() if k in ("moisture_irrigate", "pest_spray")}, changed
