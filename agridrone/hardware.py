"""Real-vehicle adapter for PX4/ArduPilot via MAVSDK.

Layers (so the safety-critical logic is testable without a drone):
  FlightExecutor  - preflight checks, mission build, monitoring, abort/RTL.  Pure asyncio, fully unit-tested.
  Link (protocol) - what the executor needs from a vehicle.
  MavsdkLink      - thin wrapper over mavsdk-grpc. NOT yet validated on real hardware or PX4 SITL.
  PayloadDriver   - hook that fires the sprayer/valve when a waypoint is reached. Default only logs;
                    wiring real actuation (servo/relay on the companion computer) is hardware-specific.
Never imported by the cloud autopilot: only the ground station (docs/GROUND_STATION.md) uses it."""
import asyncio
import math
import os
from dataclasses import dataclass, field

from . import safety
from .geo import cell_to_latlon


class PreflightError(Exception):
    pass


@dataclass
class FlightResult:
    drone: int
    status: str            # completed | aborted | refused | dry_run
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
        self._m, self.address, self.sys = mavsdk, address, mavsdk.System()

    async def connect(self, timeout=30):
        await self.sys.connect(system_address=self.address)
        async def wait():
            async for s in self.sys.core.connection_state():
                if s.is_connected:
                    return
        await asyncio.wait_for(wait(), timeout)

    async def ready(self, timeout=60):
        async def wait():
            async for h in self.sys.telemetry.health():
                if h.is_global_position_ok and h.is_home_position_ok:
                    return True
        try:
            return bool(await asyncio.wait_for(wait(), timeout))
        except asyncio.TimeoutError:
            return False

    async def battery_pct(self):
        async for b in self.sys.telemetry.battery():
            v = b.remaining_percent
            return v * 100 if v <= 1.0 else v  # MAVSDK versions differ: 0..1 vs 0..100

    async def upload(self, waypoints, speed, dwell_s):
        MI = self._m.mission.MissionItem
        nan = float("nan")
        items = [MI(lat, lon, alt, speed, False, nan, nan, MI.CameraAction.NONE, dwell_s, nan, 2.0, nan, nan,
                    MI.VehicleAction.NONE) for lat, lon, alt in waypoints]
        await self.sys.mission.set_return_to_launch_after_mission(True)
        await self.sys.mission.upload_mission(self._m.mission.MissionPlan(items))

    async def start(self):
        await self.sys.action.arm()
        await self.sys.mission.start_mission()

    async def progress(self):
        async for p in self.sys.mission.mission_progress():
            yield p.current, p.total

    async def rtl(self):
        await self.sys.action.return_to_launch()


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
            await link.connect()
            if not await link.ready():
                return FlightResult(mission.drone, "refused", 0, total, "no GPS/home lock")
            pct = await link.battery_pct()
            need = mission.energy_wh / self.fleet["battery_wh"] * 100 + reserve
            if pct is None or pct < need:
                return FlightResult(mission.drone, "refused", 0, total, f"battery {pct}% < required {need:.0f}%")
            await link.upload(wps, self.hw.get("speed_m_s", 5.0), self.hw.get("dwell_s", 4))
            await link.start()
            return await asyncio.wait_for(self._monitor(link, mission, total, reserve), self.hw.get("max_flight_s", 1200))
        except asyncio.TimeoutError:
            await self._abort(link)
            return FlightResult(mission.drone, "aborted", 0, total, "timeout; RTL commanded")
        except Exception as e:  # any surprise => fail safe
            await self._abort(link)
            return FlightResult(mission.drone, "aborted", 0, total, f"{type(e).__name__}: {e}; RTL commanded")

    async def _monitor(self, link, mission, total, reserve):
        reached = 0
        async for cur, tot in link.progress():
            while reached < min(cur, total):  # waypoint `reached` was just completed
                await self.payload.trigger(*reversed(mission.targets[reached]))
                reached += 1
            pct = await link.battery_pct()
            if pct is not None and pct < reserve:
                await link.rtl()
                return FlightResult(mission.drone, "aborted", reached, total, f"battery {pct:.0f}% below reserve; RTL")
            if cur >= tot:
                break
        while reached < total:
            await self.payload.trigger(*reversed(mission.targets[reached]))
            reached += 1
        return FlightResult(mission.drone, "completed", reached, total)

    @staticmethod
    async def _abort(link):
        try:
            await link.rtl()
        except Exception:
            pass

    async def fly(self, missions, dry_run=False):
        if not dry_run:
            self.preflight_policy(missions)
        return await asyncio.gather(*[self.fly_one(m, dry_run) for m in missions])
