# Changelog

## 0.5.0 (from a merciless self-review)
- **Honest baseline**: the benchmark now includes a *smart farmer* (same soil sensors, waters when dry, no drones). Result: the AI matches that farmer on yield and profit and saves a little water; against it the drones do not pay back under placeholder prices. Money view, briefing and docs say so. The fixed-schedule comparison is kept but is no longer the headline.
- **Robustness sweep** (`scripts/robustness.py`): 10 variants of the hidden crop physics. Finding: fixed thresholds lose up to ~4 yield points when the crop is more sensitive than assumed. Self-calibrating thresholds (`calibration.enabled`) recover some of it but spend much more water; left **off**.
- **Satellite cross-check** for slow sensor drift (virtual satellite pass in the simulator): bias recall 0.53 -> 0.88; with 4% faulty sensors yield 0.990 -> 0.991 and water 113 -> 109 mm. On by default.
- **Terrain and obstacles** (`terrain.py`): climb energy shared by planner and gate, per-altitude-layer obstacle blocking, elevation grids.
- **Lookahead planner** (forecast wind blackouts): measured no benefit (0.9936 vs 0.9939), left **off** (`lookahead.enabled`).
- **Fleet sizing**: `agridrone size --hectares N`. Economics gained payback against the smart farmer.
- Engineering: `run_cycle` split into `Cycle` stages guarded by a golden-record test; lossless log rotation; docs tables generated from `docs/benchmark.json` (`agridrone docs-sync --check` in CI); dashboard JS executed in tests; export geometry fixes; sampling-margin fix in the collision checker.
- Not built, on purpose: satellite-cued scouting (every patch already has a sensor in this simulator, so it could add nothing measurable).

## 0.4.0
- **Wind handling**: wind triangle energy/time per leg (headwind/tailwind/crosswind, infeasible legs), wind shear by altitude layer, flight and spray go/no-go limits, gust-widened collision clearance, spray drift in the simulator, wind-aware planner and an independent re-check in the safety gate, PX4 wind failsafe armed on hardware, measured-wind pre-flight check. Real wind forecasts via Open-Meteo.
- **GPS-loss handling**: pre-flight GPS quality, in-flight watcher (freeze payload, hold, resume or land in place), lower drones ordered home, grounded-drone tracking and recovery, simulator outages, tested on real PX4 SITL.
- Bug found by the season benchmark and fixed with a regression test: planner/gate energy mismatch from a rounded wind value.
- Dashboard: wind arrow and panel, GPS-loss replay and panel, plain-language events. Policy validation for wind/GPS settings. 170+ tests.

## 0.3.0
- **Collision avoidance**: per-drone launch pads, time-resolved 3D separation replay, launch staggering / pre-landing holds, one-at-a-time fallback; enforced by the safety gate and the ground station.
- **Rechargeable drones**: persistent state of charge, charging or pack swap between flights, overnight recharge, battery wear; planner budgets from it and plans are verified flight by flight.
- **Real satellite imagery** (Sentinel-2 via STAC + COG windowed reads, cloud-masked, NDVI/NDMI, zones, change detection, scouting list), dashboard panel, twin seeding, sensor cross-check.
- Dashboard: pads, safety rings, charging bars, airspace + fleet panels, satellite view.
- Policy validation for all new knobs; `fleet.json` integrity check; 110+ tests.

## 0.2.0
- **Honest benchmark** (`scripts/benchmark.py`): paired strategies on identical weather (separate weather/sensor RNG streams); replaced the strawman baseline.
- **Digital twin v2**: soil water balance (rain, FAO-56-style ET, drainage, spatially varying soil), temperature-driven spreading pests, crop growth curve,
  optional live weather (Open-Meteo) with cache and offline fallback.
- **Planner v2**: multi-sortie battery-swap planning, cheapest-insertion + 2-opt routing, load balancing, forecast-aware irrigation, incremental O(1) insertion cost,
  and **no-fly-zone leg checking with detour waypoints** (previously only targets were checked).
- **Sensor data-quality layer**: range / spatial-outlier / stuck / jump detection, quarantine, neighbour repair, maintenance reporting; fault injection in the simulator.
- **Stress model fixed** (class-balanced tree, honest evaluation, NDVI anomaly, rollback). Whole-field balanced accuracy 0.75 -> 0.91.
- **Production hardening**: policy validation, versioned state migration, pre-commit state verification, lint/format/coverage/security/Docker CI, Python 3.11-3.13 matrix.
- **Dashboard**: plain-language, animated multi-wave drone replay, time-lapse, weather, sensor health, measured benchmark.

## 0.1.0
- Autopilot, safety gate, PX4/MAVSDK adapter and ground station validated on PX4 SITL, reports, watchdog.
