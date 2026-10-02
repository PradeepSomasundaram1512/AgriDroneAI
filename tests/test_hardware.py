import asyncio
import pytest
from agridrone import store
from agridrone.agent import run_cycle
from agridrone.config import load_policy
from agridrone.geo import cell_to_latlon
from agridrone.ground_station import process_once
from agridrone.hardware import FlightExecutor, NullPayload, PreflightError
from agridrone.safety import Mission


class FakeLink:
    instances = []

    def __init__(self, addr, battery=90.0, ready=True, drain=0.0, fail=None, hang=False):
        self.addr, self.bat, self._ready, self.drain, self.fail, self.hang = addr, battery, ready, drain, fail, hang
        self.rtl_called, self.uploaded, self.started = False, None, False
        FakeLink.instances.append(self)

    async def connect(self):
        pass

    async def ready(self):
        return self._ready

    async def battery_pct(self):
        return self.bat

    async def upload(self, wps, speed, dwell):
        if self.fail:
            raise RuntimeError(self.fail)
        self.uploaded = wps

    async def start(self):
        self.started = True

    async def progress(self):
        n = len(self.uploaded)
        if getattr(self, "stale", False):
            yield 7, 7  # stale event replayed from a previous mission
        for i in range(1, n + 1):
            if self.hang:
                await asyncio.sleep(10)
            self.bat -= self.drain
            yield i, n

    async def rtl(self):
        if getattr(self, "rtl_fails", False):
            raise ConnectionError("link down")
        self.rtl_called = True

    async def ensure_failsafe(self, wind_limit_ms=None):
        self.wind_limit = wind_limit_ms
        return not getattr(self, "bad_failsafe", False)

    async def connection_lost(self, grace):
        if getattr(self, "lose_link", False):
            await asyncio.sleep(0.05)
            return True
        await asyncio.sleep(3600)

    async def wait_airborne(self, timeout):
        return not getattr(self, "no_takeoff", False)

    async def wait_landed(self, timeout):
        return not getattr(self, "stuck", False)


def pol(**hw):
    p = load_policy()
    p["hardware"].update({"enabled": True, "launch_stagger_scale": 0.0, **hw})
    return p


def mission(targets=(((5, 5), "irrigate"), ((6, 5), "spray")), drone=0, alt=30):
    return Mission(drone, alt, list(targets), 20.0)


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    FakeLink.instances.clear()
    monkeypatch.setenv("AGRIDRONE_ARMED", "1")


def ex(p=None, **kw):
    pay = NullPayload()
    return FlightExecutor(p or pol(), link_factory=lambda a: FakeLink(a, **kw), payload=pay), pay


def test_cell_to_latlon_scale():
    o = {"lat": 47.0, "lon": 8.0}
    lat, lon = cell_to_latlon((0, 0), o, 50)
    assert abs((lat - 47.0) * 111320 - 25) < 0.5
    lat2, _ = cell_to_latlon((0, 2), o, 50)
    assert abs((lat2 - lat) * 111320 - 100) < 0.5


def test_happy_path_triggers_payload_per_waypoint():
    e, pay = ex()
    r = asyncio.run(e.fly([mission()]))[0]
    assert r.status == "completed" and r.completed == 2
    assert pay.events == [("irrigate", (5, 5)), ("spray", (6, 5))]
    assert FakeLink.instances[0].started and not FakeLink.instances[0].rtl_called


def test_interlock_and_policy_flag(monkeypatch):
    monkeypatch.delenv("AGRIDRONE_ARMED")
    with pytest.raises(PreflightError, match="interlock"):
        asyncio.run(ex()[0].fly([mission()]))
    monkeypatch.setenv("AGRIDRONE_ARMED", "1")
    p = load_policy()  # hardware.enabled False
    with pytest.raises(PreflightError, match="enabled"):
        asyncio.run(FlightExecutor(p, link_factory=FakeLink).fly([mission()]))


def test_nofly_and_geofence_blocked_before_connecting():
    for t in ([((10, 10), "spray")], [((99, 1), "spray")]):
        with pytest.raises(PreflightError):
            asyncio.run(ex()[0].fly([mission(t)]))
    assert FakeLink.instances == []


