"""Does the autopilot actually beat the alternatives?  Paired benchmark on identical weather (separate env/sensor RNG
streams, so strategies differ only in what they do).

  none        : no irrigation, no spraying
  calendar    : irrigate EVERY patch weekly (36 mm) + spray EVERY patch fortnightly (a common fixed schedule)
  thresholds  : the agent with its ML model switched off (plain agronomic threshold rules, same drones/planner)
  agent-1     : full agent, 1 sortie/day per drone
  agent-3     : full agent, 3 sorties/day per drone (battery swaps)

  PYTHONPATH=. python scripts/benchmark.py [days=120] [seeds=1..6]"""

import os
import statistics as st
import sys
import tempfile
import time

os.environ["AGRIDRONE_OFFLINE"] = "1"
from agridrone import store, weather
from agridrone.agent import run_cycle
from agridrone.config import load_policy
from agridrone.sim import Farm


def passive(seed, days, size, calendar):
    f = Farm.create(size, seed)
    for _ in range(days):
        f.step(weather.synthetic(f.day + 1, seed))
        if calendar:
            if f.day % 7 == 0:
                [f.apply(c, "irrigate") for c in f.cells]
            if f.day % 14 == 0:
                [f.apply(c, "spray") for c in f.cells]
    return f.mean_yield(), f.water_used, f.chem_used, 0.0


def agent(seed, days, size, sorties, model=True, fault_rate=0.0, quality=True):
    pol = load_policy()
    pol["field"]["seed"], pol["field"]["size"] = seed, size
    pol["fleet"]["sorties_per_day"], pol["model_enabled"] = sorties, model
    pol["weather"]["source"] = "synthetic"
    pol["field"]["sensor_fault_rate"], pol["quality_filter"] = fault_rate, quality
    d = tempfile.mkdtemp()
    t = time.time()
    ok = True
    for _ in range(days):
        ok &= bool(run_cycle(pol, d)["ok"])
    f = store.load_farm(seed, size, d, fault_rate)
    assert ok, "agent cycle failed"
    return f.mean_yield(), f.water_used, f.chem_used, (time.time() - t) / days


def main(days=120, seeds=(1, 2, 3, 4, 5, 6), size=24):
    strategies = {
        "none": lambda s: passive(s, days, size, False),
        "calendar": lambda s: passive(s, days, size, True),
        "thresholds": lambda s: agent(s, days, size, 3, model=False),
        "agent-1": lambda s: agent(s, days, size, 1),
        "agent-3": lambda s: agent(s, days, size, 3),
    }
    res = {k: [fn(s) for s in seeds] for k, fn in strategies.items()}
    print(f"\n{days}-day season, {size}x{size} patches, seeds {list(seeds)}; yield index 1.00 = no crop loss\n")
    print(f"{'strategy':11s} {'yield':>14s} {'water mm/patch':>15s} {'sprays/patch':>13s} {'cycle s':>8s}")
    n = size * size
    for k, v in res.items():
        y = [r[0] for r in v]
        print(
            f"{k:11s} {st.mean(y):.3f} ± {st.pstdev(y):.3f}   {st.mean(r[1] for r in v) / n:12.0f}   {st.mean(r[2] for r in v) / n:11.2f}   {st.mean(r[3] for r in v):6.2f}"
        )
    print("\npaired yield difference vs calendar (per seed): ", end="")
    for k in ("none", "thresholds", "agent-1", "agent-3"):
        d = [a[0] - b[0] for a, b in zip(res[k], res["calendar"])]
        print(f"\n  {k:11s} mean {st.mean(d):+.3f}  wins {sum(x > 0 for x in d)}/{len(d)}", end="")
    d = [a[0] - b[0] for a, b in zip(res["agent-3"], res["thresholds"])]
    print(f"\n  agent-3 vs thresholds-only (value of the ML model): mean {st.mean(d):+.4f}  wins {sum(x > 0 for x in d)}/{len(d)}")
    w, c = st.mean(r[1] for r in res["calendar"]), st.mean(r[1] for r in res["agent-3"])
    print(
        f"  water: agent-3 uses {100 * (1 - c / w):.0f}% less than calendar; "
        f"sprays: {100 * (1 - st.mean(r[2] for r in res['agent-3']) / st.mean(r[2] for r in res['calendar'])):.0f}% fewer"
    )
    names = {
        "none": "Do nothing",
        "calendar": "Fixed schedule",
        "thresholds": "AI agent, rules only",
        "agent-1": "AI agent, 1 flight/day",
        "agent-3": "AI agent, 3 flights/day",
    }
    return {
        "days": days,
        "seeds": len(seeds),
        "patches": n,
        "strategies": [
            {
                "id": k,
                "name": names[k],
                "yield": round(st.mean(r[0] for r in v), 4),
                "yield_sd": round(st.pstdev([r[0] for r in v]), 4),
                "water_mm": round(st.mean(r[1] for r in v) / n),
                "sprays": round(st.mean(r[2] for r in v) / n, 2),
            }
            for k, v in res.items()
        ],
        "ml_value": {"mean": round(st.mean(d), 4), "wins": f"{sum(x > 0 for x in d)}/{len(d)}"},
    }


def faults(days=120, seeds=(1, 2, 3, 4), size=24, rate=0.04):
    out = {"rate": rate, "variants": []}
    print(f"\nsensor faults: {int(rate * 100)}% of sensors fail (dead / stuck / biased / spiking) at random times\n")
    print(f"{'agent-3':22s} {'yield':>14s} {'water mm/patch':>15s} {'sprays/patch':>13s}")
    for label, q in (("quality filter OFF", False), ("quality filter ON", True)):
        v = [agent(s, days, size, 3, fault_rate=rate, quality=q) for s in seeds]
        y = [r[0] for r in v]
        print(
            f"{label:22s} {st.mean(y):.3f} ± {st.pstdev(y):.3f}   {st.mean(r[1] for r in v) / (size * size):12.0f}   {st.mean(r[2] for r in v) / (size * size):11.2f}",
            flush=True,
        )
        out["variants"].append({"name": label, "yield": round(st.mean(y), 4), "water_mm": round(st.mean(r[1] for r in v) / (size * size))})
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    save = "--save" in a
    a = [x for x in a if x != "--save"]
    if a and a[0] == "faults":
        faults()
        sys.exit()
    result = main(int(a[0]) if a else 120, tuple(range(1, int(a[1]) + 1)) if len(a) > 1 else (1, 2, 3, 4, 5, 6))
    if save:  # the dashboard shows these measured results
        import json
        import pathlib

        result["faults"] = faults()
        out = pathlib.Path(__file__).resolve().parent.parent / "docs" / "benchmark.json"
        out.write_text(json.dumps(result, indent=1))
        print("saved", out)
