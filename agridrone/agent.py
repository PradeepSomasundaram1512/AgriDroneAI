"""The autonomous operating loop: observe -> classify -> plan -> safety-gate -> act -> learn -> audit.
One call to run_cycle() = one day of operation. Designed to be invoked by a scheduler (GitHub Actions)."""
import random
import time
import traceback

from . import advisor, model as M, planner, safety, store
from .adapters import SimAdapter
from .config import load_policy
from .store import append_jsonl, load_json, save_json

MAX_RETRAIN_ROWS = 400


def _labelled_sample(farm, n, rng):
    cells = rng.sample(list(farm.cells), min(n, len(farm.cells)))
    rows = []
    for c in cells:
        o = farm.observe(c)
        rows.append((o["ndvi"], o["moisture"], o["pest"], M.true_label(farm.cells[c])))
    return rows


def run_cycle(policy=None, state_dir=None, now=None):
    policy = policy or load_policy()
    thr = dict(policy["thresholds"])
    thr.update(load_json("threshold_overrides.json", {}, state_dir))
    t0 = time.time()
    audit = lambda kind, **kw: append_jsonl("audit.jsonl", {"ts": now or time.time(), "kind": kind, **kw}, state_dir)
    rec = {"ok": False, "ts": now or time.time()}
    try:
        if policy.get("kill_switch"):
            audit("kill_switch", detail="cycle skipped: kill switch engaged")
            rec.update(ok=True, skipped=True)
            append_jsonl("metrics.jsonl", rec, state_dir)
            return rec

        farm = store.load_farm(policy["field"]["seed"], policy["field"]["size"], state_dir)
        rng = random.Random(farm.seed * 1000 + farm.day)
        farm.step()

        # --- model lifecycle (continuous training on drift / low accuracy) ---
        mdl = M.StressModel.from_dict(load_json("model.json", M.StressModel().to_dict(), state_dir))
        ref = load_json("reference.json", None, state_dir)
        sample = _labelled_sample(farm, 120, rng)
        acc = mdl.accuracy(sample)
        cur_ndvi = [r[0] for r in sample]
        drift = M.psi(ref, cur_ndvi) if ref else 0.0
        retrained = False
        if mdl.version == 0 or acc < thr["min_accuracy"] or drift > thr["psi_drift"]:
            train_rows = (load_json("train_rows.json", [], state_dir) + [list(r) for r in sample])[-MAX_RETRAIN_ROWS:]
            cand = M.StressModel(list(mdl.w), mdl.version)
            cand.train([tuple(r) for r in train_rows])
            cand_acc = cand.accuracy(sample)
            if mdl.version == 0 or cand_acc >= acc:  # canary: only promote if not worse; else auto-rollback
                mdl, acc, retrained = cand, cand_acc, True
                save_json("reference.json", cur_ndvi, state_dir)
            save_json("train_rows.json", train_rows, state_dir)
            audit("retrain", promoted=retrained, acc=round(cand_acc, 3), drift=round(drift, 3), version=mdl.version)
        else:
            save_json("train_rows.json", (load_json("train_rows.json", [], state_dir) + [list(r) for r in sample])[-MAX_RETRAIN_ROWS:], state_dir)
        save_json("model.json", mdl.to_dict(), state_dir)

        # --- plan, gate, act ---
        targets = planner.find_targets(farm, mdl, thr)
        missions = planner.plan(targets, policy)
        missions, dropped = safety.repair(missions, policy)
        violations = safety.validate(missions, policy)
        level = policy["autonomy_level"]
        executed = 0
        if violations:
            audit("safety_block", violations=violations)
        elif level == "simulation":
            executed = SimAdapter(farm).execute(missions)
        else:  # supervised / autonomous: cloud only QUEUES; the ground station at the farm flies
            auto = level == "autonomous" and policy["hardware"].get("enabled", False)
            store.append_jsonl("queue.jsonl", {
                "id": f"day{farm.day}", "ts": now or time.time(), "approval": "auto" if auto else "human",
                "missions": [{"drone": m.drone, "alt": m.altitude_m, "energy_wh": m.energy_wh, "targets": m.targets} for m in missions]}, state_dir)
            audit("queued", id=f"day{farm.day}", missions=len(missions), approval="auto" if auto else "human")

        base_w, base_c = farm.blanket_usage()
        summary = {
            "day": farm.day, "targets": len(targets), "executed": executed, "dropped_by_safety": dropped,
            "accuracy": round(acc, 3), "psi": round(drift, 3), "model_version": mdl.version, "retrained": retrained,
            "water_saved_pct": round(100 * (1 - farm.water_used / base_w), 1) if base_w else 0.0,
            "chem_saved_pct": round(100 * (1 - farm.chem_used / base_c), 1) if base_c else 0.0,
            "yield_index": round(farm.mean_yield(), 4),
            "plan_seconds": round(time.time() - t0, 3),
        }
        adv = advisor.advise(summary, thr)
        if adv["thresholds"]:
            ov = load_json("threshold_overrides.json", {}, state_dir)
            ov.update(adv["thresholds"])
            save_json("threshold_overrides.json", ov, state_dir)
        audit("cycle", **summary, advisor_note=adv["note"])
        store.save_farm(farm, state_dir)
        rec.update(ok=True, **summary)
    except Exception as e:  # self-healing: record the incident, keep the loop alive for the next run
        audit("incident", error=repr(e), trace=traceback.format_exc()[-800:])
        rec.update(ok=False, error=repr(e))
    append_jsonl("metrics.jsonl", rec, state_dir)
    return rec
