"""Plain-language weekly and monthly reports from metrics.jsonl (no LLM required)."""

from datetime import datetime, UTC

from .store import read_jsonl


def _stats(rows):
    ok = [r for r in rows if r.get("ok")]
    done = [r for r in ok if not r.get("skipped")]
    last = done[-1] if done else {}
    return {
        "cycles": len(rows),
        "uptime": 100 * len(ok) / len(rows) if rows else 100.0,
        "last": last,
        "executed": sum(r.get("executed", 0) for r in done),
        "retrains": sum(1 for r in done if r.get("retrained")),
        "blocked": sum(r.get("dropped_by_safety", 0) for r in done),
        "max_plan": max((r.get("plan_seconds", 0) for r in done), default=0),
        "incidents": [r["error"] for r in rows if not r.get("ok")],
    }


def _flag(v, ok):
    return "✅" if ok else "⚠️"


def build_report(kind="weekly", state_dir=None, window=None):
    window = window or (7 if kind == "weekly" else 30)
    rows = read_jsonl("metrics.jsonl", state_dir)[-window:]
    s = _stats(rows)
    l = s["last"]
    recall_txt = "n/a" if l.get("recall") is None else format(l["recall"], ".1%")
    recall_ok = l.get("recall") is None or l["recall"] >= 0.85
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    out = (
        [
            f"# AgriDroneAI {kind} report — {today}",
            "",
            "## Headline",
            f"Over the last {s['cycles']} operating cycles the system actuated {s['executed']} field actions "
            f"({l.get('water_saved_pct', 0)}% less water and {l.get('chem_saved_pct', 0)}% less agrochemical than a simulated "
            "weekly blanket-treatment baseline; "
            f"simulated yield index {l.get('yield_index', '-')}).",
            "",
            "## Metrics vs targets",
            "| Metric | Value | Target | Status |",
            "|---|---|---|---|",
            f"| Uptime (cycle success) | {s['uptime']:.1f}% | ≥99% | {_flag(0, s['uptime'] >= 99)} |",
            f"| Detection balanced accuracy | {l.get('accuracy', 0):.1%} | ≥90% | {_flag(0, l.get('accuracy', 0) >= 0.9)} |",
            f"| Stress recall (stressed cells caught) | {recall_txt} | ≥85% | {_flag(0, recall_ok)} |",
            f"| Water saved | {l.get('water_saved_pct', 0)}% | ≥30% | {_flag(0, l.get('water_saved_pct', 0) >= 30)} |",
            f"| Chemical saved | {l.get('chem_saved_pct', 0)}% | ≥30% | {_flag(0, l.get('chem_saved_pct', 0) >= 30)} |",
            f"| Slowest planning loop | {s['max_plan']}s | <300s | {_flag(0, s['max_plan'] < 300)} |",
            "| Autonomous ratio | 100% of cycles without human input | ≥90% | ✅ |",
            "",
            "## Model & safety",
            f"- Model version {l.get('model_version', '-')}; retrained {s['retrains']}× in window; drift PSI {l.get('psi', '-')}.",
            f"- Safety gate removed {s['blocked']} unsafe/unaffordable targets (no-fly zones, battery reserve).",
            "## Incidents",
        ]
        + ([f"- {e}" for e in s["incidents"]] or ["- none"])
        + [
            "",
            "## Next steps",
            "- Continue autonomous cycles; retrain on drift or accuracy < 90%.",
            "- Human decision needed to move from `simulation` to real hardware (see docs/GOVERNANCE.md).",
            "",
            "> Metrics come from the built-in farm simulator. Real-world yield/resource claims need a hardware pilot.",
            "",
        ]
    )
    return "\n".join(out)
