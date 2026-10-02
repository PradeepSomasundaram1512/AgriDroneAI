"""How good is the sensor-fault detector? Injected faults are known, so recall/precision are exact.
PYTHONPATH=. python scripts/eval_quality.py"""

import os
import tempfile

os.environ["AGRIDRONE_OFFLINE"] = "1"
from agridrone import quality, weather
from agridrone.sim import Farm


def evaluate(seed, days=60, rate=0.06, size=24):
    f, d = Farm.create(size, seed, fault_rate=rate), tempfile.mkdtemp()
    hit, miss = {}, {}  # per mode: faulty-day detections / misses
    fp = healthy = 0
    for _ in range(days):
        f.step(weather.synthetic(f.day + 1, seed))
        _, rep = quality.clean(f.observe_all(), d)
        quarantined = set(map(tuple, rep["quarantined"]))
        flagged = set(map(tuple, rep["cells"])) | quarantined
        for c in f.cells:
            fl = f.faults.get(c)
            if fl and f.day >= fl["start"] + 3:  # allow a few days of evidence (stuck/jump need history)
                (hit if c in flagged else miss).setdefault(fl["mode"], 0)
                (hit if c in flagged else miss)[fl["mode"]] += 1
            elif not fl:
                healthy += 1
                fp += c in flagged
    return hit, miss, fp, healthy


if __name__ == "__main__":
    H, Mi, FP, HL = {}, {}, 0, 0
    for seed in range(1, 6):
        h, m, fp, hl = evaluate(seed)
        for k, v in h.items():
            H[k] = H.get(k, 0) + v
        for k, v in m.items():
            Mi[k] = Mi.get(k, 0) + v
        FP += fp
        HL += hl
    print("fault mode   detected-days   recall")
    for k in sorted(set(H) | set(Mi)):
        a, b = H.get(k, 0), Mi.get(k, 0)
        print(f"{k:10s}   {a + b:9d}      {a / (a + b):.2f}")
    print(f"false alarms on healthy sensors: {FP}/{HL} sensor-days = {100 * FP / HL:.2f}%")