def test_refuses_without_gps_or_low_battery():
    assert asyncio.run(ex(ready=False)[0].fly([mission()]))[0].status == "refused"
    r = asyncio.run(ex(battery=30)[0].fly([mission()]))[0]
    assert r.status == "refused" and "battery" in r.reason
    assert not FakeLink.instances[-1].started


def test_battery_drain_in_flight_triggers_rtl():
    r = asyncio.run(ex(battery=60, drain=40)[0].fly([mission()]))[0]
    assert r.status == "aborted" and FakeLink.instances[0].rtl_called


def test_link_failure_and_timeout_fail_safe():
    # a failure BEFORE arming (here: the mission upload) is a refusal, not an emergency: nothing flew, nothing needs recovering
    r = asyncio.run(ex(fail="boom")[0].fly([mission()]))[0]
    assert r.status == "refused" and "preparation failed" in r.reason and not FakeLink.instances[0].started
    FakeLink.instances.clear()

    class DiesInFlight(FakeLink):
        async def progress(self):
            raise RuntimeError("telemetry exploded mid-flight")
            yield  # pragma: no cover

    # a failure AFTER launch is an emergency: command return-to-launch
    r = asyncio.run(FlightExecutor(pol(), link_factory=lambda a: DiesInFlight(a), payload=NullPayload()).fly([mission()]))[0]
    assert r.status == "aborted" and "RuntimeError" in r.reason and FakeLink.instances[0].started and FakeLink.instances[0].rtl_called
    FakeLink.instances.clear()
    e, _ = ex(pol(max_flight_s=0.05), hang=True)
    r = asyncio.run(e.fly([mission()]))[0]
    assert r.status == "aborted" and "timeout" in r.reason and FakeLink.instances[0].rtl_called


def test_dry_run_never_connects():
    r = asyncio.run(ex()[0].fly([mission()], dry_run=True))[0]
    assert r.status == "dry_run" and len(r.items) == 2 and FakeLink.instances == []


def test_agent_queues_and_ground_station_flies(tmp_path):
    p = pol()
    p["autonomy_level"] = "autonomous"
    p["field"]["size"] = 16
    for _ in range(25):
        run_cycle(p, tmp_path)
    q = store.read_jsonl("queue.jsonl", tmp_path)
    assert q and q[-1]["approval"] == "auto"
    e, _ = ex(p, battery=100)
    out = process_once(p, tmp_path, executor=e)
    assert out["status"] == "completed"
    assert process_once(p, tmp_path, executor=e) is None  # already flown, nothing re-flown
    flown = {f["id"] for f in store.read_jsonl("flights.jsonl", tmp_path)}
    assert q[-1]["id"] in flown and {x["id"] for x in q} <= flown  # older ones logged superseded


def test_supervised_waits_for_human_and_expiry(tmp_path):
    p = pol()
    p["autonomy_level"] = "supervised"
    p["field"]["size"] = 16
    for _ in range(25):
        run_cycle(p, tmp_path)
    q = store.read_jsonl("queue.jsonl", tmp_path)[-1]
    assert q["approval"] == "human"
    e, _ = ex(p, battery=100)
    assert process_once(p, tmp_path, executor=e) is None
    assert process_once(p, tmp_path, executor=e, human_approved=q["id"])["status"] == "completed"


def test_stale_mission_expires_and_kill_switch(tmp_path):
    p = pol()
    p["autonomy_level"] = "autonomous"
    p["field"]["size"] = 16
    for _ in range(25):
        run_cycle(p, tmp_path)
    q = store.read_jsonl("queue.jsonl", tmp_path)[-1]
    assert process_once(p, tmp_path, executor=ex(p)[0], now=q["ts"] + 13 * 3600) is None
    assert any(f["status"] == "expired" for f in store.read_jsonl("flights.jsonl", tmp_path))
    store.append_jsonl("queue.jsonl", {**q, "id": "x", "ts": q["ts"] + 13 * 3600}, tmp_path)
    p["kill_switch"] = True
    assert process_once(p, tmp_path, executor=ex(p)[0], now=q["ts"] + 13 * 3600 + 1)["status"] == "refused"


