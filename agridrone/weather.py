"""Weather for the digital twin: real data from Open-Meteo (free, no API key) with a cached, deterministic
synthetic fallback so the autopilot never stops because a network call failed.

Each day returns today's Weather plus a short forecast, which the planner uses (e.g. skip irrigating when rain is coming)."""

import json
import math
import os
import random
import urllib.request
from dataclasses import dataclass, asdict
from datetime import date, timedelta


@dataclass
class Weather:
    rain_mm: float
    tmax: float
    tmin: float
    et0_mm: float  # reference evapotranspiration (FAO-56)
    source: str = "synthetic"
    wind_ms: float = 0.0  # sustained wind at 10 m
    gust_ms: float = 0.0
    wind_from_deg: float = 270.0  # meteorological: the direction the wind blows FROM

    @property
    def tmean(self):
        return (self.tmax + self.tmin) / 2


def synthetic(day: int, seed: int = 0, start_doy: int = 120) -> Weather:
    """Plausible temperate-climate weather: seasonal temperature, bursty rain, ET0 from temperature (Hargreaves-like).
    Deterministic per (seed, day) so paired experiments see identical weather."""
    r = random.Random(seed * 100003 + day)
    doy = start_doy + day
    season = math.sin((doy - 80) / 365 * 2 * math.pi)  # peaks ~ late June
    tmean = 14 + 11 * season + r.gauss(0, 2.5)
    rng_t = 9 + r.uniform(-2, 3)
    wet = r.random() < (0.30 + 0.06 * (1 - season))
    rain = r.expovariate(1 / 11.0) if wet else 0.0
    tmax, tmin = tmean + rng_t / 2, tmean - rng_t / 2
    et0 = max(0.5, 0.0023 * (tmean + 17.8) * math.sqrt(max(rng_t, 1)) * 14.5 * (0.6 + 0.4 * season) / 2.45 * 2.2)
    # wind comes from its OWN random stream, so adding it did not change a single rain/temperature/ET value
    rw = random.Random(seed * 7919 + day * 31 + 5)
    speed = rw.weibullvariate(4.3, 2.0) * (1.25 if wet else 1.0) * (1.0 + 0.15 * (1 - season))
    gust = speed * (1.35 + 0.35 * rw.random())
    from_deg = (270 + 60 * math.sin(day / 9.0) + rw.gauss(0, 25)) % 360
    return Weather(
        round(rain, 1),
        round(tmax, 1),
        round(tmin, 1),
        round(min(et0, 8.0), 2),
        "synthetic",
        round(speed, 1),
        round(gust, 1),
        round(from_deg) % 360,
    )


def _col(d, key, i, default=0.0):
    v = (d.get(key) or [])[i : i + 1]
    return default if not v or v[0] is None else float(v[0])


def _fetch_open_meteo(lat, lon, day0: date, past_days=2, forecast_days=4, timeout=10):
    lat, lon = float(lat), float(lon)
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=auto&past_days={past_days}&forecast_days={forecast_days}"
        "&wind_speed_unit=ms&daily=precipitation_sum,temperature_2m_max,temperature_2m_min,et0_fao_evapotranspiration,"
        "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant"
    )
    # fixed https URL; lat/lon are coerced to float above, so config values cannot inject anything into it
    with urllib.request.urlopen(url, timeout=timeout) as r:  # nosec B310
        d = json.load(r)["daily"]
    return {
        t: Weather(
            d["precipitation_sum"][i] or 0.0,
            d["temperature_2m_max"][i],
            d["temperature_2m_min"][i],
            d["et0_fao_evapotranspiration"][i] or 0.0,
            "open-meteo",
            _col(d, "wind_speed_10m_max", i),  # the daily MAX is conservative for go/no-go and energy planning
            _col(d, "wind_gusts_10m_max", i),
            _col(d, "wind_direction_10m_dominant", i, 270.0),
        )
        for i, t in enumerate(d["time"])
    }


def get_weather(policy, day: int, state_dir=None, fetch=_fetch_open_meteo, today: date = None):
    """-> (Weather for today, [Weather for next 3 days], source). Never raises."""
    from .store import load_json, save_json  # local import: store -> sim -> weather would otherwise be circular

    w = policy.get("weather", {"source": "synthetic"})
    seed, doy = policy["field"]["seed"], w.get("start_doy", 120)
    if w.get("source") == "open-meteo" and not os.environ.get("AGRIDRONE_OFFLINE"):
        today = today or date.today()
        try:
            table = fetch(w["lat"], w["lon"], today)
            cache = load_json("weather_cache.json", {}, state_dir)
            cache.update({k: asdict(v) for k, v in table.items()})
            save_json("weather_cache.json", dict(sorted(cache.items())[-30:]), state_dir)
            keys = [(today + timedelta(days=i)).isoformat() for i in range(0, 4)]
            if keys[0] in table:
                return table[keys[0]], [table[k] for k in keys[1:] if k in table], "open-meteo"
        except Exception:
            cache = load_json("weather_cache.json", {}, state_dir)  # offline: use the last known real data if recent
            k = today.isoformat()
            if k in cache:
                return Weather(**cache[k]), [], "open-meteo (cached)"
    wx = synthetic(day, seed, doy)
    return wx, [synthetic(day + i, seed, doy) for i in range(1, 4)], "synthetic"
