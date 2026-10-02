import asyncio

import pytest
from test_hardware import FakeLink, mission, pol

from agridrone import traffic
from agridrone import wind as W
from agridrone.hardware import FlightExecutor, NullPayload, PreflightError


@pytest.fixture(autouse=True)
def armed(monkeypatch):
    monkeypatch.setenv("AGRIDRONE_ARMED", "1")


class GpsLink(FakeLink):
    """FakeLink with a scriptable GPS: sats_at(t) gives the satellite count t seconds after the mission starts."""

    script = staticmethod(lambda t: 14)
    pace = 0.25  # seconds per waypoint (a real flight takes minutes; tests need the guard to have time to act)

    def __init__(self, addr, **kw):
        super().__init__(addr, **kw)
        self.t0, self.calls = None, []
        GpsLink.instances.append(self)

    async def gps_state(self):
        import time

        self.t0 = self.t0 or time.monotonic()
        return (3, self.script(time.monotonic() - self.t0))

    async def hold(self):
        self.calls.append("hold")

    async def land(self):
        self.calls.append("land")

    async def resume(self):
        self.calls.append("resume")

    async def progress(self):
        n = len(self.uploaded)
        for i in range(1, n + 1):
            await asyncio.sleep(self.pace)
            self.bat -= self.drain
            yield i, n

    async def rtl(self):
        self.calls.append("rtl")
        self.rtl_called = True


def policy(**gps_kw):
    p = pol()
    p["gps"] = {"grace_s": 0.6, "poll_s": 0.05, **gps_kw}
    p["hardware"]["telemetry_timeout_s"] = 2
    return p


def run(p, missions, script=None, **links):
    GpsLink.instances.clear()
    cls = type("L", (GpsLink,), {"script": staticmethod(script)} if script else {})
    pay = NullPayload()
    ex = FlightExecutor(p, link_factory=lambda a: cls(a, **links), payload=pay)
    return asyncio.run(ex.fly(missions)), pay, ex


def test_poor_gps_before_takeoff_means_the_drone_does_not_fly():
    res, pay, _ = run(policy(), [mission()], script=lambda t: 6)  # 6 satellites: below the 10 needed
    assert res[0].status == "refused" and "GPS" in res[0].reason and pay.events == []
    assert not GpsLink.instances[0].started


def test_a_short_gps_dropout_holds_then_resumes_and_skips_the_blind_treatment():
    flaky = lambda t: 4 if 0.18 < t < 0.4 else 14  # a dip shorter than the grace period  # noqa: E731
    res, pay, _ = run(policy(grace_s=2.0), [mission(targets=(((5, 5), "irrigate"), ((6, 5), "spray"), ((7, 5), "irrigate")))], script=flaky)
    link = GpsLink.instances[0]
    assert "hold" in link.calls and "resume" in link.calls and "land" not in link.calls
    assert res[0].status == "completed"
    assert len(pay.events) < 3  # a waypoint reached during the dip was NOT treated blind


def test_permanent_gps_loss_lands_in_place_and_reports_it():
    res, pay, ex = run(
        policy(),
        [mission(targets=(((5, 5), "irrigate"), ((6, 5), "spray"), ((7, 5), "irrigate"), ((8, 5), "spray")))],
        script=lambda t: 14 if t < 0.3 else 3,
    )
    link = GpsLink.instances[0]
    assert res[0].status == "gps_lost" and "landed in place" in res[0].reason and "recovery" in res[0].reason
    assert link.calls[:2] == ["hold", "land"] and "rtl" not in link.calls  # no return-to-launch: it cannot navigate home
    assert res[0].completed < 4 and len(pay.events) <= res[0].completed
    assert 0 in ex._emergency


def test_a_failing_high_drone_orders_the_drone_below_it_home_but_not_the_one_above():
    p = policy()
    low, mid, high = mission(drone=0, alt=30), mission(drone=1, alt=50), mission(drone=2, alt=70)
    assert traffic.schedule([low, mid, high], p, allow_hold=False)["mode"] == "concurrent"  # a real plan: delays from the scheduler

    class PerDrone(GpsLink):
        pace = 1.0

        async def gps_state(self):
            import time

            self.t0 = self.t0 or time.monotonic()
            t = time.monotonic() - self.t0
            return (3, 3 if (self.addr == p["hardware"]["links"][1] and t > 0.3) else 14)  # only the 50 m drone loses GPS

    GpsLink.instances.clear()
    ex = FlightExecutor(p, link_factory=lambda a: PerDrone(a), payload=NullPayload())
    res = {r.drone: r for r in asyncio.run(ex.fly([low, mid, high]))}
    assert res[1].status == "gps_lost"
    assert res[0].status == "aborted" and "peer GPS emergency" in res[0].reason  # 30 m: below the failing drone, ordered home
    assert res[2].status == "completed"  # 70 m: above it, unaffected


def test_if_hold_is_impossible_without_gps_it_still_lands():
    class NoHold(GpsLink):
        pace = 1.0

        async def hold(self):
            raise RuntimeError("hold needs a position estimate")

    GpsLink.instances.clear()
    p = policy()
    ex = FlightExecutor(
        p, link_factory=lambda a: type("N", (NoHold,), {"script": staticmethod(lambda t: 14 if t < 0.2 else 2)})(a), payload=NullPayload()
    )
    res = asyncio.run(ex.fly([mission()]))
    assert res[0].status == "gps_lost" and "land" in GpsLink.instances[0].calls


def test_healthy_gps_never_triggers_the_protocol():
    res, pay, ex = run(policy(), [mission()], script=lambda t: 14)
    link = GpsLink.instances[0]
    assert (
        res[0].status == "completed" and not set(link.calls) & {"hold", "land", "resume"} and ex._emergency == {} and len(pay.events) == 2
    )


def test_a_broken_gps_watcher_does_not_take_the_flight_down():
    class Broken(GpsLink):
        async def gps_state(self):
            if self.t0 is None:
                self.t0 = 1
                return (3, 14)  # preflight passes
            raise OSError("telemetry glitch")

    GpsLink.instances.clear()
    ex = FlightExecutor(policy(), link_factory=lambda a: Broken(a), payload=NullPayload())
    assert asyncio.run(ex.fly([mission()]))[0].status == "completed"


def test_measured_wind_above_the_limit_refuses_to_fly():
    p = policy()
    GpsLink.instances.clear()
    calm = FlightExecutor(p, link_factory=lambda a: GpsLink(a), payload=NullPayload(), wind_provider=lambda: W.Wind(2, 3, 0))
    assert asyncio.run(calm.fly([mission()]))[0].status == "completed"
    gale = FlightExecutor(p, link_factory=lambda a: GpsLink(a), payload=NullPayload(), wind_provider=lambda: W.Wind(9, 12, 0))
    with pytest.raises(PreflightError, match="measured wind"):
        asyncio.run(gale.fly([mission()]))
    none = FlightExecutor(p, link_factory=lambda a: GpsLink(a), payload=NullPayload(), wind_provider=lambda: None)
    assert asyncio.run(none.fly([mission()]))[0].status == "completed"


def test_a_plan_made_for_too_much_wind_is_refused_on_the_ground_station():
    p = policy()
    m = mission()
    m.wind = (9.0, 12.0, 270.0)
    m.energy_wh = 20.0
    with pytest.raises(PreflightError, match="flight limit"):
        asyncio.run(FlightExecutor(p, link_factory=lambda a: GpsLink(a)).fly([m]))
