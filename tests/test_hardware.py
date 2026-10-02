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

    async def ensure_failsafe(self):
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
    p["hardware"].update(enabled=True, **hw)
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
    r = asyncio.run(ex(fail="boom")[0].fly([mission()]))[0]
    assert r.status == "aborted" and FakeLink.instances[0].rtl_called
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
