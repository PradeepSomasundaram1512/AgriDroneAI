"""Real-vehicle adapter for PX4/ArduPilot via MAVSDK.

Layers (so the safety-critical logic is testable without a drone):
  FlightExecutor  - preflight checks, mission build, monitoring, abort/RTL.  Pure asyncio, fully unit-tested.
  Link (protocol) - what the executor needs from a vehicle.
  MavsdkLink      - thin wrapper over mavsdk-grpc. NOT yet validated on real hardware or PX4 SITL.
  PayloadDriver   - hook that fires the sprayer/valve when a waypoint is reached. Default only logs;
                    wiring real actuation (servo/relay on the companion computer) is hardware-specific.
Never imported by the cloud autopilot: only the ground station (docs/GROUND_STATION.md) uses it."""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

from . import safety
from .geo import cell_to_latlon


log = logging.getLogger("agridrone.hardware")


class PreflightError(Exception):
    pass


@dataclass
class FlightResult:
    drone: int
    status: str  # completed | aborted | refused | dry_run
    completed: int = 0
    total: int = 0
    reason: str = ""
    items: list = field(default_factory=list)


class NullPayload:
    """Logs instead of actuating. Replace with a driver for your sprayer/valve."""

    def __init__(self):
        self.events = []

    async def trigger(self, action, cell):
        self.events.append((action, tuple(cell)))


class MavsdkLink:
    def __init__(self, address):
        try:
            import mavsdk_grpc as mavsdk
        except ImportError as e:
            raise PreflightError("install hardware extra: pip install 'agridrone[hardware]'") from e
        # one mavsdk_server per vehicle, so each needs its own gRPC port (derived from the vehicle's UDP port)
        m = re.search(r":(\d+)$", address)
        grpc_port = 50000 + int(m.group(1)) % 10000 if m else 50051
        self._m, self.address, self.sys = mavsdk, address, mavsdk.System(port=grpc_port)

    async def connect(self, timeout=30):
        await self.sys.connect(system_address=self.address)

        async def wait():
            async for s in self.sys.core.connection_state():
                if s.is_connected:
                    break

        await asyncio.wait_for(wait(), timeout)

    async def ready(self, timeout=60):
        async def wait():
            async for h in self.sys.telemetry.health():
                if h.is_global_position_ok and h.is_home_position_ok:
                    return True
            return False

        try:
            return bool(await asyncio.wait_for(wait(), timeout))
        except TimeoutError:
            return False

    async def battery_pct(self):
        async for b in self.sys.telemetry.battery():
            v = b.remaining_percent
            return v * 100 if v <= 1.0 else v  # MAVSDK versions differ: 0..1 vs 0..100
        return None

    async def upload(self, waypoints, speed, dwell_s):
        MI = self._m.mission.MissionItem
        nan = float("nan")
        items = [
            MI(lat, lon, alt, speed, False, nan, nan, MI.CameraAction.NONE, dwell_s, nan, 2.0, nan, nan, MI.VehicleAction.NONE)
            for lat, lon, alt in waypoints
        ]
        m = self.sys.mission
        await m.clear_mission()  # an autopilot that rebooted may still hold the previous sortie's mission/progress
        await m.set_return_to_launch_after_mission(True)
        await m.upload_mission(self._m.mission.MissionPlan(items))
        await m.set_current_mission_item(0)

    async def start(self):
        await self.sys.action.arm()
        await self.sys.mission.start_mission()

    async def progress(self):
        async for p in self.sys.mission.mission_progress():
            yield p.current, p.total

    async def rtl(self):
        await self.sys.action.return_to_launch()

    async def ensure_failsafe(self):
        """Autopilot-side data-link-loss failsafe must be RETURN (the ground station cannot command RTL once the link
        is gone). PX4: NAV_DLL_ACT 2 = return. Set, then read back to verify."""
        try:
            await self.sys.param.set_param_int("NAV_DLL_ACT", 2)
            return await self.sys.param.get_param_int("NAV_DLL_ACT") == 2
        except Exception:
            return False

    async def connection_lost(self, grace_s):
        """Returns once the vehicle link has been down for grace_s seconds."""
        async for st in self.sys.core.connection_state():
            if not st.is_connected:
                await asyncio.sleep(grace_s)
                return True
        return False

    async def wait_airborne(self, timeout):
        async def wait():
            async for in_air in self.sys.telemetry.in_air():
                if in_air:
                    return True
            return False

        try:
            return bool(await asyncio.wait_for(wait(), timeout))
        except TimeoutError:
            return False

    async def wait_landed(self, timeout):
        async def wait():
            async for in_air in self.sys.telemetry.in_air():
                if not in_air:
                    return True
            return False

        try:
            return bool(await asyncio.wait_for(wait(), timeout))
        except TimeoutError:
            return False


