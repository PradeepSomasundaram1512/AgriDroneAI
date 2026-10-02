# AgriDroneAI

Autonomous precision-agriculture drone network. **Simulation-first, self-operating, and honest about what is proven.**

A scheduled GitHub Actions autopilot runs one operating cycle a day: read sensors → repair bad data → classify crop stress →
plan multi-drone, multi-flight missions (battery swaps, no-fly detours) → safety-gate them → act → retrain on drift → audit.
State is verified and committed to git, reports are published as issues, incidents open themselves, and a watchdog notices
if the autopilot goes silent. No person (or Claude session) has to stay online.

| | |
|---|---|
| **Dashboard** | [`docs/dashboard.html`](docs/dashboard.html): plain-language, animated drone replay, time-lapse, benchmark. Regenerated daily. |
| **Does it work?** | `scripts/benchmark.py`: paired benchmark vs "do nothing" and a fixed schedule on identical weather. Results below. |
| **Real drones** | PX4/MAVSDK adapter + unattended ground station, validated on PX4 SITL (not on real aircraft). [`docs/GROUND_STATION.md`](docs/GROUND_STATION.md) |
| **Control surface** | [`config/policy.json`](config/policy.json): autonomy level, kill switch, fleet, thresholds. Validated on every load. |

## Measured results (simulation: 120-day season, 6 random farms, identical weather per strategy)
See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#benchmark) for the table and caveats. Reproduce with:
```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
PYTHONPATH=. .venv/bin/pytest -q --cov          # 72 tests, 95% coverage, gate at 85%
PYTHONPATH=. .venv/bin/python scripts/benchmark.py 120 6
PYTHONPATH=. .venv/bin/python scripts/eval_model.py      # stress model scored against the whole field
PYTHONPATH=. .venv/bin/python scripts/eval_quality.py    # sensor-fault detector recall/false alarms
PYTHONPATH=. .venv/bin/python -m agridrone.cli cycle -n 30 && python -m agridrone.cli dashboard
```

## What is real and what is not
- **Real:** the control software, safety gate, planner, data-quality layer, model lifecycle, autopilot, CI, container, and the PX4 adapter
  (flown in PX4 SITL: normal, restart, low battery, link loss, 3 concurrent drones).
- **Simulated:** the farm itself (weather is synthetic or live from Open-Meteo), sensors and the crop. Yield/water/spray numbers are
  **simulator outputs, not field results**. A hardware pilot is required before any real-world claim.
- **Not built:** real imagery ingestion, real sprayer/valve driver (stub), cloud deployment (needs your credentials and spend),
  regulatory approval, wind/GPS-loss handling, obstacle avoidance.

Docs: [Architecture](docs/ARCHITECTURE.md) · [Governance & incident response](docs/GOVERNANCE.md) · [Ground station](docs/GROUND_STATION.md) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md)
Optional: add repo secret `ANTHROPIC_API_KEY` to enable the bounded Claude advisor (it can only nudge thresholds within hard limits).
