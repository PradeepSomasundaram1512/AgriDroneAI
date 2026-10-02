"""Wind: what it does to a drone, and the go/no-go rules built on it.

Effects modelled (all with one shared implementation, so the planner, the safety gate, the traffic checker and the
ground station can never disagree about what a leg costs):
  * ENERGY and TIME of every leg. A drone flies at a fixed airspeed `a`; its ground speed along a track is
        gs = sqrt(a^2 - crosswind^2) + tailwind          (the wind triangle: it crabs into the crosswind)
    so a headwind slows it, a tailwind speeds it, and energy (constant power x time) scales by a / gs. A leg the drone
    cannot make headway on (crosswind >= airspeed, or gs < 25% of airspeed) is INFEASIBLE.
  * WIND SHEAR: forecasts are for 10 m; wind at flight altitude follows a power law, so a drone on the 70 m layer
    faces ~47% more wind than one on the 30 m layer. High layers become infeasible first in a blow.
  * GO / NO-GO: a flight limit (sustained + gust) and a tighter SPRAY limit (drift: spraying in strong wind puts
    chemical on the wrong field).
  * SEPARATION MARGIN: gusts and GPS error enlarge the horizontal clearance the collision checker must demand.
Direction convention: meteorological ("wind FROM `from_deg`" clockwise from north). x = east, y = north."""

import math
from dataclasses import dataclass

DEFAULT_LIMITS = {
    "enabled": True,  # False = ignore wind entirely (calm-air planning), for controlled comparisons only
    "max_flight_ms": 6.0,  # sustained wind at 10 m above which nothing flies
    "max_gust_ms": 10.0,
    "max_spray_ms": 4.5,  # drift limit for spraying (irrigation is allowed up to the flight limit)
    "shear_exponent": 0.2,
    "gust_margin_m_per_ms": 0.5,  # extra horizontal clearance per m/s of gust
    "gps_error_m": 3.0,  # standard GNSS position error budget (RTK would be ~0.1)
    "flight_window_factor": 0.75,  # flights are scheduled in the morning lull: ~75% of the forecast daily maximum
}
MIN_HEADWAY = 0.25  # a leg is infeasible if ground speed would drop below this fraction of airspeed
DESIGN_GUST_SHARE = 0.5  # planning wind = sustained + this share of the (gust - sustained) gap


@dataclass(frozen=True)
class Wind:
    speed: float = 0.0  # m/s sustained at 10 m
    gust: float = 0.0  # m/s
    from_deg: float = 270.0

    def at(self, alt_m: float, shear: float = 0.2) -> float:
        """Sustained speed at altitude (power law above the 10 m reference)."""
        return self.speed * (max(alt_m, 10.0) / 10.0) ** shear

    def vector(self, alt_m: float = 10.0, shear: float = 0.2):
        """(vx, vy) in m/s: the direction the air is MOVING (east, north)."""
        s, th = self.at(alt_m, shear), math.radians(self.from_deg)
        return (-s * math.sin(th), -s * math.cos(th))

    def design(self):
        """What to plan energy against: sustained plus part of the gust margin (forecast wind is never exact)."""
        return Wind(self.speed + DESIGN_GUST_SHARE * max(0.0, self.gust - self.speed), self.gust, self.from_deg)

    def as_tuple(self):
        return (round(self.speed, 2), round(self.gust, 2), round(self.from_deg, 1))

    @classmethod
    def from_tuple(cls, t):
        return cls(*t) if t else cls()


CALM = Wind()


def limits(policy):
    return {**DEFAULT_LIMITS, **policy.get("wind", {})}


def ground_speed(dx, dy, airspeed, wvec):
    """Ground speed (m/s) along track (dx, dy), or None if the drone cannot make headway. Wind triangle."""
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return airspeed
    tx, ty = dx / n, dy / n
    along = wvec[0] * tx + wvec[1] * ty  # + = tailwind
    cross = abs(wvec[0] * ty - wvec[1] * tx)
    if cross >= airspeed:
        return None
    gs = math.sqrt(airspeed**2 - cross**2) + along
    return gs if gs >= MIN_HEADWAY * airspeed else None


def leg_factor(dx_cells, dy_cells, airspeed, wvec):
    """Energy/time multiplier for a leg versus calm air (1.0 = calm, >1 headwind, <1 tailwind); None if infeasible."""
    if wvec == (0.0, 0.0) or (dx_cells == 0 and dy_cells == 0):
        return 1.0
    gs = ground_speed(dx_cells, dy_cells, airspeed, wvec)
    return None if gs is None else airspeed / gs


def go_no_go(wind: Wind, policy):
    """-> (flight_ok, spray_ok, reason). Applies to the forecast/measured wind at the 10 m reference height."""
    lim = limits(policy)
    if wind.speed > lim["max_flight_ms"] or wind.gust > lim["max_gust_ms"]:
        return (
            False,
            False,
            f"wind {wind.speed:.1f} m/s (gusts {wind.gust:.1f}) exceeds the flight limit {lim['max_flight_ms']}/{lim['max_gust_ms']} m/s",
        )
    spray_ok = wind.speed <= lim["max_spray_ms"]
    return True, spray_ok, "" if spray_ok else f"wind {wind.speed:.1f} m/s exceeds the spray drift limit {lim['max_spray_ms']} m/s"


def clearance_margin(wind: Wind, policy):
    """Extra horizontal clearance (m) the traffic checker must add: GNSS error + gust-driven position error."""
    lim = limits(policy)
    return lim["gps_error_m"] + lim["gust_margin_m_per_ms"] * wind.gust


def spray_efficacy(wind_ms: float) -> float:
    """Fraction of the spray that lands on target: drift loss grows with wind (1.0 at <= 2 m/s)."""
    return max(0.3, 1.0 - 0.08 * max(0.0, wind_ms - 2.0))
