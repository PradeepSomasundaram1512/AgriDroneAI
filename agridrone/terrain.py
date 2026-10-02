"""Terrain and obstacles: the field is not a billiard table.

  * ELEVATION. Drones hold a constant height ABOVE GROUND, so a route over hills costs extra energy for every metre it must climb
    (descents are free: no regeneration is assumed). Elevation comes from policy.terrain: a smooth synthetic landscape (`relief_m`, `seed`)
    or an explicit `elevation` grid (rows of metres, e.g. sampled from a real DEM) - the planner and the safety gate call the SAME function,
    so they cannot disagree about what a hill costs.
  * OBSTACLES. policy.terrain.obstacles = [[x, y, height_m], ...] (a tree line, a pole, a power line). A drone may overfly one only if its
    cruise height clears it by `clearance_m`; otherwise the cell is a no-fly cell FOR THAT DRONE's altitude layer (the high layer can
    cross what the low layer must go around).
Not modelled: climb time (traffic timing ignores it), slope-dependent wind, spray on slopes."""

import math
import random

DEFAULTS = {"relief_m": 0.0, "seed": 3, "wh_per_m_climb": 0.03, "clearance_m": 10.0, "obstacles": [], "elevation": None}
STEP = 0.5  # cells between samples along a leg


def cfg(policy):
    return {**DEFAULTS, **policy.get("terrain", {})}


class Terrain:
    def __init__(self, c, size):
        self.c, self.size = c, size
        r = random.Random(c["seed"])
        self.waves = [
            (r.uniform(0.15, 0.45), r.uniform(0, 6.3), r.uniform(0.15, 0.45), r.uniform(0, 6.3), r.uniform(0.4, 1.0)) for _ in range(3)
        ]
        tot = sum(w[4] for w in self.waves)
        self.waves = [(a, b, c2, d, e / tot) for a, b, c2, d, e in self.waves]
        self.flat = not c["relief_m"] and not c["elevation"]

    def z(self, x, y):
        """Ground elevation (m) at a (fractional) cell position."""
        g = self.c["elevation"]
        if g:  # bilinear on the supplied grid [x][y]
            n = len(g)
            fx, fy = min(max(x, 0.0), n - 1.001), min(max(y, 0.0), len(g[0]) - 1.001)
            i, j = int(fx), int(fy)
            tx, ty = fx - i, fy - j
            return (g[i][j] * (1 - tx) + g[i + 1][j] * tx) * (1 - ty) + (g[i][j + 1] * (1 - tx) + g[i + 1][j + 1] * tx) * ty
        return self.c["relief_m"] * sum(w * (0.5 + 0.5 * math.sin(a * x + b) * math.sin(c * y + d)) for a, b, c, d, w in self.waves)

    def climb_m(self, a, b):
        """Metres a terrain-following drone must climb flying straight a -> b (sum of the positive steps along the way)."""
        if self.flat:
            return 0.0
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / STEP))
        up, prev = 0.0, self.z(*a)
        for i in range(1, n + 1):
            z = self.z(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
            up += max(0.0, z - prev)
            prev = z
        return up

    def climb_wh(self, a, b):
        return 0.0 if self.flat else self.climb_m(a, b) * self.c["wh_per_m_climb"]


def from_policy(policy):
    return Terrain(cfg(policy), policy["field"]["size"])


def blocked(policy, altitude_m):
    """Cells this altitude layer must not enter: the no-fly cells plus every obstacle that is not safely below the cruise height."""
    out = {tuple(c) for c in policy["no_fly_cells"]}
    c = cfg(policy)
    for x, y, h in c["obstacles"]:
        if altitude_m - h < c["clearance_m"]:
            out.add((int(x), int(y)))
    return out


def profile(policy, a, b, n=20):
    """[(fraction, ground_m)] along a leg - for the dashboard and for terrain-clearance reports."""
    t = from_policy(policy)
    return [(i / n, t.z(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)) for i in range(n + 1)]
