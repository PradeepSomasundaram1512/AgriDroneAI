# AgriDroneAI

Autonomous precision-agriculture drone network, **simulation-first and self-operating**.
A scheduled GitHub Actions autopilot runs one observe → classify → plan → safety-gate → act → learn cycle daily, commits state and audit logs, retrains on drift, publishes weekly/monthly reports as issues, and opens incidents when something breaks — no person (or Claude session) has to stay online.

```bash
python -m venv .venv && .venv/bin/pip install pytest
PYTHONPATH=. .venv/bin/pytest -q
PYTHONPATH=. .venv/bin/python -m agridrone.cli cycle -n 30
PYTHONPATH=. .venv/bin/python -m agridrone.cli report weekly
```

Docs: [Architecture](docs/ARCHITECTURE.md) · [Governance & incident response](docs/GOVERNANCE.md)
Control surface: [config/policy.json](config/policy.json) (autonomy level, kill switch, thresholds).
Optional: add repo secret `ANTHROPIC_API_KEY` to enable the bounded Claude advisor.
