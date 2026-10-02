"""Is the controller right, or only right about MY simulator?  The world's hidden physics (when the crop starts to suffer, how thirsty it
is, how fast pests grow, how noisy the sensors are) is changed WITHOUT telling the controller. A fixed-threshold controller tuned on the default
world should degrade when the crop's real stress point is higher than its trigger; a self-calibrating one should not.
  PYTHONPATH=. python scripts/robustness.py [seeds=2] [days=120] [--cal]"""

import os
import statistics as st
import sys

os.environ["AGRIDRONE_OFFLINE"] = "1"
sys.path.insert(0, os.path.dirname(__file__))
from benchmark import agent, passive, triggered  # noqa: E402

VARIANTS = [
    ("default world", {}),
    ("crop suffers earlier (onset 0.34)", {"onset_moisture": 0.34}),
    ("crop suffers much earlier (0.38)", {"onset_moisture": 0.38}),
    ("crop hardier (onset 0.26)", {"onset_moisture": 0.26}),
    ("pests hurt earlier (0.30)", {"onset_pest": 0.30}),
    ("pests hurt later (0.55)", {"onset_pest": 0.55}),
    ("thirstier crop (ET x1.4)", {"et_scale": 1.4}),
    ("less thirsty (ET x0.7)", {"et_scale": 0.7}),
    ("pests spread fast (0.10)", {"pest_growth": 0.10}),
    ("noisy sensors (0.05)", {"obs_noise": 0.05}),
]


def main(seeds=(1, 2), days=120, size=24, with_calibration=None):
    cal = {"calibration": {"enabled": True}} if with_calibration else None
    print(f"{days}-day season, {size}x{size} patches, seeds {list(seeds)}; yield index, water mm/patch\n")
    print(
        f"{'hidden physics':36s} {'calendar':>14s} {'smart farmer':>14s} {'AI fixed':>14s}" + (f" {'AI calibrating':>16s}" if cal else "")
    )
    rows = []
    for name, ph in VARIANTS:
        cal_r = [passive(s, days, size, True, ph) for s in seeds]
        trg_r = [triggered(s, days, size, ph) for s in seeds]
        ai_r = [agent(s, days, size, 3, physics=ph) for s in seeds]
        cells = [cal_r, trg_r, ai_r]
        if cal:
            cells.append([agent(s, days, size, 3, physics=ph, overrides=cal) for s in seeds])
        fmt = lambda r: f"{st.mean(x[0] for x in r):.3f}/{st.mean(x[1] for x in r) / (size * size):4.0f}"  # noqa: E731
        print(f"{name:36s} " + " ".join(f"{fmt(c):>14s}" for c in cells), flush=True)
        rows.append((name, [st.mean(x[0] for x in c) for c in cells]))
    print(
        "\nworst-case yield:",
        ", ".join(
            f"{lab}={min(r[1][i] for r in rows):.3f}"
            for i, lab in enumerate(["calendar", "smart farmer", "AI fixed", "AI calibrating"][: len(rows[0][1])])
        ),
    )
    return rows


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if x != "--cal"]
    main(tuple(range(1, int(a[0]) + 1)) if a else (1, 2), int(a[1]) if len(a) > 1 else 120, with_calibration="--cal" in sys.argv)
