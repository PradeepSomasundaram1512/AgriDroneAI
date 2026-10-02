"""Regression tests for problems found by the merciless self-review (each fails without its fix)."""

import gzip
import json

import pytest

from agridrone import docsync, store
from agridrone.agent import run_cycle
from agridrone.config import ROOT, load_policy
from agridrone.sim import Farm


# ---- unbounded logs in git
def test_rotation_is_lossless_and_keeps_the_newest_lines(tmp_path):
    for i in range(3000):
        store.append_jsonl("audit.jsonl", {"i": i, "pad": "x" * 40}, tmp_path)
    before = store.read_jsonl("audit.jsonl", tmp_path)
    arch = store.rotate("audit.jsonl", tmp_path, max_bytes=50_000, keep_lines=500)
    assert arch is not None and arch.suffix == ".gz"
    live = store.read_jsonl("audit.jsonl", tmp_path)
    old = [json.loads(line) for line in gzip.open(arch, "rt").read().splitlines()]
    assert len(live) == 500 and live[-1]["i"] == 2999 and old + live == before  # nothing lost, order preserved


def test_rotation_leaves_small_logs_alone_and_the_autopilot_stays_healthy(tmp_path):
    store.append_jsonl("audit.jsonl", {"a": 1}, tmp_path)
    assert store.rotate("audit.jsonl", tmp_path) is None and store.rotate("missing.jsonl", tmp_path) is None
    p = load_policy()
    p["field"]["size"] = 8
    for _ in range(5):
        assert run_cycle(p, tmp_path)["ok"]
    from agridrone.verify import verify

    assert verify(tmp_path) == []


# ---- docs must not lie
def test_docs_benchmark_table_matches_the_committed_benchmark_data():
    assert docsync.sync(ROOT / "docs" / "ARCHITECTURE.md", ROOT / "docs" / "benchmark.json", write=False), "run: agridrone docs-sync"


def test_docsync_rewrites_only_the_marked_block_and_demands_markers(tmp_path):
    bench = {"strategies": [{"name": "A", "yield": 0.9, "yield_sd": 0.01, "water_mm": 10, "sprays": 1.0, "flight_hours": 5.0}]}
    doc = tmp_path / "d.md"
    doc.write_text(f"before\n{docsync.START}\nOLD\n{docsync.END}\nafter\n")
    assert docsync.sync(doc, _write(tmp_path, bench)) is False  # drifted: rewritten
    text = doc.read_text()
    assert text.startswith("before\n") and text.endswith("after\n") and "OLD" not in text and "| A | 90.0% ± 1.0 | 10 | 1.0 | 5.0 |" in text
    assert docsync.sync(doc, tmp_path / "b.json") is True  # now consistent
    bad = tmp_path / "bad.md"
    bad.write_text("no markers here")
    with pytest.raises(ValueError):
        docsync.sync(bad, tmp_path / "b.json")


def _write(tmp_path, bench):
    f = tmp_path / "b.json"
    f.write_text(json.dumps(bench))
    return f


# ---- the benchmark's headline claims, as a cheap regression guard (a small farm, one season)
def test_the_headline_claims_still_hold_on_a_small_farm():
    """If a change breaks the core promise (act only where needed, keep the crop) this fails in CI long before anyone re-runs the benchmark."""
    import os
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    os.environ["AGRIDRONE_OFFLINE"] = "1"
    import benchmark as B

    none = B.passive(1, 100, 12, False)
    cal = B.passive(1, 100, 12, True)
    ai = B.agent(1, 100, 12, 3)
    assert ai[0] >= none[0]  # never worse than doing nothing
    assert ai[0] >= cal[0] - 0.04  # keeps (nearly) the crop that blanket treatment keeps
    assert ai[1] <= 0.5 * cal[1]  # ...with at most half the water
    assert ai[2] <= 0.5 * cal[2]  # ...and at most half the spray


def test_hidden_physics_perturbations_change_outcomes_but_stay_deterministic():
    a, b = Farm.create(12, 1), Farm.create(12, 1, physics={"onset_moisture": 0.38, "et_scale": 1.6})
    c = Farm.create(12, 1, physics={"onset_moisture": 0.38, "et_scale": 1.6})
    from agridrone import weather

    for _ in range(60):
        w = weather.synthetic(a.day + 1, 1)
        a.step(w)
        b.step(w)
        c.step(w)
    assert b.mean_yield() < a.mean_yield() and b.mean_yield() == c.mean_yield()
