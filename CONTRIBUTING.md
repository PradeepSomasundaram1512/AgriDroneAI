# Contributing

Thanks for looking. This is a **simulation-first** research project: please keep claims honest (simulation vs real aircraft).

## Set up
```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
AGRIDRONE_OFFLINE=1 PYTHONPATH=.:tests .venv/bin/pytest -q
.venv/bin/ruff check agridrone tests scripts && .venv/bin/ruff format agridrone tests scripts
```

## Ground rules
- **Safety first.** Anything that touches the safety gate, collision checker, wind/GPS handling or the hardware adapter needs a test that fails without the change.
- **No silent behaviour change.** `tests/test_golden_cycle.py` records the autopilot's day-by-day behaviour; if you change behaviour on purpose, regenerate it (command in that file) and say why in the PR.
- **Measure before you claim.** New features should come with a benchmark or eval script result, including when the result is "no benefit" (the lookahead planner is an example).
- **Docs follow data.** Benchmark tables in `docs/ARCHITECTURE.md` are generated: run `agridrone docs-sync` after `scripts/benchmark.py --save`; CI checks them.
- Keep line length at 140 and match the surrounding style. No secrets in commits.

## Good first issues
- Real elevation data (DEM) for `agridrone/terrain.py`.
- A scenario where AI beats a sensor-triggered farmer (sparse sensors, spreading pests).
- Noisy weather forecasts in the simulator.
- A real-world dataset to validate the soil-moisture model.

## Reporting problems
Use GitHub issues. For security problems see [SECURITY.md](SECURITY.md).
