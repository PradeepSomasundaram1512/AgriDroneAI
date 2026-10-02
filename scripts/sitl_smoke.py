"""Fly one 2-waypoint mission through the REAL MavsdkLink against PX4 SITL.
  docker run --rm -d --name px4 -p 14540:14540/udp jonasvautherin/px4-gazebo-headless:latest
  PYTHONPATH=. AGRIDRONE_ARMED=1 python scripts/sitl_smoke.py
PX4 SITL's default home is 47.397742, 8.545594, which matches config/policy.json hardware.origin."""
import asyncio, os, sys, time
from agridrone.config import load_policy
from agridrone.hardware import FlightExecutor, NullPayload
from agridrone.safety import Mission, mission_energy


async def main():
    p = load_policy()
    p["hardware"].update(enabled=True, max_flight_s=float(os.environ.get('SITL_MAX_FLIGHT_S', 240)), dwell_s=2)
    p["hardware"]["links"] = ["udp://:14540"]
    m = Mission(0, 30, [((3, 3), "irrigate"), ((4, 3), "spray")])
    m.energy_wh = mission_energy((0, 0), m.targets, p["fleet"])
    pay = NullPayload()
    t = time.time()
    r = (await FlightExecutor(p, payload=pay).fly([m]))[0]
    print(f"{r.status} {r.completed}/{r.total} reason={r.reason!r} t={time.time()-t:.0f}s payload={pay.events}")
    if os.environ.get("SITL_EXPECT") == "aborted":  # failure-path test: force a short timeout
        return 0 if r.status == "aborted" and "RTL" in r.reason else 1
    return 0 if r.status == "completed" and r.completed == 2 else 1

sys.exit(asyncio.run(main()))
