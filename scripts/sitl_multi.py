"""Multi-drone SITL: N independent PX4 simulators (one container each), flown concurrently by one FlightExecutor.
Separate sims share no airspace, so this tests coordination (concurrency, per-drone links, failure isolation),
NOT physical separation.   scenarios: all | oneweak (drone 1 gets a draining battery)"""
import asyncio, logging, sys, time
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
from agridrone import planner, safety
from agridrone.config import load_policy
from agridrone.hardware import FlightExecutor, MavsdkLink, NullPayload
from agridrone.safety import Mission, mission_energy


async def main(scenario, n):
    p = load_policy()
    p["fleet"]["drones"] = n
    p["hardware"].update(enabled=True, max_flight_s=300, dwell_s=2)
    p["hardware"]["links"] = [f"udp://:{14540 + i}" for i in range(n)]
    # missions through the real planner + safety gate: tasks spread over the field, one altitude layer per drone
    targets = [(1.0, c, a) for c, a in [((3, 3), "irrigate"), ((8, 3), "spray"), ((3, 8), "irrigate"), ((8, 8), "spray"), ((12, 5), "irrigate"), ((5, 12), "spray")]]
    missions = planner.plan(targets, p)
    missions, dropped = safety.repair(missions, p)
    missions = sorted(missions[:n], key=lambda m: m.drone)
    assert not safety.validate(missions, p), safety.validate(missions, p)
    print("PLAN", [(m.drone, m.altitude_m, [c for c, _ in m.targets]) for m in missions], flush=True)
    pay = NullPayload()
    t = time.time()
    weak_addr = p["hardware"]["links"][1] if len(p["hardware"]["links"]) > 1 else None

    class Link(MavsdkLink):
        async def connect(self, timeout=30):
            await super().connect(timeout)
            if scenario == "oneweak" and self.address == weak_addr:   # only drone 1's battery drains
                await self.sys.param.set_param_float("SIM_BAT_DRAIN", 30.0)
                await self.sys.param.set_param_float("SIM_BAT_MIN_PCT", 10.0)
                print("drone 1 battery will drain 100%->10% over 30s armed", flush=True)

    res = await FlightExecutor(p, link_factory=Link, payload=pay).fly(missions)
    wall = time.time() - t
    for r in res:
        print(f"RESULT drone {r.drone}: {r.status} {r.completed}/{r.total} reason={r.reason!r}", flush=True)
    print(f"WALL {wall:.0f}s  payload events={len(pay.events)}", flush=True)
    return res

if __name__ == "__main__":
    res = asyncio.run(main(sys.argv[1], int(sys.argv[2])))
    ok = all(r.status == "completed" for r in res) if sys.argv[1] == "all" else \
        (res[1].status == "aborted" and all(r.status == "completed" for i, r in enumerate(res) if i != 1))
    print("PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