def test_unconfirmed_landing_is_not_completed():
    class Stuck(FakeLink):
        stuck = True

    e = FlightExecutor(pol(), link_factory=lambda a: Stuck(a), payload=NullPayload())
    r = asyncio.run(e.fly([mission()]))[0]
    assert r.status == "aborted" and "landing" in r.reason


def test_no_takeoff_is_never_completed_and_payload_silent():
    class Grounded(FakeLink):
        no_takeoff = True

    pay = NullPayload()
    r = asyncio.run(FlightExecutor(pol(), link_factory=lambda a: Grounded(a), payload=pay).fly([mission()]))[0]
    assert r.status == "aborted" and pay.events == [] and FakeLink.instances[-1].rtl_called


def test_stale_progress_event_is_ignored():
    class Stale(FakeLink):
        stale = True

    pay = NullPayload()
    r = asyncio.run(FlightExecutor(pol(), link_factory=lambda a: Stale(a), payload=pay).fly([mission()]))[0]
    assert r.status == "completed" and len(pay.events) == 2


def test_refuses_if_autopilot_failsafe_cannot_be_verified():
    class Bad(FakeLink):
        bad_failsafe = True

    r = asyncio.run(FlightExecutor(pol(), link_factory=lambda a: Bad(a), payload=NullPayload()).fly([mission()]))[0]
    assert r.status == "refused" and "failsafe" in r.reason and not FakeLink.instances[-1].started


def test_link_loss_midflight_aborts_and_reports_rtl_state():
    class Slow(FakeLink):
        lose_link = True

        async def progress(self):
            await asyncio.sleep(1)
            yield 1, 2

    r = asyncio.run(FlightExecutor(pol(), link_factory=lambda a: Slow(a), payload=NullPayload()).fly([mission()]))[0]
    assert r.status == "aborted" and "link lost" in r.reason and "RTL commanded" in r.reason

    class SlowDead(Slow):
        rtl_fails = True

    r = asyncio.run(FlightExecutor(pol(), link_factory=lambda a: SlowDead(a), payload=NullPayload()).fly([mission()]))[0]
    assert "could NOT be sent" in r.reason and "failsafe" in r.reason


def test_battery_guard_fires_even_when_no_progress_events_arrive():
    class Silent(FakeLink):
        async def battery_pct(self):
            self.bat -= 15
            return self.bat  # drains on every poll

        async def progress(self):
            await asyncio.sleep(5)
            yield 2, 2  # no waypoint events for a long time

    link = {}

    def f(a):
        link["l"] = Silent(a, battery=80)
        return link["l"]

    e = FlightExecutor(pol(battery_poll_s=0.02), link_factory=f, payload=NullPayload())
    r = asyncio.run(e.fly([mission()]))[0]
    assert r.status == "aborted" and "below reserve" in r.reason and link["l"].rtl_called


def test_dead_link_hangs_are_bounded_telemetry_silence_aborts():
    class Dead(FakeLink):
        async def battery_pct(self):
            if getattr(self, "dead", False):
                await asyncio.sleep(3600)  # gRPC to a dead server never returns
            return 90.0

        async def progress(self):
            self.dead = True
            await asyncio.sleep(3600)
            yield 1, 2

        async def rtl(self):
            await asyncio.sleep(3600)

    e = FlightExecutor(
        pol(battery_poll_s=0.02, telemetry_timeout_s=0.1, rtl_timeout_s=0.1), link_factory=lambda a: Dead(a), payload=NullPayload()
    )
    r = asyncio.run(asyncio.wait_for(e.fly([mission()]), 5))[0]
    assert r.status == "aborted" and "link lost" in r.reason and "could NOT be sent" in r.reason


def test_hold_before_landing_is_refused_on_hardware():
    m = mission()
    m.hold = 12
    m.energy_wh = 20.0
    with pytest.raises(PreflightError, match="holds"):
        asyncio.run(ex()[0].fly([m]))


