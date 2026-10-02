"""How much does the charging setup matter? Same weather/farms, different charger power and swap vs charge-in-place.
PYTHONPATH=. python scripts/sweep_charging.py"""

import os
import statistics as st
import tempfile
import time

os.environ["AGRIDRONE_OFFLINE"] = "1"
from agridrone import store
from agridrone.agent import run_cycle
from agridrone.config import load_policy


def run(seed, chg, days=120, size=24, drones=3):
    pol = load_policy()
    pol["field"]["seed"], pol["field"]["size"], pol["fleet"]["drones"] = seed, size, drones
    pol["fleet"]["charging"] = {"mode": "charge", "rate_w": 90, "turnaround_min": 45, "swap_min": 3, **chg}
    pol["weather"]["source"] = "synthetic"
    d = tempfile.mkdtemp()
    t = time.time()
    peak = 0
    for _ in range(days):
        r = run_cycle(pol, d)
        assert r["ok"], r
        peak = max(peak, r["targets"] - r["executed"])
    f = store.load_farm(seed, size, d)
    fl = store.load_json("fleet.json", None, d)
    return f.mean_yield(), f.water_used / size / size, peak, (time.time() - t) / days, min(x["health"] for x in fl["drones"])


if __name__ == "__main__":
    print("charging setup            | yield  water mm  peak backlog  cycle s  battery health after 120 days")
    for name, chg in (
        ("spare packs (swap, 3 min)", {"mode": "swap"}),
        ("charger 200 W", {"rate_w": 200}),
        ("charger 90 W (default)", {}),
        ("charger 40 W", {"rate_w": 40}),
        ("charger 20 W", {"rate_w": 20}),
    ):
        r = [run(s, chg) for s in (1, 2, 3)]
        print(
            f"{name:25s} | {st.mean(x[0] for x in r):.3f}  {st.mean(x[1] for x in r):7.0f}  {max(x[2] for x in r):10d}  {st.mean(x[3] for x in r):7.2f}  {min(x[4] for x in r):.3f}",
            flush=True,
        )
