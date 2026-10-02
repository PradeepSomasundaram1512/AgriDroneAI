"""What do wind and GPS loss actually cost, and does the handling keep the fleet safe?
1. SAFETY: plan ignoring wind, then apply real wind to the same routes -> how many flights would breach the battery reserve?
2. SEASON: calm air vs the synthetic wind climate (grounded days, spraying postponed, yield, water).
3. GPS: season with outages at increasing rates (drones landing in fields, work deferred, yield).
PYTHONPATH=. python scripts/eval_wind.py"""

import os
import random
import statistics as st
import tempfile

os.environ["AGRIDRONE_OFFLINE"] = "1"
from agridrone import planner, store
from agridrone import wind as W
from agridrone.agent import run_cycle
from agridrone.config import load_policy
from agridrone.safety import mission_energy


def targets(n, seed):
    r = random.Random(seed)
    cells = list({(r.randrange(2, 24), r.randrange(0, 24)) for _ in range(n)})
    return sorted([(r.random(), c, "irrigate") for c in cells], reverse=True)


def safety_eval():
    p = load_policy()
    usable = p["fleet"]["battery_wh"] * (1 - p["fleet"]["min_reserve_pct"] / 100)
    print("1. SAFETY: wind-blind planning vs reality (every flight, 8 wind directions x 4 farms)")
    print("   wind(10 m)   flights  would breach reserve   energy vs plan   | wind-aware planning: breaches")
    for speed in (1.0, 2.0, 3.0, 4.0, 5.0, 5.5):
        n = b = bw = 0
        ratio = []
        for ang in range(0, 360, 45):
            w = W.Wind(speed, speed * 1.2, ang)
            for seed in range(4):
                for m in planner.plan(targets(300, seed), p, sorties=1):  # wind-blind
                    e = mission_energy((0, 0), m.targets, p["fleet"], w.design(), m.altitude_m)
                    n += 1
                    b += e > usable
                    ratio.append(e / m.energy_wh)
                for m in planner.plan(targets(300, seed), p, sorties=1, wind=w):  # wind-aware
                    bw += mission_energy((0, 0), m.targets, p["fleet"], w.design(), m.altitude_m) > usable + 1e-6
        print(f"   {speed:4.1f} m/s    {n:5d}    {100 * b / n:12.0f}%        {100 * (st.mean(ratio) - 1):+8.0f}%        | {bw}")


def season(seed, days=120, size=24, wind_on=True, gps_rate=0.0, drones=3):
    p = load_policy()
    p["field"]["seed"], p["field"]["size"], p["fleet"]["drones"] = seed, size, drones
    p["weather"]["source"] = "synthetic"
    p["wind"]["enabled"] = wind_on
    p["gps"]["loss_per_flight_hour"] = gps_rate
    d = tempfile.mkdtemp()
    grounded = deferred = losses = blocked = 0
    down_days = 0
    for _ in range(days):
        r = run_cycle(p, d)
        assert r["ok"], r
        grounded += r["grounded_by_wind"]
        deferred += r["spray_deferred"] > 0
        losses += r["gps_losses"]
        down_days += r["drones_grounded"]
    blocked = sum(1 for a in store.read_jsonl("audit.jsonl", d) if a["kind"] == "safety_block")
    f = store.load_farm(seed, size, d)
    return f.mean_yield(), f.water_used / size / size, grounded, deferred, losses, down_days, blocked


def season_eval():
    seeds = (1, 2, 3)
    print("\n2. SEASON (120 days, 3 farms, identical weather): calm-air planning vs the wind climate")
    print("   setting             yield   water mm  no-fly days  spray-postponed days  safety blocks")
    for name, kw in (("wind ignored (calm)", {"wind_on": False}), ("wind handled", {"wind_on": True})):
        r = [season(s, **kw) for s in seeds]
        print(
            f"   {name:19s} {st.mean(x[0] for x in r):.3f}  {st.mean(x[1] for x in r):8.0f}  {st.mean(x[2] for x in r):10.1f}  {st.mean(x[3] for x in r):15.1f}  {sum(x[6] for x in r):10d}",
            flush=True,
        )
    print("\n3. GPS LOSS (wind handled): outages per flight-hour")
    print("   rate/h   yield   GPS losses/season  drone-days grounded  safety blocks")
    for rate in (0.0, 0.02, 0.1, 0.3, 1.0):
        r = [season(s, gps_rate=rate) for s in seeds]
        print(
            f"   {rate:5.2f}   {st.mean(x[0] for x in r):.3f}  {st.mean(x[4] for x in r):12.1f}  {st.mean(x[5] for x in r):16.1f}  {sum(x[6] for x in r):10d}",
            flush=True,
        )


if __name__ == "__main__":
    safety_eval()
    season_eval()
