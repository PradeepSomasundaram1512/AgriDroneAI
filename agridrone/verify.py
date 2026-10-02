"""State integrity check, run by the autopilot BEFORE it commits: a corrupt state file must never reach the repo,
because the next scheduled run would load it. Returns a list of problems (empty == healthy)."""

import json
import math
from pathlib import Path

from .config import STATE_DIR


def _finite(o):
    if isinstance(o, float):
        return math.isfinite(o)
    if isinstance(o, dict):
        return all(_finite(v) for v in o.values())
    if isinstance(o, (list, tuple)):
        return all(_finite(v) for v in o)
    return True


def verify(state_dir=None):
    d, problems = Path(state_dir or STATE_DIR), []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception as e:
            problems.append(f"{f.name}: not valid JSON ({e})")
            continue
        if not _finite(data):
            problems.append(f"{f.name}: contains NaN/Infinity")
    for f in sorted(d.glob("*.jsonl")):
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except Exception:
                problems.append(f"{f.name}:{i}: line is not valid JSON")
                break
    farm = d / "farm.json"
    if farm.exists() and not any("farm.json" in p for p in problems):
        fd = json.loads(farm.read_text())
        n = fd.get("size", 0)
        if len(fd.get("cells", [])) != n * n:
            problems.append(f"farm.json: {len(fd.get('cells', []))} cells but size {n} needs {n * n}")
        for row in fd.get("cells", []):
            if not (0 <= row[3] <= 0.6 and 0 <= row[4] <= 1.0 and 0 <= row[5] <= 1.0 and 0 <= row[2] <= 1.0):
                problems.append(f"farm.json: patch {row[:2]} has out-of-range state")
                break
    model = d / "model.json"
    if model.exists() and not any("model.json" in p for p in problems):
        md = json.loads(model.read_text())
        if "tree" not in md and "w" not in md:
            problems.append("model.json: neither a tree nor legacy weights")
    return problems
