"""File-backed state (git is the audit trail + durable store for the autopilot)."""

import json
import random
from pathlib import Path

from .config import STATE_DIR
from .sim import PHYSICS, Cell, Farm


def _p(name, d):
    return Path(d or STATE_DIR) / name


def _rng(r):
    st = r.getstate()
    return [st[0], list(st[1]), st[2]]


def _load_rng(d):
    r = random.Random()
    r.setstate((d[0], tuple(d[1]), d[2]))
    return r


STATE_VERSION = 2  # 2 = soil water balance / pest dynamics / sorties / sensor-quality model. 1 = original prototype physics.


def migrate(d=None):
    """Simulation state from an older model version must not be silently mixed with the new physics: archive it and
    start clean. (Git history keeps everything too.) Returns the archive path if a migration happened, else None."""
    import shutil

    root = Path(d or STATE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    v = root / "version.json"
    cur = json.loads(v.read_text())["version"] if v.exists() else (1 if (root / "farm.json").exists() else STATE_VERSION)
    if cur == STATE_VERSION:
        if not v.exists():
            v.write_text(json.dumps({"version": STATE_VERSION}))
        return None
    arch = root / f"archive-v{cur}"
    arch.mkdir(exist_ok=True)
    for f in root.iterdir():
        if f.is_file() and f.name != "version.json" and f.name != ".gitkeep":
            shutil.move(str(f), arch / f.name)
    v.write_text(json.dumps({"version": STATE_VERSION}))
    return arch


def save_farm(f: Farm, d=None):
    p = _p("farm.json", d)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "size": f.size,
        "seed": f.seed,
        "day": f.day,
        "water": f.water_used,
        "chem": f.chem_used,
        "rng": _rng(f.rng),
        "obs_rng": _rng(f.obs_rng),
        "fault_rng": _rng(f.fault_rng),
        "start_doy": f.start_doy,
        "init_scene": f.init_scene,
        "phys": f.phys,
        "faults": [[x, y, v["mode"], v["start"], v["bias"], v["frozen"]] for (x, y), v in f.faults.items()],
        "cells": [
            [
                x,
                y,
                round(c.ndvi, 5),
                round(c.moisture, 5),
                round(c.pest, 5),
                round(c.yield_potential, 5),
                round(c.soil, 4),
                round(c.host, 4),
            ]
            for (x, y), c in f.cells.items()
        ],
    }
    p.write_text(json.dumps(data))


def load_farm(seed, size, d=None, fault_rate=0.0, start_doy=120, physics=None) -> Farm:
    p = _p("farm.json", d)
    if not p.exists():
        return Farm.create(size, seed, start_doy, fault_rate, physics)
    data = json.loads(p.read_text())
    f = Farm(
        size=data["size"],
        seed=data["seed"],
        day=data["day"],
        water_used=data["water"],
        chem_used=data["chem"],
        start_doy=data.get("start_doy", 120),
        init_scene=data.get("init_scene"),
        phys={**PHYSICS, **data.get("phys", {})},
    )
    f.rng, f.obs_rng = _load_rng(data["rng"]), (_load_rng(data["obs_rng"]) if "obs_rng" in data else random.Random(data["seed"] + 9001))
    f.fault_rng = _load_rng(data["fault_rng"]) if "fault_rng" in data else random.Random(data["seed"] + 31337)
    f.faults = {
        (x, y): {"mode": m, "start": st, "bias": b, "frozen": tuple(fr) if fr else None} for x, y, m, st, b, fr in data.get("faults", [])
    }
    for row in data["cells"]:
        x, y, n, m, pe, yp = row[:6]
        f.cells[(x, y)] = Cell(n, m, pe, yp, row[6] if len(row) > 6 else 1.0, row[7] if len(row) > 7 else 1.0)
    return f


def append_jsonl(name, rec, d=None):
    p = _p(name, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def rotate(name, d=None, max_bytes=300_000, keep_lines=1200):
    """Keep an append-only log from growing forever in git: when it passes max_bytes, move everything except the newest keep_lines into a
    gzip archive (state/archive/<name>-<UTC timestamp>.jsonl.gz). Lossless: archive + live file always hold every line."""
    import gzip
    from datetime import UTC, datetime

    p = _p(name, d)
    if not p.exists() or p.stat().st_size <= max_bytes:
        return None
    lines = p.read_text().splitlines()
    if len(lines) <= keep_lines:
        return None
    old, new = lines[:-keep_lines], lines[-keep_lines:]
    arch = p.parent / "archive"
    arch.mkdir(exist_ok=True)
    out = arch / f"{Path(name).stem}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}.jsonl.gz"
    with gzip.open(out, "wt") as fh:
        fh.write("\n".join(old) + "\n")
    p.write_text("\n".join(new) + "\n")
    return out


def read_jsonl(name, d=None):
    p = _p(name, d)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def load_json(name, default, d=None):
    p = _p(name, d)
    return json.loads(p.read_text()) if p.exists() else default


def save_json(name, obj, d=None):
    p = _p(name, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, separators=(",", ":")))  # compact: state is committed daily, keep diffs small
