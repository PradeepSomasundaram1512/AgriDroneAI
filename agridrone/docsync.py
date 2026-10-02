"""Keep the documentation's numbers honest: tables that quote measured results are GENERATED from docs/benchmark.json between markers,
and a test (plus `agridrone docs-sync --check` in CI) fails if the committed docs ever drift from the committed data."""

import json
import re
from pathlib import Path

START, END = "<!-- BENCH:START -->", "<!-- BENCH:END -->"


def bench_table(bench):
    rows = ["| Strategy | Yield | Water (mm/patch) | Sprays/patch | Flight hours |", "|---|---|---|---|---|"]
    for s in bench["strategies"]:
        fh = s.get("flight_hours")
        rows.append(
            f"| {s['name']} | {s['yield'] * 100:.1f}% ± {s['yield_sd'] * 100:.1f} | {s['water_mm']} | {s['sprays']} | {fh if fh else '-'} |"
        )
    return "\n".join(rows)


def render(text, bench):
    """Replace the generated block in `text`. Raises if the markers are missing."""
    block = f"{START}\n{bench_table(bench)}\n{END}"
    new, n = re.subn(re.escape(START) + r".*?" + re.escape(END), lambda _m: block, text, flags=re.S)
    if n != 1:
        raise ValueError("docs need exactly one BENCH:START / BENCH:END marker pair")
    return new


def sync(doc_path, bench_path, write=True):
    """-> True if the doc already matched. With write=True the doc is rewritten when it did not."""
    doc, bench = Path(doc_path), json.loads(Path(bench_path).read_text())
    cur = doc.read_text()
    new = render(cur, bench)
    if write and new != cur:
        doc.write_text(new)
    return new == cur
