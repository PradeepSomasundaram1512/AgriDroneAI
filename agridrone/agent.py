"""The autonomous operating loop: observe -> classify -> plan -> safety-gate -> act -> learn -> audit.
One call to run_cycle() = one day of operation. Designed to be invoked by a scheduler (GitHub Actions)."""

import os
import random
import time
import traceback

from . import advisor, calibrate, fleet, gps, imagery, planner, quality, safety, store, traffic, weather
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


class Cycle:
    """One day of operation as explicit, separately readable stages. Each stage reads and writes named attributes; run() sequences them and
    turns ANY exception into a logged incident so the next scheduled day still happens. (This replaced a 295-line function; the order of
    every
    operation that draws from a random stream is unchanged, and tests/test_golden_cycle.py proves the behaviour is identical.)"""

    def __init__(self, policy, state_dir, now):
        self.p, self.sd, self.now, self.t0 = policy, state_dir, now, time.time()
        self.thr = dict(policy["thresholds"])
        self.thr.update(load_json("threshold_overrides.json", {}, state_dir))
        if policy.get("calibration", {}).get("enabled"):  # thresholds the system learned from the crop's own response (calibrate.py)
            self.thr.update(
                {k: v for k, v in load_json("calibrated.json", {}, state_dir).items() if k in ("moisture_irrigate", "pest_spray")}
            )
        self.rec = {"ok": False, "ts": now or time.time()}
        self.level = policy["autonomy_level"]
        self.sim_mode = self.level == "simulation"  # hardware modes plan ONE sortie: a battery swap needs a human at the aircraft
        self.executed, self.fates, self.flown = 0, {}, []
        self.gps_info = {"events": [], "patches_deferred": 0, "grounded": []}

    def audit(self, kind, **kw):
        append_jsonl("audit.jsonl", {"ts": self.now or time.time(), "kind": kind, **kw}, self.sd)

    # ------------------------------------------------------------------ stage 1: state, farm, satellite
    def load(self):
        p, sd = self.p, self.sd
        migrated = store.migrate(sd)
        if migrated:
            self.audit(
                "state_migrated", archived_to=str(migrated.name), reason="simulation model upgraded; old state archived, starting clean"
            )
        self.farm = store.load_farm(
            p["field"]["seed"],
            p["field"]["size"],
            sd,
            p["field"].get("sensor_fault_rate", 0.0),
            p.get("weather", {}).get("start_doy", 120),
            p["field"].get("physics"),
        )
        # real satellite data: refresh when stale (never raises), then seed the twin with the real field's spatial pattern
        if not os.environ.get("AGRIDRONE_OFFLINE"):
            st = imagery.refresh(p, sd)
            if st["status"] in ("updated", "unavailable", "error"):
                self.audit("imagery", **st)
        if p.get("imagery", {}).get("init_field") and self.farm.init_scene is None and self.farm.day <= 7:
            scenes = imagery.load_scenes(sd)
            if scenes:
                imagery.init_field(self.farm, scenes[-1])
                self.farm.init_scene = scenes[-1].id
                self.audit("twin_initialized_from_satellite", scene=scenes[-1].id, date=scenes[-1].date, valid_frac=scenes[-1].valid_frac)

    # ------------------------------------------------------------------ stage 2: the world moves one day
    def advance(self):
        f = self.farm
        self.rng = random.Random(f.seed * 1000 + f.day)
        self.wx, self.forecast, self.wx_src = weather.get_weather(self.p, f.day + 1, self.sd)
        f.step(self.wx)
        save_json(
            "weather.json",
            {"day": f.day, "source": self.wx_src, "today": self.wx.__dict__, "forecast": [w.__dict__ for w in self.forecast]},
            self.sd,
        )

    # ------------------------------------------------------------------ stage 3: sensors -> clean data -> scouted sample -> calibration
    def perceive(self):
        p, sd = self.p, self.sd
        # data quality: detect + repair bad sensor readings BEFORE anything (model, planner) sees them
        self.obs, self.dq = quality.clean(self.farm.observe_all(), sd, enabled=p.get("quality_filter", True))
        if self.dq["flagged"] or self.dq["quarantined"]:
            self.audit("sensor_faults", flagged=self.dq["flagged"], quarantined=len(self.dq["quarantined"]), reasons=self.dq["reasons"])
        self.sample = _labelled_sample(self.farm, self.obs, 120, self.rng)
        learned, changed = calibrate.update(p, self.sample, sd)  # learn where THIS crop starts to suffer
        self.thr.update(learned)
        if changed:
            self.audit("calibrated", **learned)

    # ------------------------------------------------------------------ stage 4: the stress model (honest evaluation, canary, rollback)
    def learn(self):
        sd, thr, farm, sample = self.sd, self.thr, self.farm, self.sample
        mdl = M.StressModel.from_dict(load_json("model.json", M.StressModel().to_dict(), sd))
        ref = load_json("reference.json", None, sd)
        train_buf, eval_buf = _load_buf("train_rows.json", sd), _load_buf("eval_rows.json", sd)
        # disjoint split: even rows may be trained on, odd rows are ONLY ever used to evaluate
        _add(train_buf, sample[0::2], TRAIN_CAP)
        _add(eval_buf, sample[1::2], EVAL_CAP)
        recent = load_json("eval_recent.json", {}, sd)  # {day: held-out rows}: the CURRENT regime
        recent[str(farm.day)] = [[round(r[0], 4), round(r[1], 4), round(r[2], 4), r[3]] for r in sample[1::2]]
        recent = {k: v for k, v in recent.items() if farm.day - int(k) < 5}
        save_json("eval_recent.json", recent, sd)
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
        last_train_day = load_json("last_train_day.json", 0, sd)
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
                    save_json("reference.json", cur_ndvi, sd)
                    save_json("last_train_day.json", farm.day, sd)
                self.audit(
                    "retrain",
                    promoted=retrained,
                    cand_bal=cs["bal"] and round(cs["bal"], 3),
                    incumbent_bal=inc["bal"] and round(inc["bal"], 3) if not retrained else None,
                    drift=round(drift, 3),
                    version=mdl.version,
                    rolled_back=not retrained,
                )
            else:
                self.audit("retrain_skipped", reason="training data lacks one class")
        save_json("model.json", mdl.to_dict(), sd)
        _save_buf("train_rows.json", train_buf, sd)
        _save_buf("eval_rows.json", eval_buf, sd)
        self.mdl, self.drift, self.retrained = mdl, drift, retrained
        self.acc = inc["bal"] if inc["bal"] is not None else mdl.accuracy(sample)  # balanced accuracy, not raw accuracy
        self.recall = inc["recall"]
        if self.acc < thr["min_accuracy"] or (self.recall is not None and self.recall < thr["min_recall"]):
            self.audit(
                "model_below_target",
                balanced_accuracy=round(self.acc, 3),
                recall=self.recall and round(self.recall, 3),
                detail="needs more/better labelled data; planner still protects crops via threshold rules",
            )

    # ------------------------------------------------------------------ stage 5: decide (wind go/no-go, targets, plan, safety gate)
    def decide(self):
        p, farm, wx = self.p, self.farm, self.wx
        # wind: flights are scheduled in the morning lull; above the limits nothing flies, above the spray limit only irrigation does
        lim = W.limits(p)
        self.wind = (
            W.Wind(wx.wind_ms * lim["flight_window_factor"], wx.gust_ms * lim["flight_window_factor"], wx.wind_from_deg)
            if lim["enabled"]
            else W.CALM
        )
        self.flight_ok, spray_ok, wind_why = W.go_no_go(self.wind, p)
        self.spray_deferred = []
        if self.flight_ok:
            model = self.mdl if p.get("model_enabled", True) else M.StressModel()
            self.targets = planner.find_targets(farm, model, self.thr, self.forecast, self.obs, spray_ok, self.spray_deferred)
        else:
            self.targets = []
            self.audit("grounded_by_wind", reason=wind_why, wind=self.wind.as_tuple())
        if self.flight_ok and not spray_ok and self.spray_deferred:
            self.audit("spray_deferred_by_wind", patches=len(self.spray_deferred), reason=wind_why)
        farm.flight_wind = self.wind.speed  # spray drift loss in the simulator
        self.batteries = fleet.Fleet.load(p, self.sd)
        self.batteries.today = farm.day  # drones that landed in a field earlier are grounded until `grounded_until`
        self.overnight_wh, self.overnight_h = self.batteries.overnight()  # every pack is back to full at dawn
        missions = planner.plan(
            self.targets,
            p,
            sorties=p["fleet"].get("sorties_per_day", 1) if self.sim_mode else 1,
            allow_hold=self.sim_mode,
            fleet=self.batteries,
            wind=self.wind,
        )
        self.missions, self.dropped = safety.repair(missions, p)
        self.traffic_reps = [  # trimming a route changes its timing: prove the final schedule is conflict-free
            traffic.schedule([m for m in self.missions if m.sortie == k], p, allow_hold=self.sim_mode)
            for k in sorted({m.sortie for m in self.missions})
        ]
        self.energy = self.batteries.simulate(self.missions)  # flight-by-flight battery check incl. charging between sorties
        self.violations = safety.validate(self.missions, p) + self.energy["violations"]
        self.flown = self.missions

    # ------------------------------------------------------------------ stage 6: act (simulate the day, or queue it for the ground station)
    def act(self):
        p, farm, missions = self.p, self.farm, self.missions
        self.before = snapshot(farm)  # for the dashboard replay: the field as the drones found it
        if self.violations:
            self.audit("safety_block", violations=self.violations)
        elif self.sim_mode:
            # GNSS outages: a drone that loses its fix lands in place, drones below it are ordered home, the rest is deferred
            events = gps.sample_losses(missions, p, farm.seed, farm.day)
            if events:
                self.flown, self.gps_info = gps.apply_losses(missions, events, p)
                for e in self.gps_info["events"]:
                    self.batteries.ground(e["drone"], farm.day + 1 + int(gps.cfg(p)["recovery_days"]))
                    self.audit("gps_loss", **e, patches_deferred=self.gps_info["patches_deferred"])
                self.energy = self.batteries.simulate(self.flown)  # truncated flights use less energy: charge what was really flown
            self.executed = SimAdapter(farm).execute(self.flown)
            self.fates = _fates(missions, self.flown, self.gps_info)
        else:  # supervised / autonomous: cloud only QUEUES; the ground station at the farm flies
            auto = self.level == "autonomous" and p["hardware"].get("enabled", False)
            store.append_jsonl(
                "queue.jsonl",
                {
                    "id": f"day{farm.day}",
                    "ts": self.now or time.time(),
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
                self.sd,
            )
            self.audit("queued", id=f"day{farm.day}", missions=len(missions), approval="auto" if auto else "human")
        if not self.violations:
            self.batteries.commit(self.energy)  # flown (or queued) day: end-of-day charge state and wear
        self.batteries.save(self.sd)

    # ------------------------------------------------------------------ stage 7: record (summary, audit, dashboard feeds)
    def summary(self):
        p, farm, b, wind = self.p, self.farm, self.batteries, self.wind
        base_w, base_c = farm.blanket_usage()
        flown = self.flown if self.sim_mode and not self.violations else self.missions
        return {
            "day": farm.day,
            "targets": len(self.targets),
            "executed": self.executed,
            "sorties": len({m.sortie for m in self.missions}),
            "gps_losses": len(self.gps_info["events"]),
            "gps_patches_deferred": self.gps_info["patches_deferred"],
            "drones_grounded": sum(1 for i in range(b.n) if b.grounded(i)),
            "moisture_trigger": round(self.thr["moisture_irrigate"], 3),
            "pest_trigger": round(self.thr["pest_spray"], 3),
            "wind_ms": round(wind.speed, 1),
            "gust_ms": round(wind.gust, 1),
            "wind_from_deg": round(wind.from_deg),
            "grounded_by_wind": not self.flight_ok,
            "spray_deferred": len(self.spray_deferred),
            "flight_hours": round(sum(traffic.duration(m, p) for m in flown) / 3600, 3),
            "fleet_energy_wh": self.energy["used_wh"],
            "charged_between_flights_wh": self.energy["charged_wh"],
            "overnight_charge_wh": round(self.overnight_wh),
            "overnight_charge_hours": round(self.overnight_h, 1),
            **b.summary(),
            "traffic_min_ratio": min((r["min_ratio"] for r in self.traffic_reps if r.get("min_ratio") is not None), default=None),
            "traffic_mode": "serialized" if any(r["mode"] == "serialized" for r in self.traffic_reps) else "concurrent",
            "launch_stagger_s": max((r["max_delay_s"] for r in self.traffic_reps), default=0),
            "rain_mm": self.wx.rain_mm,
            "weather_source": self.wx_src,
            "sensors_flagged": self.dq["flagged"],
            "sensors_quarantined": len(self.dq["quarantined"]),
            "dropped_by_safety": self.dropped,
            "accuracy": round(self.acc, 3),
            "recall": None if self.recall is None else round(self.recall, 3),
            "psi": round(self.drift, 3),
            "model_version": self.mdl.version,
            "retrained": self.retrained,
            "water_saved_pct": round(100 * (1 - farm.water_used / base_w), 1) if base_w else 0.0,
            "chem_saved_pct": round(100 * (1 - farm.chem_used / base_c), 1) if base_c else 0.0,
            "yield_index": round(farm.mean_yield(), 4),
            "plan_seconds": round(time.time() - self.t0, 3),
        }

    def record(self):
        farm, sd = self.farm, self.sd
        summary = self.summary()
        adv = advisor.advise(summary, self.thr)
        if adv["thresholds"]:
            ov = load_json("threshold_overrides.json", {}, sd)
            ov.update(adv["thresholds"])
            save_json("threshold_overrides.json", ov, sd)
        self.audit("cycle", **summary, advisor_note=adv["note"])
        store.save_farm(farm, sd)
        # dashboard feed: today's mission (animated replay) + a rolling 30-day field history (time-lapse)
        save_json(
            "last_mission.json",
            {
                "day": farm.day,
                "size": farm.size,
                "before": self.before,
                "no_fly": self.p["no_fly_cells"],
                "wind": self.wind.as_tuple(),
                "gps_events": self.gps_info["events"],
                "missions": [
                    {
                        "drone": m.drone,
                        "alt": m.altitude_m,
                        "sortie": m.sortie,
                        "energy_wh": round(m.energy_wh, 1),
                        "t0": m.t0,
                        "hold": m.hold,
                        "pad": m.pad if m.pad >= 0 else m.drone,
                        "soc": next((r for r in self.energy["drones"].get(m.drone, []) if r["sortie"] == m.sortie), None),
                        "fate": self.fates.get((m.drone, m.sortie)),
                        "targets": [[c[0], c[1], a] for c, a in m.targets],
                    }
                    for m in self.missions
                ],
            },
            sd,
        )
        hist = load_json("field_history.json", [], sd)
        hist = [h for h in hist if h["day"] != farm.day][-29:] + [{"day": farm.day, "cells": snapshot(farm)}]
        save_json("field_history.json", hist, sd)
        self.rec.update(ok=True, **summary)

    # ------------------------------------------------------------------ the day
    def run(self):
        try:
            if self.p.get("kill_switch"):
                self.audit("kill_switch", detail="cycle skipped: kill switch engaged")
                self.rec.update(ok=True, skipped=True)
                append_jsonl("metrics.jsonl", self.rec, self.sd)
                return self.rec
            for stage in (self.load, self.advance, self.perceive, self.learn, self.decide, self.act, self.record):
                stage()
        except Exception as e:  # self-healing: record the incident, keep the loop alive for the next run
            self.audit("incident", error=repr(e), trace=traceback.format_exc()[-800:])
            self.rec.update(ok=False, error=repr(e))
        append_jsonl("metrics.jsonl", self.rec, self.sd)
        for log_name in (
            "audit.jsonl",
            "metrics.jsonl",
            "flights.jsonl",
            "queue.jsonl",
        ):  # keep git-tracked logs bounded (lossless archive)
            store.rotate(log_name, self.sd)
        return self.rec


def run_cycle(policy=None, state_dir=None, now=None):
    """One day of operation. See Cycle for the stages."""
    return Cycle(policy or load_policy(), state_dir, now).run()
