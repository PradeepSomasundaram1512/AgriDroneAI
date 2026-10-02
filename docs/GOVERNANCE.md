# Governance, permissions & incident response

## Autonomy ladder (`config/policy.json` → `autonomy_level`)
| Level | Behaviour | Who can change it |
|---|---|---|
| `simulation` (default) | Missions execute against the built-in simulator only | repo owner |
| `supervised` | Missions are written to `state/pending_missions.json`; a human approves before any hardware adapter acts | repo owner |
| `autonomous` | A real adapter (PX4/MAVSDK) may actuate. **No adapter ships**; the agent logs `autonomous_unavailable` | repo owner, after a hardware pilot + regulatory sign-off |

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
