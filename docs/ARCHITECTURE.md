# Architecture

```mermaid
flowchart LR
  subgraph Scheduler["GitHub Actions (always-on, no human needed)"]
    AP[autopilot.yml daily] --> CY
    WD[watchdog.yml] -.heartbeat.-> ST
    RP[reports.yml] --> RPT[reports/ + issues]
  end
  subgraph Agent["agridrone package"]
    CY[agent.run_cycle] --> OBS[observe sensors/drones]
    OBS --> ML[model.StressModel + PSI drift]
    ML --> PL[planner]
    PL --> SG[safety gate]
    SG -->|simulation| SIM[SimAdapter]
    SG -->|supervised| PEND[pending_missions.json → human]
    SG -->|supervised/autonomous| Q[state/queue.jsonl]
    Q --> GS[ground station at farm] --> HW[(PX4/MAVSDK)]
    CY --> ADV[optional Claude advisor, bounded]
  end
  CY --> ST[(state/: farm, model, audit, metrics — in git)]
  ST --> RP
```

| PRD item | Status here |
|---|---|
| Crop health monitoring | ✅ simulated NDVI/moisture/pest + classifier |
| Drone coordination | ✅ multi-drone, multi-sortie planner (cheapest insertion + 2-opt, battery swaps, forecast-aware, no-fly detours); altitude-layer separation |
| Safety/compliance | ✅ no-fly cells **and flight legs**, battery reserve, kill switch, audit log, policy validation |
| Adaptive ML pipeline | ✅ drift (PSI) + accuracy-triggered retraining, canary promote |
| LLM command & control | ✅ bounded advisor (optional); flight commands never LLM-originated |
| Reports | ✅ weekly/monthly, auto-published as issues |
| Dashboard | ✅ static `docs/dashboard.html` (KPIs, NDVI heatmap, audit feed), regenerated each cycle; Mapbox/React UI later |
| Real drones | 🟡 PX4/MAVSDK adapter + unattended ground station built and unit-tested with a fake vehicle; validated on PX4 SITL, **not yet on real hardware**; payload stubbed (docs/GROUND_STATION.md) |
| Edge IoT / real imagery | ⏳ needs hardware |
| AWS/K8s/Terraform | ⏳ deliberately not provisioned (cost + credentials); Dockerfile is ready |

## Path to production
1. Hardware pilot (1 km²) with a PX4 adapter in `supervised` mode.
2. Replace `sim.py` observations with real multispectral ingestion; replace the logistic model with a PyTorch/ONNX CNN behind `StressModel`'s interface.
3. Containerise (Dockerfile) → EKS/GKE via Terraform once a human approves spend.
4. Regulatory: drone operations (FAA Part 107/137 or local), pesticide-application rules.

## Honest limits
Yield/water/chemical figures are **simulator outputs**, not field evidence. The 20–30% yield and 30–50% savings goals are unvalidated until a real pilot.

