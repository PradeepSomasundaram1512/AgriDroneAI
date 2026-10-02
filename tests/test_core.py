import json
from agridrone import model as M, planner, safety, store
from agridrone.agent import run_cycle
from agridrone.config import load_policy
from agridrone.reporting import build_report
from agridrone.sim import Farm


def test_safety_blocks_nofly_and_battery():
    pol = load_policy()
    m = safety.Mission(0, 30, [((10, 10), "spray"), ((5, 5), "irrigate")])
    m.energy_wh = safety.mission_energy((0, 0), m.targets, pol["fleet"])
    assert any("no-fly" in v for v in safety.validate([m], pol))
    fixed, dropped = safety.repair([m], pol)
    assert dropped == 1 and not safety.validate(fixed, pol)


def test_altitude_separation():
    pol = load_policy()
    f = Farm.create(12, 1)
    for _ in range(10):
        f.step(rain=0)
    mdl = M.StressModel()
    assert mdl.train(M.seed_rows())
    ms = planner.plan(planner.find_targets(f, mdl, pol["thresholds"]), pol)
    ms, _ = safety.repair(ms, pol)
    assert not safety.validate(ms, pol)


def test_kill_switch_halts_actuation(tmp_path):
    pol = load_policy()
    pol["kill_switch"] = True
    rec = run_cycle(pol, tmp_path)
    assert rec["ok"] and rec["skipped"]


def test_autopilot_runs_30_days_and_saves_resources(tmp_path):
    pol = load_policy()
    pol["field"]["size"] = 12
    for _ in range(30):
        rec = run_cycle(pol, tmp_path)
        assert rec["ok"], rec
    assert rec["accuracy"] >= 0.85
    assert rec["water_saved_pct"] > 0 and rec["chem_saved_pct"] > 0
    assert "Metrics vs targets" in build_report("weekly", tmp_path)


def test_state_persists_and_resumes(tmp_path):
    pol = load_policy()
    pol["field"]["size"] = 8
    run_cycle(pol, tmp_path)
    r2 = run_cycle(pol, tmp_path)
    assert r2["day"] == 2


def test_corrupt_state_is_incident_not_crash(tmp_path):
    pol = load_policy()
    pol["field"]["size"] = 8
    (tmp_path / "version.json").write_text('{"version": 2}')  # current-version state that is corrupt => incident, not migration
    (tmp_path / "farm.json").write_text("{broken")
    rec = run_cycle(pol, tmp_path)
    assert rec["ok"] is False
    assert any(r["kind"] == "incident" for r in store.read_jsonl("audit.jsonl", tmp_path))


def test_dashboard_renders(tmp_path):
    from agridrone.dashboard import render

    pol = load_policy()
    pol["field"]["size"] = 8
    for _ in range(3):
        run_cycle(pol, tmp_path)
    out = render(tmp_path, pol)
    assert "AgriDroneAI" in out and "simulated farm" in out and "<canvas" in out
    assert "/*__DATA__*/null" not in out  # data was injected
    import re

    data = json.loads(re.search(r"const D=(\{.*?\});\n", out, re.S).group(1))
    assert data["size"] == 8 and data["kpi"]["cells"] == 64 and data["mission"]["missions"] is not None


def test_planner_spreads_work_across_drones():
    pol = load_policy()
    tg = [(1.0, c, "irrigate") for c in [(3, 3), (8, 3), (3, 8), (8, 8), (12, 5), (5, 12)]]
    ms = planner.plan(tg, pol)
    assert len(ms) == pol["fleet"]["drones"] and all(m.targets for m in ms)
    assert len({m.altitude_m for m in ms}) == len(ms)
    assert not safety.validate(ms, pol)
