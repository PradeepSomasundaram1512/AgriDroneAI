# Ground station & real-drone adapter

```
GitHub Actions (cloud)                    Farm ground station (Pi / laptop)             Vehicles
autopilot: plan → safety gate  ──queue──▶ git pull → re-validate → interlocks → fly ──▶ PX4 via MAVSDK
state/queue.jsonl  ◀── state/flights.jsonl (results, pushed back to git) ◀───────────── telemetry
```
The cloud **cannot** actuate anything; it only appends to `state/queue.jsonl`. The ground station re-runs the safety gate itself.

## Independent interlocks (all must pass to leave the ground)
1. `autonomy_level` ≠ `simulation` and `hardware.enabled: true` in `config/policy.json` (repo owner)
2. `AGRIDRONE_ARMED=1` in the ground station's environment (whoever physically controls the machine)
3. `kill_switch` false
4. Queue record is fresh (<`max_queue_age_h`), newest, and unflown (older ones are logged `superseded`/`expired`, never flown)
5. Approval: `approval: auto` only when level is `autonomous`; otherwise a human runs `agridrone fly --approve <id>`
6. Safety gate: no-fly cells, field geofence, battery reserve, altitude separation
7. Per-drone: GPS+home lock, telemetry battery ≥ mission energy + reserve, else **refused**
8. In flight: battery below reserve (polled every 2 s), telemetry silence/link loss, link error or timeout ⇒ **return-to-launch** (time-bounded) and logged `aborted`; if RTL can't be sent the record says so and the autopilot failsafe is the backstop
9. Autopilot link-loss failsafe (`NAV_DLL_ACT`=return) is set and read back before arming; if that fails the drone is **refused**

## Setup
```bash
pip install -e ".[hardware]"
# edit config/policy.json: hardware.origin (field SW corner lat/lon), hardware.links (one per drone)
agridrone fly --dry-run                 # builds waypoints, connects to nothing
sudo cp deploy/agridrone-ground.service /etc/systemd/system/ && sudo systemctl enable --now agridrone-ground
```
Run the unattended daemon with a git credential that can push to this repo (deploy key) so flight results flow back.

## What is and isn't validated
- ✅ Flight logic (preflight, refusal, battery RTL, timeout, interlocks, queue rules): unit-tested with a fake vehicle.
- 🟡 `MavsdkLink` is validated against **PX4 SITL** (`scripts/sitl_smoke.py`, arm64 image on Colima): normal flight, back-to-back sorties, flight after an autopilot restart, and a forced-timeout abort with return-to-launch. **Not** yet run on a real aircraft. Failure modes (`scripts/sitl_failures.sh lowbat|linkloss`): **low battery** mid-flight aborts at the reserve, commands RTL and confirms landing; **ground-link loss** (MAVSDK server killed mid-flight) aborts within a second, reports honestly that RTL could not be sent, and PX4's own data-link-loss failsafe (Hold 5 s, then return) brought the drone down ~60 s later, well before the mission would have ended. Not yet exercised: multi-drone, GPS loss, wind.
- SITL findings that shaped the adapter: battery is now polled on its own timer (a progress-driven check reacted ~40 s late, at 10% instead of 25%); every gRPC call on a dead link can hang forever, so telemetry silence counts as link loss and RTL/abort calls are time-bounded; the adapter sets and verifies the autopilot's link-loss failsafe (`NAV_DLL_ACT`=return) before arming and refuses to fly if it can't; after a reboot PX4 kept the previous mission's progress, so a new mission never started (now: clear mission + reset current waypoint before every upload); progress/in_air streams can replay stale state (now: completion is only believed after confirmed takeoff and a matching waypoint count, and the drone must confirm landing).
- ❌ Payload (sprayer/valve) is a stub (`NullPayload` logs). Implement `PayloadDriver.trigger(action, cell)` for your hardware.
- ❌ Not covered: wind/weather limits, obstacle avoidance, takeoff/landing sites, regulatory approval (Part 107/137 or local), pesticide rules, spotter/line-of-sight requirements. Do not fly over people or property you don't control.
