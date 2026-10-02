"""Deterministic farm simulator: stands in for drones + ground sensors until real
hardware adapters are attached (see adapters.py). Produces NDVI, soil moisture and
pest-pressure per grid cell, evolving day by day with weather and applied treatments."""
import math
import random
from dataclasses import dataclass, field


@dataclass
class Cell:
    ndvi: float
    moisture: float
    pest: float
    yield_potential: float = 1.0


@dataclass
class Farm:
    size: int
    seed: int
    day: int = 0
    cells: dict = field(default_factory=dict)
    water_used: float = 0.0
    chem_used: float = 0.0
    rng: random.Random = None

    @classmethod
    def create(cls, size=24, seed=7):
        f = cls(size=size, seed=seed, rng=random.Random(seed))
        for x in range(size):
            for y in range(size):
                f.cells[(x, y)] = Cell(
                    ndvi=0.7 + f.rng.uniform(-0.05, 0.05),
                    moisture=0.4 + f.rng.uniform(-0.05, 0.05),
                    pest=max(0.0, f.rng.gauss(0.1, 0.05)),
                )
        return f

    def step(self, rain=None):
        """Advance one day. Moisture drains, pests spread, stress cuts yield potential."""
        self.day += 1
        rain = rain if rain is not None else max(0.0, self.rng.gauss(0.02, 0.05))
        hot_spot = (self.day * 3 % self.size, (self.day * 5) % self.size)
        for (x, y), c in self.cells.items():
            c.moisture = min(0.6, max(0.0, c.moisture - 0.03 + rain + self.rng.gauss(0, 0.005)))
            near = math.hypot(x - hot_spot[0], y - hot_spot[1]) < 3
            c.pest = min(1.0, max(0.0, c.pest * 1.03 + (0.04 if near else 0) + self.rng.gauss(0, 0.005)))
            stress = max(0.0, 0.3 - c.moisture) * 1.5 + max(0.0, c.pest - 0.4)
            c.ndvi = min(0.9, max(0.1, c.ndvi + 0.01 - stress * 0.12))
            c.yield_potential = max(0.0, c.yield_potential - stress * 0.01)

    def observe(self, cell, noise=0.02):
        c = self.cells[cell]
        n = lambda v: v + self.rng.gauss(0, noise)
        return {"cell": cell, "day": self.day, "ndvi": n(c.ndvi), "moisture": n(c.moisture), "pest": n(c.pest)}

    def apply(self, cell, action):
        c = self.cells[cell]
        if action == "irrigate":
            c.moisture = min(0.6, c.moisture + 0.25)
            self.water_used += 1.0
        elif action == "spray":
            c.pest *= 0.2
            self.chem_used += 1.0

    def blanket_usage(self):
        """Baseline: uniform irrigation/spray of the whole field weekly (charitable strawman; real baselines vary)."""
        n = len(self.cells)
        return (n / 7 * self.day, n / 7 * self.day)

    def mean_yield(self):
        return sum(c.yield_potential for c in self.cells.values()) / len(self.cells)
