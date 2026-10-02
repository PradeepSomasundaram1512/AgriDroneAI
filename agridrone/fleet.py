"""Rechargeable fleet: battery state of charge, charging between flights, overnight recharge, and wear.

Each drone carries a state of charge (Wh) that persists day to day (state/fleet.json). Between sorties it either
  * charge: recharges in place at `rate_w` for `turnaround_min` (a slow charger really does limit how much a later sortie
            can do), or
  * swap:   exchanges the pack for a charged spare (instant, full), the charging happens off-line.
Overnight the whole fleet charges to full. Every Wh flown ages the pack: capacity fades with equivalent full cycles
(modelled at 0.04% per cycle, floor 80%), so planning uses each drone's CURRENT capacity.

The planner budgets each sortie from this model, and `simulate()` re-checks the final plan flight by flight, so a plan that
would drain a battery below the safety reserve is rejected before it flies."""

from .store import load_json, save_json

DEFAULT_CHARGING = {"mode": "charge", "rate_w": 90.0, "turnaround_min": 45.0, "swap_min": 3.0}
FADE_PER_CYCLE = 0.0004
MIN_HEALTH = 0.80


def charging(policy):
    return {**DEFAULT_CHARGING, **policy["fleet"].get("charging", {})}


class Fleet:
    def __init__(self, policy, drones=None):
        f = policy["fleet"]
        self.n, self.nominal, self.res = f["drones"], float(f["battery_wh"]), f["min_reserve_pct"] / 100.0
        self.chg = charging(policy)
        self.today = 0  # set by the agent: grounded drones come back on their `grounded_until` day
        self.d = drones or [{"soc_wh": self.nominal, "cycles": 0.0, "health": 1.0} for _ in range(self.n)]
        while len(self.d) < self.n:  # fleet grew
            self.d.append({"soc_wh": self.nominal, "cycles": 0.0, "health": 1.0})

    # ---- persistence
    @classmethod
    def load(cls, policy, state_dir=None):
        data = load_json("fleet.json", None, state_dir)
        return cls(policy, data["drones"] if data else None)

    def save(self, state_dir=None):
        save_json("fleet.json", {"version": 1, "drones": [{k: round(v, 3) for k, v in x.items()} for x in self.d]}, state_dir)

    # ---- model
    def cap(self, i):
        return self.nominal * self.d[i]["health"]

    def reserve(self, i):
        return self.res * self.cap(i)

    def gap_h(self):
        c = self.chg
        return (c["swap_min"] if c["mode"] == "swap" else c["turnaround_min"]) / 60.0

    def overnight(self):
        """Charge every pack to full. Returns (energy_in_wh, hours_needed_at_the_charger_power)."""
        energy, hours = 0.0, 0.0
        for i, x in enumerate(self.d):
            need = self.cap(i) - x["soc_wh"]
            energy += max(0.0, need)
            hours = max(hours, max(0.0, need) / self.chg["rate_w"])
            x["soc_wh"] = self.cap(i)
        return energy, hours

    def refill(self, i, soc):
        """State of charge after one turnaround."""
        if self.chg["mode"] == "swap":
            return self.cap(i)
        return min(self.cap(i), soc + self.chg["rate_w"] * self.gap_h())

    def grounded(self, i):
        """True while the drone is sitting in the field after an emergency landing, waiting to be recovered."""
        return self.today < self.d[i].get("grounded_until", 0)

    def ground(self, i, until_day):
        self.d[i]["grounded_until"] = max(self.d[i].get("grounded_until", 0), until_day)
        self.d[i]["incidents"] = self.d[i].get("incidents", 0) + 1

    def budget(self, i, s):
        """Usable energy (Wh above the safety reserve) drone i can spend on sortie s. For s>0 it conservatively assumes
        the previous sortie ended exactly at the reserve, so the plan stays feasible whatever the earlier flights used."""
        if self.grounded(i):
            return 0.0  # it is in a field somewhere: nothing to plan for it today
        if s == 0:
            return max(0.0, self.d[i]["soc_wh"] - self.reserve(i))
        return max(0.0, self.refill(i, self.reserve(i)) - self.reserve(i))

    def simulate(self, missions):
        """Walk every drone's sorties in order.
        -> {'drones': {i: [{sortie, soc_start, soc_end, charged}]}, 'violations', 'used_wh', 'charged_wh'}."""
        out, viol, used, charged = {}, [], 0.0, 0.0
        for i in range(self.n):
            soc, rows, prev = self.d[i]["soc_wh"], [], None
            for m in sorted((m for m in missions if m.drone == i and m.targets), key=lambda m: m.sortie):
                gained = 0.0
                if prev is not None:
                    new = self.refill(i, soc)
                    gained, soc = new - soc, new
                start = soc
                soc -= m.energy_wh
                used += m.energy_wh
                charged += gained
                if soc < self.reserve(i) - 1e-6:
                    viol.append(
                        f"drone {i} sortie {m.sortie}: battery would fall to {soc:.0f} Wh, "
                        f"below the {self.reserve(i):.0f} Wh safety reserve (started at {start:.0f} Wh after charging)"
                    )
                rows.append({"sortie": m.sortie, "soc_start": round(start, 1), "soc_end": round(soc, 1), "charged": round(gained, 1)})
                prev = m
            out[i] = rows
        return {"drones": out, "violations": viol, "used_wh": round(used, 1), "charged_wh": round(charged, 1)}

    def commit(self, sim):
        """Apply a flown day: end-of-day state of charge, wear."""
        for i, rows in sim["drones"].items():
            if not rows:
                continue
            x = self.d[i]
            used = sum(r["soc_start"] - r["soc_end"] for r in rows)
            x["soc_wh"] = max(0.0, rows[-1]["soc_end"])
            x["cycles"] += used / max(1.0, self.cap(i))
            x["health"] = max(MIN_HEALTH, 1.0 - FADE_PER_CYCLE * x["cycles"])

    def summary(self):
        return {
            "battery_health_min": round(min(x["health"] for x in self.d), 4),
            "soc_min_pct": round(100 * min(x["soc_wh"] / self.cap(i) for i, x in enumerate(self.d)), 1),
            "cycles_max": round(max(x["cycles"] for x in self.d), 2),
        }
