"""GPS-loss scenarios against REAL PX4 (SITL). SIM_GPS_USED sets the satellites the simulated receiver uses; 0 = total GPS loss.
  scenarios:  preflight  (6 sats before takeoff)   -> must be REFUSED
              dip        (GPS gone ~4 s mid-flight) -> hold, then resume and finish (grace not exceeded)
              loss       (GPS gone for good)        -> hold, then land in place, status gps_lost
  usage: scripts/sitl_gps.sh <scenario>"""

import asyncio
import logging
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
from agridrone.config import load_policy
from agridrone.hardware import FlightExecutor, MavsdkLink, NullPayload
from agridrone.safety import Mission, mission_energy

SC = sys.argv[1]


class Scripted(MavsdkLink):
    async def connect(self, timeout=30):
        await super().connect(timeout)
        if SC == "preflight":
            await self.sys.param.set_param_int("SIM_GPS_USED", 6)
            await asyncio.sleep(3)

    async def start(self):
        await super().start()
        if SC in ("dip", "loss"):
            asyncio.get_event_loop().create_task(self._script())

    async def _script(self):
        await asyncio.sleep(22)  # let it take off and fly for a while
        logging.info("*** SIM_GPS_USED -> 0 (GPS lost)")
        await self.sys.param.set_param_int("SIM_GPS_USED", 0)
        if SC == "dip":
            await asyncio.sleep(4)
            logging.info("*** SIM_GPS_USED -> 10 (GPS back)")
            await self.sys.param.set_param_int("SIM_GPS_USED", 10)


def px4_events():
    cmd = "docker logs px4 2>&1 | grep -aoE '(INFO|WARN|ERROR) +\\[[a-z_]+\\][^[:cntrl:]]{0,90}' | grep -aiE 'gps|position|failsafe|Landing|landed|Disarm|Hold|Mission|Returning|navigation' | uniq | tail -16"
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


async def main():
    p = load_policy()
    p["hardware"].update(enabled=True, max_flight_s=300, dwell_s=2, links=["udp://:14540"])
    p["gps"] = {**p.get("gps", {}), "grace_s": 8.0, "poll_s": 1.0}
    m = Mission(0, 30, [((6, 3), "irrigate"), ((12, 3), "spray"), ((12, 9), "irrigate"), ((6, 9), "spray")])
    m.energy_wh = mission_energy((0, 0), m.targets, p["fleet"])
    pay = NullPayload()
    r = (await FlightExecutor(p, link_factory=Scripted, payload=pay).fly([m]))[0]
    print(f"RESULT {SC}: {r.status} {r.completed}/{r.total} reason={r.reason!r} payload_events={len(pay.events)}", flush=True)
    await asyncio.sleep(8)
    print("--- PX4's own log ---\n" + px4_events())
    ok = {"preflight": r.status == "refused", "dip": r.status == "completed", "loss": r.status == "gps_lost"}[SC]
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
