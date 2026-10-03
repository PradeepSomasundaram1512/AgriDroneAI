# AgriDroneAI

Autonomous precision-agriculture drone network. **Simulation-first, self-operating, and honest about what is proven.**

> **Honest result (v0.5):** in the simulator the AI keeps ~99% of the crop and uses ~80% less water than a fixed schedule, but a farmer who simply reacts to the same soil sensors does about as well on yield and profit. The drones' value shown here is automation and coverage, not better agronomy. See `scripts/robustness.py` and `agridrone economics`.

A scheduled GitHub Actions autopilot runs one operating cycle a day: read sensors → repair bad data → classify crop stress →
plan multi-drone, multi-flight missions (own pads, 3D collision-checked, rechargeable batteries, wind-aware, no-fly detours) → safety-gate them → act → retrain on drift → audit.
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
PYTHONPATH=. .venv/bin/pytest -q --cov          # 110+ tests, gate at 85% coverage
PYTHONPATH=. .venv/bin/python scripts/benchmark.py 120 6
PYTHONPATH=. .venv/bin/python scripts/eval_model.py      # stress model scored against the whole field
PYTHONPATH=. .venv/bin/python scripts/eval_quality.py    # sensor-fault detector recall/false alarms
PYTHONPATH=. .venv/bin/python scripts/sweep_charging.py  # what the charger speed costs in yield
PYTHONPATH=. .venv/bin/python scripts/eval_wind.py       # what wind and GPS loss cost; proves the handling is safe
PYTHONPATH=. .venv/bin/python -m agridrone.cli imagery fetch && python -m agridrone.cli imagery map   # real Sentinel-2 (needs the 'imagery' extra)
PYTHONPATH=. .venv/bin/python -m agridrone.cli cycle -n 30 && python -m agridrone.cli dashboard
```

## What is real and what is not
- **Real:** the control software, safety gate, 3D collision checker, planner, battery/charging model, data-quality layer, model lifecycle, autopilot, CI, container, **real Sentinel-2 satellite ingestion**, and the PX4 adapter
  (flown in PX4 SITL: normal, restart, low battery, link loss, 3 concurrent drones).
- **Simulated:** the farm itself (weather is synthetic or live from Open-Meteo), ground sensors and the crop (the satellite panel is real data of a real field, but there is no ground truth to score the classifier on). Yield/water/spray numbers are
  **simulator outputs, not field results**. A hardware pilot is required before any real-world claim.
- **Not built:** real sprayer/valve driver (stub), cloud deployment (needs your credentials and spend),
  regulatory approval, obstacle avoidance (trees, wires) and GNSS spoofing/jamming detection. Wind *physics* is validated by tests and the model, not in PX4 SITL (no wind source there); GPS loss is tested on real PX4 SITL.

Explainer videos for non-experts (rendered from the project's real data): `python video/make_video.py` (4 min animated explainer), `python video/make_layman_video.py` (story + dashboard tour, natural neural voice via `pip install edge-tts`; add `--short` for a 2-minute cut). Edit the narration in `video/script.py` / `video/story_script.py`. Narration is AI-generated.

Docs: [Architecture](docs/ARCHITECTURE.md) · [Governance & incident response](docs/GOVERNANCE.md) · [Ground station](docs/GROUND_STATION.md) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md)
Optional: add repo secret `ANTHROPIC_API_KEY` to enable the bounded Claude advisor (it can only nudge thresholds within hard limits).
