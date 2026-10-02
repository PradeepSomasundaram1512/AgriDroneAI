"""The autonomous operating loop: observe -> classify -> plan -> safety-gate -> act -> learn -> audit.
One call to run_cycle() = one day of operation. Designed to be invoked by a scheduler (GitHub Actions)."""

import os
import random
import time
import traceback

from . import advisor, fleet, gps, imagery, planner, quality, safety, store, traffic, weather
from . import wind as W
from . import model as M
from .adapters import SimAdapter
from .config import load_policy
from .store import append_jsonl, load_json, save_json

TRAIN_CAP = 250  # per class: rare stressed examples must not be evicted by healthy ones
EVAL_CAP = 150


def _load_buf(name, d):
    b = load_json(name, {"pos": [], "neg": []}, d)
    return b if isinstance(b, dict) else {"pos": [], "neg": []}


def _save_buf(name, b, d):
    save_json(name, b, d)


def _add(buf, rows, cap):
    for r in rows:
        buf["pos" if r[3] else "neg"].append([round(r[0], 4), round(r[1], 4), round(r[2], 4), r[3]])
    for k in ("pos", "neg"):
        buf[k] = buf[k][-cap:]


def _fates(planned, flown, gps_info):
    """What really happened to each planned flight (for the dashboard replay): completed, recalled (a drone above lost GPS),
    gps_lost (landed in place, needs recovery) or cancelled (its drone was already down in a field)."""
    got = {(m.drone, m.sortie): m for m in flown}
    lost = {(e["drone"], e["sortie"]) for e in gps_info["events"]}
    out = {}
    for m in planned:
        k = (m.drone, m.sortie)
        if k in lost:
            status = "gps_lost"
        elif k not in got:
            status = "cancelled"
        elif len(got[k].targets) < len(m.targets):
            status = "recalled"
        else:
            status = "completed"
        out[k] = {"status": status, "flown_targets": len(got[k].targets) if k in got else 0}
    return out


def snapshot(farm):
    """Compact field state for the dashboard: [[ndvi%, moisture%, pest%]] ordered x-major (index = x*size + y)."""
    return [
        [round(farm.cells[(x, y)].ndvi * 100), round(farm.cells[(x, y)].moisture * 100), round(farm.cells[(x, y)].pest * 100)]
        for x in range(farm.size)
        for y in range(farm.size)
    ]


def _labelled_sample(farm, clean_obs, n, rng):
    """Scouted samples: cleaned sensor readings + ground-truth labels from a field visit."""
    by = {tuple(o["cell"]): o for o in clean_obs}
    cells = rng.sample(list(farm.cells), min(n, len(farm.cells)))
    obs = [by[c] for c in cells]
    med = M.median([o["ndvi"] for o in clean_obs])  # NDVI anomaly vs the field, not raw NDVI (growth trend)
    return [(o["ndvi"] - med, o["moisture"], o["pest"], M.true_label(farm.cells[c])) for o, c in zip(obs, cells)]