## Model quality notes (stress classifier)
Measured with `scripts/eval_model.py` (60 days x 5 seeds, scored against the *whole field*, not the agent's own sample):

| | before | after |
|---|---|---|
| mean balanced accuracy | 0.75 | 0.91 |
| worst outbreak-day recall | 0.00 (missed every stressed cell) | 0.60 |
| pooled recall | n/a | 0.92 |

Root causes fixed: one-class cold start (a model that learned "everything is fine" scored 100%); evaluating on training data;
plain accuracy rewarding never flagging anything; rare positives evicted by a FIFO buffer; a linear model that cannot represent
"too dry OR too infested"; a shortcut on raw NDVI (it trends up with crop growth, now NDVI anomaly vs field median);
a stale evaluation set that hid the problem; and a planner that depended on the model (thresholds now always act).
Honest ceiling: sensor noise (+-0.02) at the decision thresholds limits any detector to ~0.885 balanced accuracy in this
simulator, so the 90% target sits at the noise limit. Labels in the simulator come from the same threshold rule as the
seed set, so real agronomist labels will be harder.


## Benchmark
`scripts/benchmark.py`: 120-day season, 576 patches, 6 random farms. Every strategy sees **identical weather** (environment and
sensor noise use separate random streams), so differences come only from what each strategy does. Yield index 1.00 = no crop loss.

| Strategy | Yield | Water (mm/patch) | Sprays/patch |
|---|---|---|---|
| Do nothing | 87.9% ± 6.4 | 0 | 0.0 |
| Fixed schedule | 100.0% ± 0.0 | 612 | 8.0 |
| AI agent, rules only | 99.7% ± 0.1 | 122 | 0.56 |
| AI agent, 1 flight/day | 96.0% ± 4.2 | 66 | 0.69 |
| AI agent, 3 flights/day | 99.7% ± 0.1 | 126 | 0.62 |

- The AI crew keeps **99.7% of the fixed schedule's yield with ~79% less water and ~92% fewer sprays**; doing nothing loses ~12% of the crop.
- **Capacity matters:** one flight/day per drone cannot keep up with a dry spell (96% yield); with battery swaps (3 flights/day) a 3-drone fleet reaches the plateau (backlog -> ~0).
  Beyond that, yield no longer improves with more drones.
- **The ML model adds almost nothing here** (+0.04 yield points over plain threshold rules; better on 6/6 farms). In this simulator stress *is* a clean threshold on the same sensors.
  Its value would appear where stress is not a simple threshold; that cannot be shown without real data. The rules always act regardless of the model (fail-safe).
- **Sensor faults (4% of sensors dead/stuck/biased/spiking):** without the quality layer the fleet wastes ~29% extra water (184 vs 131 mm) for no yield benefit.
  Detector recall (`scripts/eval_quality.py`): dead 1.00, stuck 0.98, spiking 0.79, **biased 0.53** (a persistent offset is hard to tell from irrigation/soil variation
  without a second sensor or scouting measurements), false alarms 0.02%.
- Action thresholds are the farmer's water-vs-yield knob (`scripts/sweep_thresholds.py`): acting at 0.30 moisture / 0.45 pests (just before damage starts) reaches 0.997 yield; more eager settings buy ~0.1% yield for 25% more water.

### Limits of these numbers
Simulator physics are simplified (single crop, one soil layer, no wind/disease/hail, drone treatment instantly effective). Weather is a synthetic seasonal generator unless
`weather.source` is `open-meteo`. Sensor noise (+-0.02 at decision thresholds) caps any detector at ~0.885 balanced accuracy. **None of this is field evidence.**

## Reliability & engineering
- Policy is schema-validated on every load; the autopilot runs the test suite first, validates policy, runs the cycle, **verifies state integrity before committing**, and opens an incident issue on failure.
- Versioned state migration (`state/version.json`): a simulator upgrade archives old state instead of silently mixing models.
- CI: ruff lint + format, tests on Python 3.11/3.12/3.13 with an 85% coverage gate (currently 95%), bandit (medium+ fails), pip-audit, Docker build + run as non-root.
- Planner: incremental insertion cost with cached leg costs; 300+ candidate patches planned in ~0.5 s.


## Airspace safety (drones must never clash)
Altitude layers alone only protect drones in *level* flight; the dangerous moments are climbs, descents and overflights of a pad.
`traffic.py` therefore enforces four layers, and the safety gate (`safety.validate`) re-verifies them on every plan:
1. **One launch pad per drone**, `pad_spacing_m` (40 m) apart (policy-validated to be >= 1.5x the clearance).
2. **Distinct cruise altitudes** for drones flying together, >= `min_separation_m` (15 m) apart.
3. **A time-resolved 3D replay**: each mission's full trajectory (climb, legs, hover, detours, return, hold, descent, with real speeds) is stepped
   second by second for all drones flying together. A *conflict* is both closer than `horizontal_clearance_m` (20 m) sideways **and** closer than
   15 m vertically at the same instant. Any conflict rejects the plan.
4. **A scheduler** staggers launches (highest altitude first) and adds pre-landing holds until the replay shows zero conflicts. If no concurrent
   schedule exists it **flies one drone at a time**, which cannot overlap in time and is safe by construction.

Hardware modes plan one sortie and allow launch stagger only (no holds: return-to-launch timing cannot be delayed); the ground station sleeps each drone's
`t0` on the ground before connecting, and the preflight refuses any plan that needs a hold. Verified by tests on adversarial crossings, random fleets of 2/3/5
drones (always conflict-free, margin >= 1.0x) and the serialized fallback. Real planner output without the scheduler *does* contain conflicts, which is why
the check exists. **Not modelled:** wind, GPS error beyond the clearance margin, drones outside this fleet, birds, communication loss of the whole fleet.

## Rechargeable fleet
`fleet.py` gives every drone a persistent battery (`state/fleet.json`): state of charge, equivalent full cycles, health.
- **Charging between flights** (`fleet.charging.mode: charge`): the pack regains `rate_w` x `turnaround_min` of energy; **or** `swap`: instant full spare pack.
  The planner budgets each sortie from this (a slow charger really shrinks later flights); `simulate()` re-walks the final plan flight by flight and the
  agent blocks any plan that would drop a battery below the 25% reserve.
- **Overnight**: everything charges to full (the report says how many hours that needs at the charger's power).
- **Wear**: capacity fades 0.04% per equivalent full cycle (floor 80%), so planning uses each drone's *current* capacity.

Effect of the charging setup (`scripts/sweep_charging.py`; 120-day season, 3 farms, 3 drones x 3 flights/day):

| Charging | Yield | Water (mm) | Pack health after 120 days |
|---|---|---|---|
| spare packs (swap) | 0.997 | 128 | 94.7% |
| charger 200 W | 0.997 | 128 | 94.7% |
| charger 90 W (default) | 0.991 | 118 | 94.8% |
| charger 40 W | 0.951 | 79 | 96.8% |
| charger 20 W | 0.938 | 65 | 97.4% |

Rule of thumb from the simulation: ~90 W per drone is the practical minimum for a 100 Wh pack; below that, later flights cannot clear a dry spell.
The wear model is a simplification (no temperature, depth-of-discharge or calendar aging).

## Satellite imagery (real data)
`imagery.py` ingests **real Sentinel-2 L2A** (10 m, ~5-day revisit, free, no key) through the Earth Search STAC API and windowed reads of Cloud-Optimized
GeoTIFFs: only the field's few hundred KB, never the tile. Clouds, shadows, cirrus, snow and no-data are masked **before** averaging to the patch grid, so a
scene that is 55% cloudy over the tile but clear over the field is still used. Radiometry honours each item's own `earthsearch:boa_offset_applied` flag
(blindly applying the -0.1 offset gave impossible NDVI > 1 on real tiles; found by testing on live data). Outputs: NDVI (vigor) and NDMI (leaf water),
robust-z low-vigor zones, change vs the previous scene, ranked "scout these patches first", and a time series; all shown on the dashboard as **real, not simulated**.
Two uses beyond display: the digital twin is seeded with the real field's spatial patchiness (`init_field`), and `crosscheck()` compares ground-sensor NDVI against
the satellite as an independent reference (the bias-fault gap the sensor-quality layer cannot close on its own).
Optional extra: `pip install "agridrone[imagery]"` (rasterio + numpy). Every failure degrades to "no imagery today"; the autopilot never stops for it.
Verified on live data (`AGRIDRONE_LIVE=1 pytest tests/test_imagery.py -k live`): 9 real scenes (Aug-Sep 2026) for the default Iowa field, mean NDVI 0.79-0.84,
a believable late-season decline, 100% of the field clear in every kept scene.
**Limits:** satellite NDVI is a ~10 m average and may be days old; there is no ground truth for real pixels, so the stress *classifier* is not trained or scored on real
imagery (the zone/scouting analysis is unsupervised); cloud masking depends on Sentinel-2's SCL layer; the default field location is an Iowa row-crop area for demonstration; set your own.
