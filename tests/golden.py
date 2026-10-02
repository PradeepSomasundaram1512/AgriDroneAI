"""Golden-record helper for refactoring safety: run deterministic seasons and return their behaviour as plain data."""

import tempfile

from agridrone import store
from agridrone.agent import run_cycle
from agridrone.config import load_policy

VOLATILE = {"ts", "plan_seconds"}  # wall-clock values


def season(days=70, **sections):
    p = load_policy()
    p["field"]["size"] = 14
    p["imagery"]["enabled"] = False
    for k, v in sections.items():
        sec, key = k.split("__")
        p[sec][key] = v
    d = tempfile.mkdtemp()
    rows = []
    for _ in range(days):
        r = run_cycle(p, d)
        rows.append({k: v for k, v in r.items() if k not in VOLATILE})
    farm = store.load_farm(p["field"]["seed"], 14, d, p["field"].get("sensor_fault_rate", 0.0))
    audit = [a["kind"] for a in store.read_jsonl("audit.jsonl", d)]
    return {"rows": rows, "yield": round(farm.mean_yield(), 9), "water": round(farm.water_used, 6), "chem": farm.chem_used, "audit": audit}


CONFIGS = {
    "default": {},
    "stress": {
        "gps__loss_per_flight_hour": 0.8,
        "field__sensor_fault_rate": 0.05,
        "calibration__enabled": True,
        "field__physics": {"onset_moisture": 0.35},
    },
}
