"""Water-vs-yield trade-off of the two action thresholds (the knob a farmer actually tunes).
PYTHONPATH=. python scripts/sweep_thresholds.py"""

import os
import statistics as st
import tempfile

os.environ["AGRIDRONE_OFFLINE"] = "1"
from agridrone import store
from agridrone.agent import run_cycle
from agridrone.config import load_policy


def run(seed, mi, ps, days=120, size=24):
    pol = load_policy()
    pol["field"]["seed"], pol["field"]["size"] = seed, size
    pol["weather"]["source"] = "synthetic"
    pol["thresholds"]["moisture_irrigate"], pol["thresholds"]["pest_spray"] = mi, ps
    d = tempfile.mkdtemp()
    for _ in range(days):
        run_cycle(pol, d)
    f = store.load_farm(seed, size, d)
    return f.mean_yield(), f.water_used / size / size, f.chem_used / size / size


if __name__ == "__main__":
    print("irrigate_below pest_above | yield   water mm/patch  sprays/patch   (calendar: 1.000, 612, 8.0)")
    for mi, ps in [(0.28, 0.50), (0.30, 0.50), (0.32, 0.50), (0.30, 0.45), (0.32, 0.42), (0.34, 0.40)]:
        r = [run(s, mi, ps) for s in (1, 2, 3)]
        print(
            f"   {mi:.2f}         {ps:.2f}   | {st.mean(x[0] for x in r):.3f}  {st.mean(x[1] for x in r):10.0f}   {st.mean(x[2] for x in r):9.2f}",
            flush=True,
        )
