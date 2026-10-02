# Governance, permissions & incident response

## Autonomy ladder (`config/policy.json` → `autonomy_level`)
| Level | Behaviour | Who can change it |
|---|---|---|
| `simulation` (default) | Missions execute against the built-in simulator only | repo owner |
| `supervised` | Missions are queued in `state/queue.jsonl`; a human runs `agridrone fly --approve <id>` on the ground station | repo owner |
| `autonomous` | Queue records are `approval: auto`; the farm ground station flies them unattended (needs `hardware.enabled` **and** `AGRIDRONE_ARMED=1` there; see GROUND_STATION.md) | repo owner, only after SITL + supervised pilot + regulatory sign-off |

The agent never edits `policy.json`. It may only nudge thresholds inside hard bounds (`advisor.py`) via `state/threshold_overrides.json`, all logged.

## Permissions actually used
- GitHub Actions `contents: write` (commit state/reports), `issues: write` (reports/incidents). Nothing else.
- Optional secret `ANTHROPIC_API_KEY` for the plain-language advisor. Without it the agent is fully deterministic.
- **No cloud account, spend, or credentials are provisioned.** Adding AWS/GKE hosting is a deliberate human decision (see ARCHITECTURE.md → Path to production).

## Controls
- **Kill switch:** set `"kill_switch": true` in policy.json (or revert the autopilot commit). Cycles skip and log.
- **Safety gate:** `safety.py` enforces no-fly cells, battery reserve, unique altitude layers and ≥ separation; unsafe targets are dropped, violations block the cycle.
- **Model canary + rollback:** a retrained model is promoted only if it scores ≥ the incumbent on fresh labelled data.
- **Audit:** `state/audit.jsonl` (every decision) + git history (every state change).

## Incident response
1. Failed cycle → `incident` issue opened automatically (autopilot.yml); next scheduled run proceeds unaffected.
2. Silent system → watchdog opens an issue after 36h.
3. Suspected bad behaviour → set kill switch, `git revert` the offending state commit, review `audit.jsonl`.
4. Corrupt state → delete `state/farm.json` to re-seed; incident is logged, not fatal.

## Operational safeguards added in 0.2.0
- `config/policy.json` is validated on every load (battery reserve 10-60%, thresholds in range, no-fly cells inside the field and never the home pad, one hardware link per drone...). A bad edit fails fast with every problem listed.
- The autopilot **verifies state before committing** (valid JSON, no NaN, correct patch count, sane ranges); a corrupt file blocks the commit and opens an incident.
- Simulator upgrades **migrate state** (`state/archive-v1/`, `state/version.json`) rather than mixing physics.
- Sensor faults are detected, repaired from neighbours, and reported (`sensor_faults` in the audit log; sensors flagged for maintenance appear on the dashboard).
- Hardware modes plan a **single sortie**: a battery swap between flights needs a human at the aircraft.

## Added in 0.3.0
- **Airspace safety is part of the safety gate**: a plan is rejected if the 3D replay finds two drones within the clearance, whatever the planner proposed.
- **Battery feasibility is checked flight by flight** (including charging between sorties); a plan that would breach the reserve is blocked and logged as `safety_block`.
- **Real satellite data** is refreshed by the autopilot only when stale (default 5 days), never blocks a cycle, and is clearly labelled real vs simulated everywhere.
- New policy knobs are validated: `fleet.charging.*`, `fleet.pad_spacing_m` (>= 1.5x clearance), speeds, `imagery.*`, `field.location`.
- `state/fleet.json` (battery state) is integrity-checked before every commit.

## Added in 0.4.0
- **Wind is part of the safety gate**: the gate recomputes each mission's energy for the wind it was planned for, rejects plans above the flight limit, rejects spraying above the drift limit, and widens traffic clearance in gusts. Wind limits are validated against the aircraft's airspeed.
- **GPS loss has a fixed protocol** (freeze payload, hold, land in place, send lower drones home); grounded drones are tracked in `state/fleet.json` (`grounded_until`, `incidents`) and checked by `verify`.
- The audit log records `grounded_by_wind`, `spray_deferred_by_wind` and `gps_loss`, shown in plain language on the dashboard.
- Hardware: PX4's wind failsafe is armed (it is off by default); measured wind above the limit refuses the flight.
