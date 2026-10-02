import random

from agridrone import calibrate as C
from agridrone import store
from agridrone.agent import run_cycle
from agridrone.config import load_policy


def pol(enabled=True):
    p = load_policy()
    p["calibration"] = {"enabled": enabled, "margin_moisture": 0.015, "margin_pest": 0.02}
    return p


def hinge_rows(onset_m, n=500, seed=1, noise=0.03, onset_p=None):
    """Synthetic patches whose greenness falls behind the field once moisture is below `onset_m` (and pests above `onset_p`)."""
    r = random.Random(seed)
    out = []
    for _ in range(n):
        m, pe = r.uniform(0.12, 0.5), r.choice([0.05, 0.1, 0.15]) if onset_p is None else r.uniform(0.0, 0.9)
        anomaly = -0.9 * max(0.0, onset_m - m) - (0.5 * max(0.0, pe - onset_p) if onset_p else 0.0) + r.gauss(0, noise)
        out.append((anomaly, m, pe, 0))
    return out


def test_the_hinge_fit_recovers_the_knee_within_a_few_hundredths():
    for onset in (0.26, 0.30, 0.34, 0.38):
        rows = hinge_rows(onset)
        fit = C.fit_hinge([r[1] for r in rows], [r[0] for r in rows], C.KNEE_GRID["moisture"], below=True)
        assert fit is not None and abs(fit[0] - onset) <= 0.035, (onset, fit)


def test_no_relationship_means_no_calibration():
    r = random.Random(3)
    flat = [(r.gauss(0, 0.03), r.uniform(0.12, 0.5), 0.1, 0) for _ in range(500)]
    assert C.fit_hinge([x[1] for x in flat], [x[0] for x in flat], C.KNEE_GRID["moisture"]) is None
    assert C.fit_hinge([x[1] for x in flat[:50]], [x[0] for x in flat[:50]], C.KNEE_GRID["moisture"]) is None  # too little data


def test_the_wrong_sign_is_rejected():
    """If greenness RISES when it is drier, that is not drought stress: never calibrate to it."""
    r = random.Random(5)
    rows = [(0.9 * max(0.0, 0.3 - m) + r.gauss(0, 0.02), m, 0.1, 0) for m in (r.uniform(0.12, 0.5) for _ in range(500))]
    assert C.fit_hinge([x[1] for x in rows], [x[0] for x in rows], C.KNEE_GRID["moisture"]) is None


def test_update_learns_smooths_clamps_and_reports(tmp_path):
    p = pol()
    learned = {}
    for day in range(12):
        learned, _ = C.update(p, hinge_rows(0.38, 120, seed=day), tmp_path)
    assert (
        0.35 <= learned["moisture_irrigate"] <= p["thresholds"]["moisture_irrigate"] + 0.12 + 1e-9
    )  # raised from 0.30 toward the real knee
    assert store.load_json("calibrated.json", {}, tmp_path)["moisture_irrigate_fit"]["r2"] > 0.1
    # absurd data cannot push the trigger outside the band around the default
    q = pol()
    for day in range(30):
        learned, _ = C.update(q, hinge_rows(0.45, 120, seed=100 + day), tmp_path / "x")
    assert learned["moisture_irrigate"] <= q["thresholds"]["moisture_irrigate"] + 0.12 + 1e-9


def test_disabled_does_nothing(tmp_path):
    assert C.update(pol(enabled=False), hinge_rows(0.38), tmp_path) == ({}, False)
    assert not (tmp_path / "calib_rows.json").exists()


def test_pests_are_calibrated_on_the_other_side():
    rows = hinge_rows(0.20, n=700, onset_p=0.30)  # moisture fine; pests hurt above 0.30
    wet = [r for r in rows if r[1] > 0.34]
    fit = C.fit_hinge([r[2] for r in wet], [r[0] for r in wet], C.KNEE_GRID["pest"], below=False)
    assert fit is not None and abs(fit[0] - 0.30) <= 0.06


def test_the_agent_runs_with_calibration_on_and_records_its_triggers(tmp_path):
    p = pol()
    p["field"]["size"] = 16
    p["field"]["physics"] = {"onset_moisture": 0.36}
    trig = []
    for _ in range(60):
        rec = run_cycle(p, tmp_path)
        assert rec["ok"], rec
        trig.append(rec["moisture_trigger"])
    assert trig[0] == p["thresholds"]["moisture_irrigate"]
    assert max(trig) >= trig[0]  # it moves, and only inside the safe band
    assert max(trig) <= p["thresholds"]["moisture_irrigate"] + 0.12 + 1e-9
