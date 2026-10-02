"""Judge the stress model honestly: run N autopilot days per seed, then score the model in use against the
WHOLE field every day (recall of truly stressed cells, balanced accuracy), not just the agent's own sample.
  PYTHONPATH=. python scripts/eval_model.py [days] [seeds...]"""
import sys, tempfile
from agridrone import model as M, store
from agridrone.agent import run_cycle
from agridrone.config import load_policy


def full_field_scores(state_dir, farm):
    mdl = M.StressModel.from_dict(store.load_json("model.json", M.StressModel().to_dict(), state_dir))
    tp = fp = tn = fn = 0
    obs = {c: farm.observe(c) for c in farm.cells}
    med = M.median([o["ndvi"] for o in obs.values()])
    for c, cell in farm.cells.items():
        o = obs[c]
        pred, y = mdl.prob(o["ndvi"] - med, o["moisture"], o["pest"]) > 0.5, M.true_label(cell)
        tp += pred and y; fp += pred and not y; tn += (not pred) and not y; fn += (not pred) and y
    rec = tp / (tp + fn) if tp + fn else None
    spec = tn / (tn + fp) if tn + fp else None
    bal = (rec + spec) / 2 if rec is not None and spec is not None else None
    return rec, spec, bal, tp + fn, tp


def main(days=60, seeds=(1, 2, 3, 4, 5)):
    rows = []
    for seed in seeds:
        pol = load_policy(); pol["field"]["seed"] = seed
        d = tempfile.mkdtemp(); worst_rec, bals, stressed_days, pos_total, tp_total = 1.0, [], 0, 0, 0
        for _ in range(days):
            run_cycle(pol, d)
            farm = store.load_farm(seed, pol["field"]["size"], d)
            rec, spec, bal, npos, tp = full_field_scores(d, farm)
            if npos >= 1:
                bals.append(bal); pos_total += npos; tp_total += tp
            if npos >= 15:                     # worst-day recall only on days with a real outbreak (1-5 borderline cells is noise)
                stressed_days += 1; worst_rec = min(worst_rec, rec)
        m = [r for r in store.read_jsonl("metrics.jsonl", d) if r.get("ok")]
        rows.append((seed, stressed_days, worst_rec, sum(bals) / len(bals) if bals else float("nan"), tp_total / max(1, pos_total),
                     min(r["accuracy"] for r in m), m[-1]["model_version"]))
        print(f"seed {seed}: outbreak_days={stressed_days:2d}  pooled_recall={rows[-1][4]:.2f}  worst_outbreak_recall={worst_rec:.2f}  "
              f"mean_bal_acc={rows[-1][3]:.3f}  model_v={rows[-1][6]}", flush=True)
    print(f"OVERALL pooled_recall={sum(r[4] for r in rows)/len(rows):.2f}  worst_outbreak_recall={min(r[2] for r in rows):.2f}  mean_bal_acc={sum(r[3] for r in rows)/len(rows):.3f}")


if __name__ == "__main__":
    a = sys.argv[1:]
    main(int(a[0]) if a else 60, tuple(int(x) for x in a[1:]) or (1, 2, 3, 4, 5))
