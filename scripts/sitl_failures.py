"""Failure-mode tests against PX4 SITL.  Fresh container per scenario (see scripts/sitl_failures.sh).
  lowbat   : sim battery drains below the reserve mid-flight  -> expect abort + RTL + confirmed landing
  linkloss : ground link is killed mid-flight                  -> expect abort; autopilot failsafe must bring it home"""
import asyncio, logging, os, signal, subprocess, sys, threading, time
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
from agridrone.config import load_policy
from agridrone.hardware import FlightExecutor, MavsdkLink, NullPayload
from agridrone.safety import Mission, mission_energy

LOG_RE = r"(INFO|WARN|ERROR) +\[[a-z_]+\][^[:cntrl:]]{0,90}"


def px4_events():
    out = subprocess.run(f"docker logs px4 2>&1 | grep -aoE '{LOG_RE}' | grep -aE 'Return|Landing|landed|Disarm|Failsafe|failsafe|link|Link|Low battery|low battery|Takeoff|Armed|Mission' | uniq",
                         shell=True, capture_output=True, text=True).stdout
    return out.strip().splitlines()


def kill_mavsdk_server():
    me = os.getpid()
    for line in subprocess.run(["ps", "-axo", "pid,command"], capture_output=True, text=True).stdout.splitlines():
        if "bin/mavsdk_server" in line and "sitl_failures" not in line:
            pid = int(line.split()[0])
            if pid != me:
                os.kill(pid, signal.SIGKILL); print(f"*** killed mavsdk_server pid {pid} (ground link lost)", flush=True)


async def main(scenario):
    p = load_policy()
    p["hardware"].update(enabled=True, max_flight_s=240, dwell_s=2)
    p["hardware"]["links"] = ["udp://:14540"]
    targets = [((3, 3), "irrigate"), ((6, 3), "spray")]
    if scenario == "linkloss":  # long (~160s) so an early return can only be the autopilot's link-loss failsafe
        targets = [((3, 3), "irrigate"), ((10, 3), "spray"), ((10, 8), "spray")]
    m = Mission(0, 30, targets)
    m.energy_wh = mission_energy((0, 0), m.targets, p["fleet"])
    link = MavsdkLink("udp://:14540")
    await link.connect()
    if scenario == "lowbat":
        await link.sys.param.set_param_float("SIM_BAT_DRAIN", 30.0)
        await link.sys.param.set_param_float("SIM_BAT_MIN_PCT", 10.0)
        print("sim battery set to drain 100%->10% over 30s of armed time", flush=True)
    async def noop(*a, **k): pass
    link.connect = noop                       # reuse the already-connected link
    if scenario == "linkloss":
        threading.Timer(30, kill_mavsdk_server).start()
    t = time.time()
    r = (await FlightExecutor(p, link_factory=lambda a: link, payload=NullPayload()).fly([m]))[0]
    print(f"RESULT {scenario}: {r.status} {r.completed}/{r.total} reason={r.reason!r} t={time.time()-t:.0f}s", flush=True)
    return r


if __name__ == "__main__":
    sc = sys.argv[1]
    r = asyncio.run(main(sc))
    if sc == "linkloss":  # executor can't see the vehicle any more: use PX4's own log as the witness
        print("waiting up to 200s for autopilot-side evidence...", flush=True)
        t0 = time.time()
        for _ in range(40):
            ev = px4_events()
            if any("Disarmed" in e or "Landing detected" in e for e in ev[-8:]) and any("Return" in e or "link" in e.lower() for e in ev):
                break
            time.sleep(5)
        print(f"autopilot evidence settled {time.time()-t0:.0f}s after executor gave up (mission alone needs ~160s)", flush=True)
    time.sleep(1)
    print("--- PX4 log events ---"); print("\n".join(px4_events()[-14:]))