class FlightExecutor:
    def __init__(self, policy, link_factory=MavsdkLink, payload=None):
        self.policy, self.hw, self.fleet = policy, policy["hardware"], policy["fleet"]
        self.link_factory, self.payload = link_factory, payload or NullPayload()

    # --- pure planning, no I/O ---
    def waypoints(self, mission):
        return [(*cell_to_latlon(c, self.hw["origin"], self.policy["field"]["cell_m"]), mission.altitude_m) for c, _ in mission.targets]

    def preflight_policy(self, missions):
        if not self.hw.get("enabled"):
            raise PreflightError("hardware.enabled is false in policy")
        if os.environ.get("AGRIDRONE_ARMED") != "1":
            raise PreflightError("physical interlock: AGRIDRONE_ARMED=1 not set on this ground station")
        if any(m.hold for m in missions):
            raise PreflightError("pre-landing holds cannot be flown on hardware (the return-to-launch timing cannot be delayed)")
        v = safety.validate(missions, self.policy)
        if v:
            raise PreflightError("; ".join(v))
        size = self.policy["field"]["size"]
        for m in missions:
            for (x, y), _ in m.targets:
                if not (0 <= x < size and 0 <= y < size):
                    raise PreflightError(f"target {(x, y)} outside geofence")
            if m.drone >= len(self.hw["links"]):
                raise PreflightError(f"no link configured for drone {m.drone}")

    # --- per-drone flight ---
    async def fly_one(self, mission, dry_run=False):
        wps = self.waypoints(mission)
        total = len(wps)
        if dry_run:
            return FlightResult(mission.drone, "dry_run", 0, total, items=wps)
        link = self.link_factory(self.hw["links"][mission.drone])
        reserve = self.fleet["min_reserve_pct"]
        try:
            log.info("drone %s: connecting", mission.drone)
            await link.connect()
            log.info("drone %s: connected, waiting for GPS/home lock", mission.drone)
            if not await link.ready():
                return FlightResult(mission.drone, "refused", 0, total, "no GPS/home lock")
            pct = await link.battery_pct()
            log.info("drone %s: lock ok, battery %s%%", mission.drone, pct)
            need = mission.energy_wh / self.fleet["battery_wh"] * 100 + reserve
            if pct is None or pct < need:
                return FlightResult(mission.drone, "refused", 0, total, f"battery {pct}% < required {need:.0f}%")
            if hasattr(link, "ensure_failsafe") and not await link.ensure_failsafe():
                return FlightResult(mission.drone, "refused", 0, total, "could not set/verify autopilot link-loss failsafe (RTL)")
            await link.upload(wps, self.hw.get("speed_m_s", 5.0), self.hw.get("dwell_s", 4))
            log.info("drone %s: mission uploaded (%d waypoints), arming + starting", mission.drone, total)
            await link.start()
            log.info("drone %s: started, waiting for takeoff", mission.drone)
            # Progress/in_air streams can replay STALE state from a previous mission, so completion is only
            # believed after takeoff is confirmed (found by SITL: a restart produced an instant false "completed").
            if not await link.wait_airborne(self.hw.get("takeoff_wait_s", 90)):
                await self._abort(link)
                return FlightResult(mission.drone, "aborted", 0, total, "no takeoff confirmed; RTL/land commanded")
            log.info("drone %s: airborne, monitoring", mission.drone)
            res = await asyncio.wait_for(self._watched(link, mission, total, reserve), self.hw.get("max_flight_s", 1200))
            log.info("drone %s: mission %s (%d/%d), waiting for landing", mission.drone, res.status, res.completed, total)
            # the drone is only "done" once it is back on the ground; the next sortie must not start before that
            if not await link.wait_landed(self.hw.get("land_wait_s", 300)):
                res.status, res.reason = "aborted", (res.reason + "; did not confirm landing").lstrip("; ")
            return res
        except TimeoutError:
            sent = await self._abort(link)
            return FlightResult(mission.drone, "aborted", 0, total, "timeout; " + ("RTL commanded" if sent else "RTL NOT sent"))
        except Exception as e:  # any surprise => fail safe
            sent = await self._abort(link)
            return FlightResult(
                mission.drone,
                "aborted",
                0,
                total,
                f"{type(e).__name__}: {str(e)[:80]}; "
                + ("RTL commanded" if sent else "RTL could NOT be sent; relying on autopilot failsafe"),
            )

    async def _watched(self, link, mission, total, reserve):
        """Run the monitor while watching the vehicle link; on loss, try RTL and report honestly."""
        state = {"reached": 0}
        mon = asyncio.ensure_future(self._monitor(link, mission, total, reserve, state))
        guard = asyncio.ensure_future(self._battery_guard(link, mission, total, reserve, state))
        lost = (
            asyncio.ensure_future(link.connection_lost(self.hw.get("link_loss_grace_s", 3))) if hasattr(link, "connection_lost") else None
        )
        try:
            await asyncio.wait([t for t in (mon, guard, lost) if t], return_when=asyncio.FIRST_COMPLETED)
            if mon.done():
                return mon.result()
            if guard.done() and not guard.cancelled() and guard.exception() is None:
                mon.cancel()
                return guard.result()
            mon.cancel()
            sent = await self._abort(link)
            how = "RTL commanded" if sent else "RTL could NOT be sent; relying on autopilot link-loss failsafe"
            log.error("drone %s: LINK LOST in flight; %s", mission.drone, how)
            return FlightResult(mission.drone, "aborted", state["reached"], total, f"link lost; {how}")
        finally:
            for t in (mon, guard, lost):
                if t and not t.done():
                    t.cancel()
                elif t and not t.cancelled():
                    t.exception()  # retrieve so a dead link stream does not log "never retrieved"

    async def _battery_guard(self, link, mission, total, reserve, state):
        """Battery is polled on its own timer: waypoint-progress events can be tens of seconds apart (found in SITL:
        a progress-driven check only fired at 10% when the 25% reserve was crossed ~40s earlier)."""
        poll = self.hw.get("battery_poll_s", 2.0)
        while True:
            await asyncio.sleep(poll)
            try:
                pct = await asyncio.wait_for(link.battery_pct(), self.hw.get("telemetry_timeout_s", 6))
            except TimeoutError:
                sent = await self._abort(link)
                how = "RTL commanded" if sent else "RTL could NOT be sent; relying on autopilot link-loss failsafe"
                log.error("drone %s: TELEMETRY SILENT (link lost); %s", mission.drone, how)
                return FlightResult(mission.drone, "aborted", state["reached"], total, f"link lost (telemetry silent); {how}")
            if pct is not None and pct < reserve:
                sent = await self._abort(link)
                log.error(
                    "drone %s: battery %.0f%% below reserve %s%%; %s",
                    mission.drone,
                    pct,
                    reserve,
                    "RTL commanded" if sent else "RTL NOT sent",
                )
                return FlightResult(
                    mission.drone,
                    "aborted",
                    state["reached"],
                    total,
                    f"battery {pct:.0f}% below reserve; " + ("RTL commanded" if sent else "RTL could NOT be sent"),
                )

    async def _monitor(self, link, mission, total, reserve, state=None):
        state = state if state is not None else {"reached": 0}
        reached = 0
        async for cur, tot in link.progress():
            if tot != total:  # stale event from a different mission
                log.warning("drone %s: ignoring stale progress %s/%s (expected %s waypoints)", mission.drone, cur, tot, total)
                continue
            while reached < min(cur, total):  # waypoint `reached` was just completed
                if mission.targets[reached][1] != "via":  # detour waypoints do not trigger the sprayer/valve
                    await self.payload.trigger(*reversed(mission.targets[reached]))
                reached += 1
                state["reached"] = reached
            pct = await link.battery_pct()
            if pct is not None and pct < reserve:
                await link.rtl()
                return FlightResult(mission.drone, "aborted", reached, total, f"battery {pct:.0f}% below reserve; RTL")
            if cur >= tot:
                break
        while reached < total:
            if mission.targets[reached][1] != "via":
                await self.payload.trigger(*reversed(mission.targets[reached]))
            reached += 1
        return FlightResult(mission.drone, "completed", reached, total)

    async def _abort(self, link):
        """Command return-to-launch, time-bounded (a dead link makes gRPC calls hang forever).
        True only if the command was actually sent."""
        try:
            await asyncio.wait_for(link.rtl(), self.hw.get("rtl_timeout_s", 5))
            return True
        except Exception:
            return False

    async def fly(self, missions, dry_run=False):
        if not dry_run:
            self.preflight_policy(missions)
        return await asyncio.gather(*[self._staggered(m, dry_run) for m in missions])

    async def _staggered(self, m, dry_run):
        """Launch times come from the traffic scheduler: a drone that must wait for another to clear the airspace waits here,
        on the ground, before it even connects."""
        if not dry_run and m.t0 > 0:
            log.info("drone %s: holding on the pad for %ss (airspace deconfliction)", m.drone, m.t0)
            await asyncio.sleep(m.t0 * self.hw.get("launch_stagger_scale", 1.0))
        return await self.fly_one(m, dry_run)