def run_cycle(policy=None, state_dir=None, now=None):
    policy = policy or load_policy()
    thr = dict(policy["thresholds"])
    thr.update(load_json("threshold_overrides.json", {}, state_dir))
    t0 = time.time()

    def audit(kind, **kw):
        append_jsonl("audit.jsonl", {"ts": now or time.time(), "kind": kind, **kw}, state_dir)

    rec = {"ok": False, "ts": now or time.time()}
    try:
        if policy.get("kill_switch"):
            audit("kill_switch", detail="cycle skipped: kill switch engaged")
            rec.update(ok=True, skipped=True)
            append_jsonl("metrics.jsonl", rec, state_dir)
            return rec

        migrated = store.migrate(state_dir)
        if migrated:
            audit("state_migrated", archived_to=str(migrated.name), reason="simulation model upgraded; old state archived, starting clean")
        farm = store.load_farm(
            policy["field"]["seed"],
            policy["field"]["size"],
            state_dir,
            policy["field"].get("sensor_fault_rate", 0.0),
            policy.get("weather", {}).get("start_doy", 120),
        )
        # real satellite data: refresh when stale (never raises), then seed the twin with the real field's spatial pattern
        if not os.environ.get("AGRIDRONE_OFFLINE"):
            st = imagery.refresh(policy, state_dir)
            if st["status"] in ("updated", "unavailable", "error"):
                audit("imagery", **st)
        icfg = policy.get("imagery", {})
        if icfg.get("init_field") and farm.init_scene is None and farm.day <= 7:
            scenes = imagery.load_scenes(state_dir)
            if scenes:
                imagery.init_field(farm, scenes[-1])
                farm.init_scene = scenes[-1].id
                audit("twin_initialized_from_satellite", scene=scenes[-1].id, date=scenes[-1].date, valid_frac=scenes[-1].valid_frac)
        rng = random.Random(farm.seed * 1000 + farm.day)
        wx, forecast, wx_src = weather.get_weather(policy, farm.day + 1, state_dir)
        farm.step(wx)
        save_json(
            "weather.json", {"day": farm.day, "source": wx_src, "today": wx.__dict__, "forecast": [w.__dict__ for w in forecast]}, state_dir
        )

        # --- model lifecycle: honest evaluation + continuous training with canary promotion/rollback ---
        mdl = M.StressModel.from_dict(load_json("model.json", M.StressModel().to_dict(), state_dir))
        ref = load_json("reference.json", None, state_dir)
        # data quality: detect + repair bad sensor readings BEFORE anything (model, planner) sees them
        obs, dq = quality.clean(farm.observe_all(), state_dir, enabled=policy.get("quality_filter", True))
        if dq["flagged"] or dq["quarantined"]:
            audit("sensor_faults", flagged=dq["flagged"], quarantined=len(dq["quarantined"]), reasons=dq["reasons"])
        sample = _labelled_sample(farm, obs, 120, rng)
        train_buf = _load_buf("train_rows.json", state_dir)
        eval_buf = _load_buf("eval_rows.json", state_dir)
        # disjoint split: even rows may be trained on, odd rows are ONLY ever used to evaluate
        _add(train_buf, sample[0::2], TRAIN_CAP)
        _add(eval_buf, sample[1::2], EVAL_CAP)
        recent = load_json("eval_recent.json", {}, state_dir)  # {day: held-out rows}: the CURRENT regime
        recent[str(farm.day)] = [[round(r[0], 4), round(r[1], 4), round(r[2], 4), r[3]] for r in sample[1::2]]
        recent = {k: v for k, v in recent.items() if farm.day - int(k) < 5}
        save_json("eval_recent.json", recent, state_dir)
        recent_rows = [tuple(r) for v in recent.values() for r in v]
        long_rows = eval_buf["pos"] + eval_buf["neg"]

        # judge on the recent window when it contains both classes, else on the class-balanced long-term buffer;
        # a candidate must also not regress on the long-term buffer (guards against forgetting old regimes)
        def judge(m):
            sr, sl = m.score(recent_rows), m.score(long_rows)
            return (sr if sr["bal"] is not None else sl), sl

        inc, inc_long = judge(mdl) if mdl.tree else ({"bal": None, "recall": None}, {"bal": None})
        cur_ndvi = [r[0] for r in sample]  # anomalies: drift here means the field is becoming heterogeneous
        drift = M.psi(ref, cur_ndvi) if ref else 0.0
        last_train_day = load_json("last_train_day.json", 0, state_dir)
        weak = (
            mdl.tree is None
            or inc["bal"] is None
            or inc["bal"] < thr["min_accuracy"]
            or (inc["recall"] is not None and inc["recall"] < thr["min_recall"])
        )
        retrained = False
        if weak or drift > thr["psi_drift"] or farm.day - last_train_day >= 7:
            cand = M.StressModel(mdl.tree, mdl.version)
            # the seed set is ALWAYS included (never evicted): its features are independent of the label, which
            # stops the tree from latching onto drifting shortcut features such as NDVI trend
            if cand.train(M.seed_rows() + train_buf["pos"] + train_buf["neg"]):
                cs, cs_long = judge(cand)
                better = inc["bal"] is None or (
                    cs["bal"] is not None
                    and cs["bal"] >= inc["bal"] + 0.005
                    and (inc_long["bal"] is None or cs_long["bal"] is None or cs_long["bal"] >= inc_long["bal"] - 0.03)
                )
                if better:  # canary passed: promote
                    mdl, inc, retrained = cand, cs, True
                    save_json("reference.json", cur_ndvi, state_dir)
                    save_json("last_train_day.json", farm.day, state_dir)
                audit(
                    "retrain",
                    promoted=retrained,
                    cand_bal=cs["bal"] and round(cs["bal"], 3),
                    incumbent_bal=inc["bal"] and round(inc["bal"], 3) if not retrained else None,
                    drift=round(drift, 3),
                    version=mdl.version,
                    rolled_back=not retrained,
                )
            else:
                audit("retrain_skipped", reason="training data lacks one class")
        save_json("model.json", mdl.to_dict(), state_dir)
        _save_buf("train_rows.json", train_buf, state_dir)
        _save_buf("eval_rows.json", eval_buf, state_dir)
        acc = inc["bal"] if inc["bal"] is not None else mdl.accuracy(sample)  # balanced accuracy, not raw accuracy
        recall = inc["recall"]
        if acc < thr["min_accuracy"] or (recall is not None and recall < thr["min_recall"]):
            audit(
                "model_below_target",
                balanced_accuracy=round(acc, 3),
                recall=recall and round(recall, 3),
                detail="needs more/better labelled data; planner still protects crops via threshold rules",
            )

        # --- plan, gate, act ---
        # wind: flights are scheduled in the morning lull; above the limits nothing flies, above the spray limit only irrigation does
        lim = W.limits(policy)
        wind = (
            W.Wind(wx.wind_ms * lim["flight_window_factor"], wx.gust_ms * lim["flight_window_factor"], wx.wind_from_deg)
            if lim["enabled"]
            else W.CALM
        )
        flight_ok, spray_ok, wind_why = W.go_no_go(wind, policy)
        spray_deferred = []
        if flight_ok:
            targets = planner.find_targets(
                farm, mdl if policy.get("model_enabled", True) else M.StressModel(), thr, forecast, obs, spray_ok, spray_deferred
            )
        else:
            targets = []
            audit("grounded_by_wind", reason=wind_why, wind=wind.as_tuple())
        if flight_ok and not spray_ok and spray_deferred:
            audit("spray_deferred_by_wind", patches=len(spray_deferred), reason=wind_why)
        farm.flight_wind = wind.speed  # spray drift loss in the simulator
        # hardware modes plan ONE sortie: a battery swap between sorties needs a human at the aircraft
        sim_mode = policy["autonomy_level"] == "simulation"
        batteries = fleet.Fleet.load(policy, state_dir)
        batteries.today = farm.day  # drones that landed in a field earlier are grounded until `grounded_until`
        overnight_wh, overnight_h = batteries.overnight()  # every pack is back to full at dawn
        missions = planner.plan(
            targets,
            policy,
            sorties=policy["fleet"].get("sorties_per_day", 1) if sim_mode else 1,
            allow_hold=sim_mode,
            fleet=batteries,
            wind=wind,
        )
        missions, dropped = safety.repair(missions, policy)
        traffic_reps = [  # trimming a route changes its timing: prove the final schedule is conflict-free
            traffic.schedule([m for m in missions if m.sortie == k], policy, allow_hold=sim_mode)
            for k in sorted({m.sortie for m in missions})
        ]
        energy = batteries.simulate(missions)  # flight-by-flight battery check incl. charging between sorties
        violations = safety.validate(missions, policy) + energy["violations"]
        level = policy["autonomy_level"]
        executed = 0
        gps_info = {"events": [], "patches_deferred": 0, "grounded": []}
        flown = missions
        fates = {}
        before = snapshot(farm)  # for the dashboard replay: the field as the drones found it
        if violations:
            audit("safety_block", violations=violations)
        elif level == "simulation":
            # GNSS outages: a drone that loses its fix lands in place, drones below it are ordered home, the rest is deferred
            flown, gps_info = missions, {"events": [], "patches_deferred": 0, "grounded": []}
            events = gps.sample_losses(missions, policy, farm.seed, farm.day)
            if events:
                flown, gps_info = gps.apply_losses(missions, events, policy)
                for e in gps_info["events"]:
                    batteries.ground(e["drone"], farm.day + 1 + int(gps.cfg(policy)["recovery_days"]))
                    audit("gps_loss", **e, patches_deferred=gps_info["patches_deferred"])
                energy = batteries.simulate(flown)  # truncated flights use less energy: charge what was really flown
            executed = SimAdapter(farm).execute(flown)
            fates = _fates(missions, flown, gps_info)
        else:  # supervised / autonomous: cloud only QUEUES; the ground station at the farm flies
            auto = level == "autonomous" and policy["hardware"].get("enabled", False)
            store.append_jsonl(
                "queue.jsonl",
                {
                    "id": f"day{farm.day}",
                    "ts": now or time.time(),
                    "approval": "auto" if auto else "human",
                    "missions": [
                        {
                            "drone": m.drone,
                            "alt": m.altitude_m,
                            "energy_wh": m.energy_wh,
                            "sortie": m.sortie,
                            "t0": m.t0,
                            "hold": m.hold,
                            "pad": m.pad if m.pad >= 0 else m.drone,
                            "wind": m.wind,
                            "targets": m.targets,
                        }
                        for m in missions
                    ],
                },
                state_dir,
            )
            audit("queued", id=f"day{farm.day}", missions=len(missions), approval="auto" if auto else "human")

        if not violations:
            batteries.commit(energy)  # flown (or queued) day: end-of-day charge state and wear
        batteries.save(state_dir)
        base_w, base_c = farm.blanket_usage()
        summary = {
            "day": farm.day,
            "targets": len(targets),
            "executed": executed,
            "sorties": len({m.sortie for m in missions}),
            "gps_losses": len(gps_info["events"]),
            "gps_patches_deferred": gps_info["patches_deferred"],
            "drones_grounded": sum(1 for i in range(batteries.n) if batteries.grounded(i)),
            "wind_ms": round(wind.speed, 1),
            "gust_ms": round(wind.gust, 1),
            "wind_from_deg": round(wind.from_deg),
            "grounded_by_wind": not flight_ok,
            "spray_deferred": len(spray_deferred),
            "flight_hours": round(
                sum(traffic.duration(m, policy) for m in (flown if level == "simulation" and not violations else missions)) / 3600, 3
            ),
            "fleet_energy_wh": energy["used_wh"],
            "charged_between_flights_wh": energy["charged_wh"],
            "overnight_charge_wh": round(overnight_wh),
            "overnight_charge_hours": round(overnight_h, 1),
            **batteries.summary(),
            "traffic_min_ratio": min((r["min_ratio"] for r in traffic_reps if r.get("min_ratio") is not None), default=None),
            "traffic_mode": "serialized" if any(r["mode"] == "serialized" for r in traffic_reps) else "concurrent",
            "launch_stagger_s": max((r["max_delay_s"] for r in traffic_reps), default=0),
            "rain_mm": wx.rain_mm,
            "weather_source": wx_src,
            "sensors_flagged": dq["flagged"],
            "sensors_quarantined": len(dq["quarantined"]),
            "dropped_by_safety": dropped,
            "accuracy": round(acc, 3),
            "recall": None if recall is None else round(recall, 3),
            "psi": round(drift, 3),
            "model_version": mdl.version,
            "retrained": retrained,
            "water_saved_pct": round(100 * (1 - farm.water_used / base_w), 1) if base_w else 0.0,
            "chem_saved_pct": round(100 * (1 - farm.chem_used / base_c), 1) if base_c else 0.0,
            "yield_index": round(farm.mean_yield(), 4),
            "plan_seconds": round(time.time() - t0, 3),
        }
        adv = advisor.advise(summary, thr)
        if adv["thresholds"]:
            ov = load_json("threshold_overrides.json", {}, state_dir)
            ov.update(adv["thresholds"])
            save_json("threshold_overrides.json", ov, state_dir)
        audit("cycle", **summary, advisor_note=adv["note"])
        store.save_farm(farm, state_dir)
        # dashboard feed: today's mission (animated replay) + a rolling 30-day field history (time-lapse)
        save_json(
            "last_mission.json",
            {
                "day": farm.day,
                "size": farm.size,
                "before": before,
                "no_fly": policy["no_fly_cells"],
                "wind": wind.as_tuple(),
                "gps_events": gps_info["events"],
                "missions": [
                    {
                        "drone": m.drone,
                        "alt": m.altitude_m,
                        "sortie": m.sortie,
                        "energy_wh": round(m.energy_wh, 1),
                        "t0": m.t0,
                        "hold": m.hold,
                        "pad": m.pad if m.pad >= 0 else m.drone,
                        "soc": next((r for r in energy["drones"].get(m.drone, []) if r["sortie"] == m.sortie), None),
                        "fate": fates.get((m.drone, m.sortie)),
                        "targets": [[c[0], c[1], a] for c, a in m.targets],
                    }
                    for m in missions
                ],
            },
            state_dir,
        )
        hist = load_json("field_history.json", [], state_dir)
        hist = [h for h in hist if h["day"] != farm.day][-29:] + [{"day": farm.day, "cells": snapshot(farm)}]
        save_json("field_history.json", hist, state_dir)
        rec.update(ok=True, **summary)
    except Exception as e:  # self-healing: record the incident, keep the loop alive for the next run
        audit("incident", error=repr(e), trace=traceback.format_exc()[-800:])
        rec.update(ok=False, error=repr(e))
    append_jsonl("metrics.jsonl", rec, state_dir)
    return rec
