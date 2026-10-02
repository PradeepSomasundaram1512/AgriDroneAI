"""Farm digital twin: stands in for drones + ground sensors until real hardware adapters are attached.

Physically motivated but deliberately simple:
  * soil water balance in mm (rain - crop evapotranspiration - drainage) over a 300 mm root zone, with spatially
    varying soil (sandy patches dry faster, clay patches hold water)
  * crop greenness (NDVI) follows a growth curve and is pulled down by water/pest stress
  * pests grow with temperature and humidity, spread to neighbours, and arrive from the field edges
  * yield potential is lost in proportion to stress
Weather comes from weather.py (real Open-Meteo data or a synthetic generator). Weather randomness and sensor noise use
SEPARATE random streams, so two strategies on the same seed see identical weather: required for fair benchmarks."""

import math
import random
from dataclasses import dataclass, field

from .weather import Weather, synthetic

ROOT_ZONE_MM = 300.0
IRRIGATION_MM = 36.0
THETA_MAX = 0.55


@dataclass
class Cell:
    ndvi: float
    moisture: float  # volumetric soil water, 0..0.55
    pest: float  # 0..1
    yield_potential: float = 1.0
    soil: float = 1.0  # >1 drains/dries faster (sandy), <1 holds water (clay)
    host: float = 1.0  # pest susceptibility of this patch (microclimate, canopy density)


