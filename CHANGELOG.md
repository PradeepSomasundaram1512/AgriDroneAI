# Changelog

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
