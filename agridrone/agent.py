"""The autonomous operating loop: observe -> classify -> plan -> safety-gate -> act -> learn -> audit.
One call to run_cycle() = one day of operation. Designed to be invoked by a scheduler (GitHub Actions)."""
import random
import time
import traceback

from . import advisor, model as M, planner, safety, store
from .adapters import SimAdapter
from .config import load_policy
from .store import append_jsonl, load_json, save_json

TRAIN_CAP = 250   # per class: rare stressed examples must not be evicted by healthy ones
EVAL_CAP = 150


def _load_buf(name, d):
    b = load_json(name, {"pos": [], "neg": []}, d)
    return b if isinstance(b, dict) else {"pos": [], "neg": []}


def _save_buf(name, b, d):
    save_json(name, b, d)


def _add(buf, rows, cap):
    for r in rows:
        buf["pos" if r[3] else "neg"].append([round(r[0], 4), round(r[1], 4), round(r[2], 4), r[3]])
    for k in ("pos", "neg"):
        buf[k] = buf[k][-cap:]


def snapshot(farm):
    """Compact field state for the dashboard: [[ndvi%, moisture%, pest%]] ordered x-major (index = x*size + y)."""
    return [[round(farm.cells[(x, y)].ndvi * 100), round(farm.cells[(x, y)].moisture * 100), round(farm.cells[(x, y)].pest * 100)]
            for x in range(farm.size) for y in range(farm.size)]


def _labelled_sample(farm, n, rng):
    cells = rng.sample(list(farm.cells), min(n, len(farm.cells)))
    obs = [farm.observe(c) for c in cells]
    med = M.median([o["ndvi"] for o in obs])               # NDVI anomaly vs the field, not raw NDVI (growth trend)
    return [(o["ndvi"] - med, o["moisture"], o["pest"], M.true_label(farm.cells[c])) for o, c in zip(obs, cells)]


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

        # --- model lifecycle: honest evaluation + continuous training with canary promotion/rollback ---
        mdl = M.StressModel.from_dict(load_json("model.json", M.StressModel().to_dict(), state_dir))
        ref = load_json("reference.json", None, state_dir)
        sample = _labelled_sample(farm, 120, rng)
        train_buf = _load_buf("train_rows.json", state_dir)
        eval_buf = _load_buf("eval_rows.json", state_dir)
        # disjoint split: even rows may be trained on, odd rows are ONLY ever used to evaluate
        _add(train_buf, sample[0::2], TRAIN_CAP)
        _add(eval_buf, sample[1::2], EVAL_CAP)
        recent = load_json("eval_recent.json", {}, state_dir)           # {day: held-out rows}: the CURRENT regime
        recent[str(farm.day)] = [[round(r[0], 4), round(r[1], 4), round(r[2], 4), r[3]] for r in sample[1::2]]
        recent = {k: v for k, v in recent.items() if farm.day - int(k) < 5}
        save_json("eval_recent.json", recent, state_dir)
        recent_rows = [tuple(r) for v in recent.values() for r in v]
        long_rows = eval_buf["pos"] + eval_buf["neg"]
        # judge on the recent window when it contains both classes, else on the class-balanced long-term buffer;
        # a candidate must also not regress on the long-term buffer (guards against forgetting old regimes)
        def judge(m):
            sr, sl = m.score(recent_rows), m.score(long_rows)
            return (sr if sr["bal"] is not None else sl), sl
        eval_rows = long_rows
        inc, inc_long = judge(mdl) if mdl.tree else ({"bal": None, "recall": None}, {"bal": None})
        cur_ndvi = [r[0] for r in sample]   # anomalies: drift here means the field is becoming heterogeneous
        drift = M.psi(ref, cur_ndvi) if ref else 0.0
        last_train_day = load_json("last_train_day.json", 0, state_dir)
        weak = mdl.tree is None or inc["bal"] is None or inc["bal"] < thr["min_accuracy"] or (inc["recall"] is not None and inc["recall"] < thr["min_recall"])
        retrained = False
        if weak or drift > thr["psi_drift"] or farm.day - last_train_day >= 7:
            cand = M.StressModel(mdl.tree, mdl.version)
            # the seed set is ALWAYS included (never evicted): its features are independent of the label, which
            # stops the tree from latching onto drifting shortcut features such as NDVI trend
            if cand.train(M.seed_rows() + train_buf["pos"] + train_buf["neg"]):
                cs, cs_long = judge(cand)
                better = inc["bal"] is None or (cs["bal"] is not None and cs["bal"] >= inc["bal"] + 0.005
                                                and (inc_long["bal"] is None or cs_long["bal"] is None or cs_long["bal"] >= inc_long["bal"] - 0.03))
                if better:                                      # canary passed: promote
                    mdl, inc, retrained = cand, cs, True
                    save_json("reference.json", cur_ndvi, state_dir)
                    save_json("last_train_day.json", farm.day, state_dir)
                audit("retrain", promoted=retrained, cand_bal=cs["bal"] and round(cs["bal"], 3), incumbent_bal=inc["bal"] and round(inc["bal"], 3) if not retrained else None,
                      drift=round(drift, 3), version=mdl.version, rolled_back=not retrained)
            else:
                audit("retrain_skipped", reason="training data lacks one class")
        save_json("model.json", mdl.to_dict(), state_dir)
        _save_buf("train_rows.json", train_buf, state_dir); _save_buf("eval_rows.json", eval_buf, state_dir)
        acc = inc["bal"] if inc["bal"] is not None else mdl.accuracy(sample)   # balanced accuracy, not raw accuracy
        recall = inc["recall"]
        if acc < thr["min_accuracy"] or (recall is not None and recall < thr["min_recall"]):
            audit("model_below_target", balanced_accuracy=round(acc, 3), recall=recall and round(recall, 3),
                  detail="needs more/better labelled data; planner still protects crops via threshold rules")

        # --- plan, gate, act ---
        targets = planner.find_targets(farm, mdl, thr)
        missions = planner.plan(targets, policy)
        missions, dropped = safety.repair(missions, policy)
        violations = safety.validate(missions, policy)
        level = policy["autonomy_level"]
        executed = 0
        before = snapshot(farm)   # for the dashboard replay: the field as the drones found it
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
            "accuracy": round(acc, 3), "recall": None if recall is None else round(recall, 3), "psi": round(drift, 3), "model_version": mdl.version, "retrained": retrained,
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
        # dashboard feed: today's mission (animated replay) + a rolling 30-day field history (time-lapse)
        save_json("last_mission.json", {"day": farm.day, "size": farm.size, "before": before, "no_fly": policy["no_fly_cells"],
                  "missions": [{"drone": m.drone, "alt": m.altitude_m, "targets": [[c[0], c[1], a] for c, a in m.targets]} for m in missions]}, state_dir)
        hist = load_json("field_history.json", [], state_dir)
        hist = [h for h in hist if h["day"] != farm.day][-29:] + [{"day": farm.day, "cells": snapshot(farm)}]
        save_json("field_history.json", hist, state_dir)
        rec.update(ok=True, **summary)
    except Exception as e:  # self-healing: record the incident, keep the loop alive for the next run
        audit("incident", error=repr(e), trace=traceback.format_exc()[-800:])
        rec.update(ok=False, error=repr(e))
    append_jsonl("metrics.jsonl", rec, state_dir)
    return rec