@dataclass
class Farm:
    size: int
    seed: int
    day: int = 0
    cells: dict = field(default_factory=dict)
    water_used: float = 0.0  # mm applied (summed over patches)
    chem_used: float = 0.0  # patch-sprays
    rng: random.Random = None  # environment stream (pest arrivals)
    obs_rng: random.Random = None  # sensor-noise stream
    start_doy: int = 120
    last_weather: Weather = None
    faults: dict = field(default_factory=dict)  # {cell: {"mode": dead|stuck|bias|spike, "start": day, "val": ...}}
    fault_rng: random.Random = None
    init_scene: str = None  # id of the satellite scene that seeded this twin's spatial variability

    @classmethod
    def create(cls, size=24, seed=7, start_doy=120, fault_rate=0.0):
        f = cls(
            size=size,
            seed=seed,
            rng=random.Random(seed),
            obs_rng=random.Random(seed + 9001),
            start_doy=start_doy,
            fault_rng=random.Random(seed + 31337),
        )
        soil, host = f._smooth_map(4242, 0.75, 1.3), f._smooth_map(777, 0.35, 1.5)
        for x in range(size):
            for y in range(size):
                s = soil[x][y]
                f.cells[(x, y)] = Cell(
                    ndvi=0.30 + f.rng.uniform(-0.02, 0.02),
                    moisture=0.38 + f.rng.uniform(-0.04, 0.04),
                    pest=max(0.0, f.rng.gauss(0.06, 0.03)),
                    soil=s,
                    host=host[x][y],
                )
        fr = random.Random(seed + 555)  # which sensors fail, how and when: identical for every strategy
        for cell in f.cells:
            if fr.random() < fault_rate:
                f.faults[cell] = {
                    "mode": fr.choice(["dead", "stuck", "bias", "spike"]),
                    "start": fr.randint(1, 40),
                    "bias": fr.choice([-0.15, 0.15]),
                    "frozen": None,
                }
        return f

    def _smooth_map(self, salt, lo, hi):
        """Smooth random field in lo..hi: blurred noise, so patches are spatially coherent like real soil/microclimate zones."""
        n, r = self.size, random.Random(self.seed + salt)
        g = [[r.random() for _ in range(n)] for _ in range(n)]
        for _ in range(3):
            g = [[sum(g[(x + dx) % n][(y + dy) % n] for dx in (-1, 0, 1) for dy in (-1, 0, 1)) / 9 for y in range(n)] for x in range(n)]
        lo0, hi0 = min(min(c) for c in g), max(max(c) for c in g)
        return [[lo + (hi - lo) * (v - lo0) / ((hi0 - lo0) or 1) for v in row] for row in g]

    def step(self, weather: Weather = None, rain=None):
        """Advance one day. `rain` (mm) is a convenience override for tests."""
        self.day += 1
        w = weather or synthetic(self.day, self.seed, self.start_doy)
        if rain is not None:
            w = Weather(rain, w.tmax, w.tmin, w.et0_mm, w.source)
        self.last_weather = w
        n = self.size
        growth = 1 / (1 + math.exp(-(self.day - 35) / 12))  # crop growth curve (0..1) over the season
        kc = 0.35 + 0.8 * growth  # crop coefficient rises with canopy
        humid = 1.0 if w.rain_mm > 1 else 0.55
        suit = min(1.0, max(0.0, (w.tmean - 10) / 16)) * (0.5 + 0.5 * humid)  # pest weather suitability
        old = {k: c.pest for k, c in self.cells.items()}
        for (x, y), c in self.cells.items():
            # --- water balance (mm -> volumetric fraction over the root zone)
            fstress = min(1.0, c.moisture / 0.30)  # crops close stomata when dry
            zr = ROOT_ZONE_MM * (1.3 - 0.6 * (c.soil - 0.75) / 0.55)  # sandy (soil high) = shallow effective store, clay = deep
            etc = kc * w.et0_mm * fstress * (0.8 + 0.4 * (c.soil - 0.75) / 0.55)
            drain = max(0.0, c.moisture - 0.42) * (0.25 + 0.35 * c.soil)
            c.moisture = min(THETA_MAX, max(0.02, c.moisture + (0.85 * w.rain_mm - etc) / zr - drain))
            # --- pests: temperature-driven growth + diffusion from neighbours + rare arrivals at the edges
            nb = [old[(x + dx, y + dy)] for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)) if (x + dx, y + dy) in old]
            spread = 0.10 * max(0.0, (sum(nb) / len(nb)) - old[(x, y)])
            arrive = 0.12 if (self.rng.random() < (0.004 if (x in (0, n - 1) or y in (0, n - 1)) else 0.0007) * (0.3 + suit)) else 0.0
            c.pest = min(1.0, max(0.0, old[(x, y)] * (1 + 0.065 * suit * c.host * (1 - old[(x, y)]) - 0.02) + spread + arrive))
            # --- crop: greenness chases a growth curve, pulled down by stress; yield potential erodes with stress
            stress = max(0.0, 0.30 - c.moisture) * 1.6 + max(0.0, c.pest - 0.4) * 0.9
            target = 0.28 + 0.62 * growth
            c.ndvi = min(0.92, max(0.1, c.ndvi + 0.22 * (target - 0.55 * stress - c.ndvi)))
            c.yield_potential = max(0.0, c.yield_potential - stress * 0.012 * (0.6 + 0.8 * growth))

    def observe(self, cell, noise=0.02):
        c = self.cells[cell]

        def n(v):
            return v + self.obs_rng.gauss(0, noise)

        o = {"cell": cell, "day": self.day, "ndvi": n(c.ndvi), "moisture": n(c.moisture), "pest": n(c.pest)}
        f = self.faults.get(cell)
        if f and self.day >= f["start"]:
            mode = f["mode"]
            if mode == "dead":  # sensor reads zero
                o.update(ndvi=0.0, moisture=0.0, pest=0.0)
            elif mode == "stuck":  # freezes at the first faulty reading
                if f["frozen"] is None:
                    f["frozen"] = (o["ndvi"], o["moisture"], o["pest"])
                o["ndvi"], o["moisture"], o["pest"] = f["frozen"]
            elif mode == "bias":  # calibration drift: reads wet (hides drought) or dry (wastes water)
                o["moisture"] += f["bias"]
            elif mode == "spike" and self.fault_rng.random() < 0.3:  # intermittent wild readings
                o["moisture"] += self.fault_rng.choice([-0.3, 0.3])
                o["pest"] = min(1.0, max(0.0, o["pest"] + self.fault_rng.choice([-0.4, 0.4])))
        return o

    def observe_all(self):
        return [self.observe(c) for c in self.cells]

    def apply(self, cell, action):
        c = self.cells[cell]
        if action == "irrigate":
            zr = ROOT_ZONE_MM * (1.3 - 0.6 * (c.soil - 0.75) / 0.55)
            c.moisture = min(THETA_MAX, c.moisture + IRRIGATION_MM / zr)
            self.water_used += IRRIGATION_MM
        elif action == "spray":
            c.pest *= 0.2
            self.chem_used += 1.0

    def blanket_usage(self):
        """Reference schedule: irrigate every patch weekly and spray every patch fortnightly (a common calendar practice)."""
        n = len(self.cells)
        return (n / 7 * self.day * IRRIGATION_MM, n / 14 * self.day)

    def mean_yield(self):
        return sum(c.yield_potential for c in self.cells.values()) / len(self.cells)