def test_launch_stagger_keeps_a_drone_on_the_ground_until_its_slot():
    import time

    starts = {}

    class Timed(FakeLink):
        async def start(self):  # the moment each drone ARMS: connections now all happen first, in phase 1
            starts[self.addr] = time.monotonic()
            await super().start()

    from agridrone import traffic

    p = pol(launch_stagger_scale=0.03)
    a, b = mission(drone=0, alt=30), mission(drone=1, alt=50, targets=(((7, 5), "irrigate"),))
    assert traffic.schedule([a, b], p, allow_hold=False)["mode"] == "concurrent"  # real plans get their delays from the scheduler
    gap = abs(a.t0 - b.t0) * 0.03  # seconds of real waiting this test should see (the scale shrinks the 15 s of airspace deconfliction)
    asyncio.run(FlightExecutor(p, link_factory=lambda addr: Timed(addr), payload=NullPayload()).fly([a, b]))
    first, second = sorted(starts.values())
    assert gap > 0.3 and second - first >= 0.8 * gap


def test_ground_station_roundtrips_the_schedule_and_pad():
    from agridrone.ground_station import to_missions

    rec = {
        "missions": [
            {"drone": 1, "alt": 50, "energy_wh": 10.0, "sortie": 0, "t0": 6, "hold": 0, "pad": 1, "targets": [[[5, 5], "irrigate"]]}
        ]
    }
    m = to_missions(rec)[0]
    assert (m.t0, m.hold, m.pad, m.drone) == (6, 0, 1, 1)


def test_the_autopilot_wind_failsafe_is_armed_at_the_headway_limit():
    e, _ = ex()
    asyncio.run(e.fly([mission()]))
    assert FakeLink.instances[0].wind_limit == 0.75 * e.fleet["speed_ms"]  # 7.5 m/s for a 10 m/s airspeed


def test_no_drone_arms_until_every_drone_is_connected_even_if_a_connection_freezes_the_loop():
    """Found by the review: connecting a late-launching drone used to freeze the event loop while earlier drones were already airborne
    (their battery/GPS/link watchers dead). Phase 1 (connect + checks for all) must finish before phase 2 (arming) starts."""
    import time

    from agridrone import traffic

    events = []

    class Freezing(FakeLink):
        async def connect(self):
            events.append(("connect_start", self.addr))
            if self.addr.endswith("14540"):
                time.sleep(0.4)  # like mavsdk_server startup: blocks the WHOLE loop
            events.append(("connect_done", self.addr))

        async def start(self):
            events.append(("armed", self.addr))
            await super().start()

    p = pol(launch_stagger_scale=0.02)
    ms = [
        mission(drone=0, alt=30),
        mission(drone=1, alt=50, targets=(((7, 5), "irrigate"),)),
        mission(drone=2, alt=70, targets=(((9, 6), "irrigate"),)),
    ]
    assert traffic.schedule(ms, p, allow_hold=False)["mode"] == "concurrent"
    res = asyncio.run(FlightExecutor(p, link_factory=lambda a: Freezing(a), payload=NullPayload()).fly(ms))
    assert all(r.status == "completed" for r in res)
    last_connect = max(i for i, e in enumerate(events) if e[0] == "connect_done")
    first_arm = min(i for i, e in enumerate(events) if e[0] == "armed")
    assert last_connect < first_arm


def test_a_drone_refused_in_phase_one_does_not_stop_the_others():
    class Picky(FakeLink):
        async def ready(self):
            return not self.addr.endswith("14541")

    from agridrone import traffic

    ms = [mission(drone=0, alt=30), mission(drone=1, alt=50, targets=(((7, 5), "irrigate"),))]
    traffic.schedule(ms, pol(), allow_hold=False)
    res = asyncio.run(FlightExecutor(pol(), link_factory=lambda a: Picky(a), payload=NullPayload()).fly(ms))
    by = {r.drone: r for r in res}
    assert by[1].status == "refused" and by[0].status == "completed"
