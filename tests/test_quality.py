import math
from agridrone import quality, store, weather
from agridrone.agent import run_cycle
from agridrone.config import load_policy
from agridrone.sim import Farm


def field(seed=1, rate=0.0, days=5, size=16):
    f = Farm.create(size, seed, fault_rate=rate)
    for _ in range(days):
        f.step(weather.synthetic(f.day + 1, seed))
    return f


def test_healthy_field_has_almost_no_false_alarms(tmp_path):
    f, flagged = field(days=1), 0
    for _ in range(25):
        f.step(weather.synthetic(f.day + 1, 1))
        _, rep = quality.clean(f.observe_all(), tmp_path)
        flagged += rep["flagged"]
    assert flagged / (25 * 256) < 0.005


def test_dead_sensor_is_repaired_from_neighbours(tmp_path):
    f = field()
    obs = f.observe_all()
    victim = next(o for o in obs if o["cell"] == (8, 8))
    truth = f.cells[(8, 8)].moisture
    victim["moisture"] = 0.0  # dead probe
    clean, rep = quality.clean(obs, tmp_path)
    fixed = next(o for o in clean if o["cell"] == (8, 8))
    assert (8, 8) in map(tuple, rep["cells"]) and abs(fixed["moisture"] - truth) < 0.1 and fixed["repaired"]


def test_stuck_sensor_flagged_after_a_few_identical_days(tmp_path):
    f = field()
    caught = False
    for _ in range(6):
        f.step(weather.synthetic(f.day + 1, 1))
        obs = f.observe_all()
        next(o for o in obs if o["cell"] == (5, 5)).update(moisture=0.3333, ndvi=0.5, pest=0.1)  # frozen output
        _, rep = quality.clean(obs, tmp_path)
        caught = caught or (5, 5) in map(tuple, rep["cells"])
    assert caught


def test_persistent_offender_is_quarantined_and_reported(tmp_path):
    f = field()
    for _ in range(5):
        f.step(weather.synthetic(f.day + 1, 1))
        obs = f.observe_all()
        next(o for o in obs if o["cell"] == (3, 12)).update(moisture=0.0)
        _, rep = quality.clean(obs, tmp_path)
    assert (3, 12) in map(tuple, rep["quarantined"])


def test_nan_and_garbage_never_crash_the_pipeline(tmp_path):
    f = field()
    obs = f.observe_all()
    obs[0]["moisture"] = float("nan")
    obs[1]["ndvi"] = float("inf")
    obs[2]["pest"] = -5
    clean, rep = quality.clean(obs, tmp_path)
    assert all(math.isfinite(o[c]) for o in clean for c in ("moisture", "ndvi", "pest"))


def test_filter_disabled_passes_data_through(tmp_path):
    f = field()
    obs = f.observe_all()
    obs[0]["moisture"] = 0.0
    clean, _ = quality.clean(obs, tmp_path, enabled=False)
    assert clean[0]["moisture"] == 0.0


def test_faults_are_deterministic_and_identical_across_strategies():
    a, b = Farm.create(16, 3, fault_rate=0.1), Farm.create(16, 3, fault_rate=0.1)
    assert a.faults.keys() == b.faults.keys() and len(a.faults) > 5
    assert {k: v["mode"] for k, v in a.faults.items()} == {k: v["mode"] for k, v in b.faults.items()}


def test_agent_survives_faulty_sensors_and_reports_them(tmp_path):
    pol = load_policy()
    pol["field"]["size"] = 16
    pol["field"]["sensor_fault_rate"] = 0.08
    for _ in range(25):
        rec = run_cycle(pol, tmp_path)
        assert rec["ok"], rec
    assert rec["sensors_flagged"] >= 0 and any(a["kind"] == "sensor_faults" for a in store.read_jsonl("audit.jsonl", tmp_path))
    f = store.load_farm(pol["field"]["seed"], 16, tmp_path, 0.08)
    assert f.faults  # fault map survives save/load


def test_detector_recall_on_dead_and_stuck_sensors(tmp_path):
    f, hit, tot = Farm.create(24, 2, fault_rate=0.06), 0, 0
    for _ in range(30):
        f.step(weather.synthetic(f.day + 1, 2))
        _, rep = quality.clean(f.observe_all(), tmp_path)
        flagged = set(map(tuple, rep["cells"])) | set(map(tuple, rep["quarantined"]))
        for c, fl in f.faults.items():
            if fl["mode"] in ("dead", "stuck") and f.day >= fl["start"] + 3:
                tot += 1
                hit += c in flagged
    assert tot > 50 and hit / tot > 0.9
