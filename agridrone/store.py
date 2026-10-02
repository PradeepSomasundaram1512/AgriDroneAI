"""File-backed state (git is the audit trail + durable store for the autopilot)."""
import json
from pathlib import Path

from .config import STATE_DIR
from .sim import Cell, Farm


def _p(name, d):
    return Path(d or STATE_DIR) / name


def save_farm(f: Farm, d=None):
    p = _p("farm.json", d)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"size": f.size, "seed": f.seed, "day": f.day, "water": f.water_used, "chem": f.chem_used,
            "rng": [f.rng.getstate()[0], list(f.rng.getstate()[1]), f.rng.getstate()[2]],
            "cells": [[x, y, c.ndvi, c.moisture, c.pest, c.yield_potential] for (x, y), c in f.cells.items()]}
    p.write_text(json.dumps(data))


def load_farm(seed, size, d=None) -> Farm:
    p = _p("farm.json", d)
    if not p.exists():
        return Farm.create(size, seed)
    data = json.loads(p.read_text())
    f = Farm(size=data["size"], seed=data["seed"], day=data["day"], water_used=data["water"], chem_used=data["chem"])
    import random
    f.rng = random.Random()
    v, internal, gauss = data["rng"]
    f.rng.setstate((v, tuple(internal), gauss))
    for x, y, n, m, pe, yp in data["cells"]:
        f.cells[(x, y)] = Cell(n, m, pe, yp)
    return f


def append_jsonl(name, rec, d=None):
    p = _p(name, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


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
    p.write_text(json.dumps(obj, indent=1))
