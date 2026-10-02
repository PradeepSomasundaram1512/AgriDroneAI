"""Satellite imagery ingestion: real Sentinel-2 L2A (10 m, ~5-day revisit, free, no API key) for the field.

Pipeline
  search   STAC catalogue (Element 84 Earth Search) for scenes over the field bbox
  read     windowed HTTP range reads of Cloud-Optimized GeoTIFFs (a ~1 km field is a few hundred KB, never the whole tile)
  mask     Scene Classification Layer: clouds, shadows, cirrus, snow, saturated and no-data pixels are removed BEFORE averaging
  index    NDVI (vigor) and NDMI (canopy water content), aggregated to the field's patch grid (grid[x][y], y = north)
  analyse  vigor zones (robust z-scores), change vs the previous scene, ranked scouting targets, time series

Reflectance honours each item's own metadata: Earth Search flags `earthsearch:boa_offset_applied`; applying the -0.1
offset on top of values that already include it produces impossible NDVI (found by testing on real tiles).

Optional dependency: `pip install "agridrone[imagery]"` (rasterio + numpy). The core stays dependency-free and
every failure degrades to "no imagery" instead of stopping the autopilot."""

import json
import logging
import math
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .store import load_json, save_json

log = logging.getLogger("agridrone.imagery")
STAC_URL = "https://earth-search.aws.element84.com/v1/search"
VALID_SCL = {4, 5, 6, 7}  # vegetation, bare soil, water, unclassified
FINE = 3  # sub-pixels per patch side: indices are computed per pixel, masked, THEN averaged per patch
MIN_VALID_FRAC = 0.35  # a patch needs this fraction of clear pixels to get a value
KEEP_SCENES = 12


class ImageryUnavailable(RuntimeError):
    """Optional deps missing, no network, or no usable scene. Callers treat this as 'no imagery today'."""


@dataclass
class Scene:
    id: str
    date: str
    cloud_scene: float  # scene-level cloud cover % from the catalogue
    valid_frac: float  # fraction of THIS field's patches with clear data
    size: int
    ndvi: list  # [x][y] floats or None, y = north
    ndmi: list
    source: str = "sentinel-2-l2a"
    bbox: list = field(default_factory=list)

    def cells(self, which="ndvi"):
        g = getattr(self, which)
        return [(x, y, g[x][y]) for x in range(self.size) for y in range(self.size) if g[x][y] is not None]


# ---------------------------------------------------------------------------------------------- geometry
def field_bbox(location, size, cell_m):
    """(west, south, east, north) in lon/lat of the field whose SW corner is `location` (x east, y north)."""
    lat, lon = float(location["lat"]), float(location["lon"])
    dlat = size * cell_m / 111_320.0
    dlon = size * cell_m / (111_320.0 * math.cos(math.radians(lat)))
    return (lon, lat, lon + dlon, lat + dlat)


# ---------------------------------------------------------------------------------------------- search
def search(bbox, start: date, end: date, max_cloud=60, limit=12, timeout=30, opener=urllib.request.urlopen):
    """STAC search -> items (newest first). Raises ImageryUnavailable on any network/catalogue problem."""
    body = {
        "collections": ["sentinel-2-l2a"],
        "bbox": list(bbox),
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "limit": limit,
        "query": {"eo:cloud_cover": {"lt": max_cloud}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    req = urllib.request.Request(STAC_URL, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})  # noqa: S310
    last = None
    for attempt in range(3):
        try:
            with opener(req, timeout=timeout) as r:  # nosec B310 - fixed https URL
                return json.load(r).get("features", [])
        except Exception as e:  # network, HTTP, JSON: retry, then degrade
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise ImageryUnavailable(f"STAC search failed: {last!r}")


def _contains(item_bbox, bbox):
    return item_bbox[0] <= bbox[0] and item_bbox[1] <= bbox[1] and item_bbox[2] >= bbox[2] and item_bbox[3] >= bbox[3]


# ---------------------------------------------------------------------------------------------- reading
def _deps():
    try:
        import numpy as np
        import rasterio
    except ImportError as e:
        raise ImageryUnavailable('imagery needs the optional extra: pip install "agridrone[imagery]"') from e
    return np, rasterio


def _radiometry(item, asset):
    """(scale, offset) to turn DN into reflectance, honouring the item's own flags."""
    a = item["assets"][asset]
    rb = (a.get("raster:bands") or [{}])[0]
    scale = rb.get("scale", 0.0001)
    if item["properties"].get("earthsearch:boa_offset_applied"):
        return scale, 0.0  # the -0.1 offset is already baked into the DN: applying it again breaks every index
    return scale, rb.get("offset", -0.1 if float(item["properties"].get("s2:processing_baseline", "0") or 0) >= 4.0 else 0.0)


def _read(item, asset, bbox, shape, categorical=False):
    np, rasterio = _deps()
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    href = item["assets"][asset]["href"]
    env = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff",
        "GDAL_HTTP_TIMEOUT": "30",
        "GDAL_HTTP_MAX_RETRY": "3",
    }
    with rasterio.Env(**env), rasterio.open(href) as ds:
        b = transform_bounds("EPSG:4326", ds.crs, *bbox)
        win = from_bounds(*b, transform=ds.transform)
        res = Resampling.nearest if categorical else Resampling.average
        return ds.read(1, window=win, out_shape=shape, resampling=res, boundless=False).astype("float64"), ds.nodata


def read_scene(item, bbox, size, include_ndmi=True) -> Scene:
    """Read one STAC item over the field into a Scene on the size x size patch grid."""
    np, _ = _deps()
    n = size * FINE
    red, nd = _read(item, "red", bbox, (n, n))
    nir, _ = _read(item, "nir", bbox, (n, n))
    scl, _ = _read(item, "scl", bbox, (n, n), categorical=True)
    sr, so = _radiometry(item, "red")
    nr, no = _radiometry(item, "nir")
    refl_r, refl_n = red * sr + so, nir * nr + no
    ok = np.isin(scl.astype(int), list(VALID_SCL)) & (red != (nd or 0)) & (nir != (nd or 0)) & (refl_r + refl_n > 1e-6)
    ndvi = np.where(ok, (refl_n - refl_r) / np.where(ok, refl_n + refl_r, 1.0), np.nan)
    ndmi = np.full_like(ndvi, np.nan)
    if include_ndmi and "swir16" in item["assets"]:
        sw, _ = _read(item, "swir16", bbox, (n, n))
        ss, so2 = _radiometry(item, "swir16")
        refl_s = sw * ss + so2
        ok2 = ok & (refl_n + refl_s > 1e-6)
        ndmi = np.where(ok2, (refl_n - refl_s) / np.where(ok2, refl_n + refl_s, 1.0), np.nan)

    def to_grid(a):  # rows are north->south; grid is [x][y] with y north. Average CLEAR pixels only.
        g = [[None] * size for _ in range(size)]
        for x in range(size):
            for y in range(size):
                r0, c0 = (size - 1 - y) * FINE, x * FINE
                blk = a[r0 : r0 + FINE, c0 : c0 + FINE]
                good = blk[np.isfinite(blk)]
                if good.size >= max(1, MIN_VALID_FRAC * FINE * FINE):
                    g[x][y] = round(float(np.clip(good.mean(), -1, 1)), 4)
        return g

    gn, gm = to_grid(ndvi), to_grid(ndmi)
    valid = sum(1 for x in range(size) for y in range(size) if gn[x][y] is not None) / (size * size)
    p = item["properties"]
    return Scene(
        id=item["id"],
        date=p["datetime"][:10],
        cloud_scene=round(float(p.get("eo:cloud_cover", -1)), 1),
        valid_frac=round(valid, 3),
        size=size,
        ndvi=gn,
        ndmi=gm,
        bbox=[round(v, 6) for v in bbox],
    )


# ---------------------------------------------------------------------------------------------- cache + fetch
def load_scenes(state_dir=None):
    return [Scene(**s) for s in load_json("imagery.json", [], state_dir)]


def save_scenes(scenes, state_dir=None):
    scenes = sorted({s.id: s for s in scenes}.values(), key=lambda s: s.date)[-KEEP_SCENES:]
    save_json("imagery.json", [asdict(s) for s in scenes], state_dir)
    return scenes


def is_stale(scenes, today: date, max_age_days=5):
    return not scenes or (today - date.fromisoformat(scenes[-1].date)).days > max_age_days


def fetch(policy, state_dir=None, today: date = None, days=60, max_new=4, min_valid=0.6, search_fn=search, read_fn=read_scene):
    """Search + read the newest clear scenes not yet cached. Returns (all cached scenes, [new scenes]). Never half-writes the cache."""
    loc = policy["field"].get("location")
    if not loc:
        raise ImageryUnavailable("policy.field.location (lat/lon of the field's SW corner) is not set")
    size, cell_m = policy["field"]["size"], policy["field"]["cell_m"]
    bbox, today = field_bbox(loc, size, cell_m), today or date.today()
    have = load_scenes(state_dir)
    known = {s.id for s in have}
    items = [
        i for i in search_fn(bbox, today - timedelta(days=days), today) if i["id"] not in known and _contains(i.get("bbox", bbox), bbox)
    ]
    new = []
    for it in items:
        if len(new) >= max_new:
            break
        try:
            sc = read_fn(it, bbox, size)
        except ImageryUnavailable:
            raise
        except Exception as e:  # one unreadable scene must not lose the rest
            log.warning("skipping unreadable scene %s: %r", it["id"], e)
            continue
        if sc.valid_frac >= min_valid:  # cloud over THIS field matters, not the tile-level percentage
            new.append(sc)
    return (save_scenes(have + new, state_dir) if new else have), new


# ---------------------------------------------------------------------------------------------- analysis
def _median(v):
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def robust_z(grid, size):
    """Robust z-score per patch: (value - median) / (1.4826 * MAD), so a few extreme patches do not hide the rest."""
    vals = [grid[x][y] for x in range(size) for y in range(size) if grid[x][y] is not None]
    if len(vals) < 8:
        return [[None] * size for _ in range(size)]
    med = _median(vals)
    mad = _median([abs(v - med) for v in vals]) * 1.4826 or 1e-6
    return [[None if grid[x][y] is None else (grid[x][y] - med) / mad for y in range(size)] for x in range(size)]


def _components(cells, size):
    left, out = set(cells), []
    while left:
        stack, comp = [left.pop()], []
        comp.extend(stack)
        while stack:
            x, y = stack.pop()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (x + dx, y + dy)
                if nb in left:
                    left.remove(nb)
                    stack.append(nb)
                    comp.append(nb)
        out.append(comp)
    return out


def analyze(scenes, z_low=-1.5, drop=0.10, min_zone=3):
    """-> dict: field stats, low-vigor zones, declining patches vs the previous scene, ranked scouting targets, time series."""
    if not scenes:
        return {"scenes": 0}
    cur, n = scenes[-1], scenes[-1].size
    zn, zm = robust_z(cur.ndvi, n), robust_z(cur.ndmi, n)
    low = [(x, y) for x in range(n) for y in range(n) if zn[x][y] is not None and zn[x][y] < z_low]
    zones = [c for c in _components(low, n) if len(c) >= min_zone]
    decl = []
    if len(scenes) >= 2:
        prev = scenes[-2]
        for x in range(n):
            for y in range(n):
                a, b = cur.ndvi[x][y], prev.ndvi[x][y]
                if a is not None and b is not None and b - a > drop:
                    decl.append((x, y, round(b - a, 3)))
    drop_map = {(x, y): d for x, y, d in decl}
    scored = []
    for x in range(n):
        for y in range(n):
            if zn[x][y] is None:
                continue
            s = (
                max(0.0, -zn[x][y])
                + (2 * drop_map.get((x, y), 0) / drop if (x, y) in drop_map else 0)
                + (max(0.0, -zm[x][y]) * 0.5 if zm[x][y] is not None else 0)
            )
            if s > 1.5:
                scored.append((round(s, 2), x, y))
    scored.sort(reverse=True)
    vals = [v for _, _, v in cur.cells("ndvi")]
    return {
        "scenes": len(scenes),
        "latest": {"id": cur.id, "date": cur.date, "cloud_scene": cur.cloud_scene, "valid_frac": cur.valid_frac},
        "ndvi": {
            "mean": round(sum(vals) / len(vals), 3),
            "median": round(_median(vals), 3),
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
        }
        if vals
        else None,
        "zones": [
            {"cells": len(c), "centre": [round(sum(p[0] for p in c) / len(c)), round(sum(p[1] for p in c) / len(c))]}
            for c in sorted(zones, key=len, reverse=True)
        ],
        "declining": len(decl),
        "scouting": [{"cell": [x, y], "score": s} for s, x, y in scored[:10]],
        "series": [
            {"date": s.date, "ndvi": round(sum(v for _, _, v in s.cells()) / max(1, len(s.cells())), 3), "valid": s.valid_frac}
            for s in scenes
        ],
    }


# ---------------------------------------------------------------------------------------------- uses of real imagery
def init_field(farm, scene):
    """Seed the digital twin with this REAL field's spatial variability: low-vigor patches get shallow, fast-draining soil
    (soil factor up) and start greener/browner accordingly. Only the spatial pattern is borrowed, not absolute values."""
    z = robust_z(scene.ndvi, farm.size)
    for (x, y), c in farm.cells.items():
        if z[x][y] is not None:
            zz = max(-3.0, min(3.0, z[x][y]))
            c.soil = round(max(0.75, min(1.3, 1.0 - 0.09 * zz)), 4)  # vigor z -3 -> sandy 1.27, +3 -> clay 0.75
    return farm


def crosscheck(sensor_ndvi, scene, tol=0.25):
    """Independent reference for ground sensors: sensor NDVI far from the satellite's for the same patch is suspicious.
    Satellite NDVI is a ~10 m average and may be days old, so this flags only gross disagreement. -> sorted [(cell, diff)]."""
    out = []
    for (x, y), v in sensor_ndvi.items():
        s = scene.ndvi[x][y] if 0 <= x < scene.size and 0 <= y < scene.size else None
        if s is not None and v is not None and abs(v - s) > tol:
            out.append(((x, y), round(v - s, 3)))
    return sorted(out, key=lambda t: -abs(t[1]))


def refresh(policy, state_dir=None, today: date = None, **kw):
    """What the autopilot calls: refresh only when the cache is stale; never raises. -> status dict for the audit log."""
    today = today or date.today()
    cfg = policy.get("imagery", {})
    if not cfg.get("enabled", False):
        return {"status": "disabled"}
    have = load_scenes(state_dir)
    if not is_stale(have, today, cfg.get("max_age_days", 5)):
        return {"status": "fresh", "latest": have[-1].date}
    try:
        scenes, new = fetch(policy, state_dir, today, days=cfg.get("lookback_days", 60), **kw)
        return {
            "status": "updated" if new else "no_new_clear_scene",
            "new": [s.id for s in new],
            "latest": scenes[-1].date if scenes else None,
        }
    except ImageryUnavailable as e:
        return {"status": "unavailable", "reason": str(e)[:160]}
    except Exception as e:  # satellite trouble must never stop the farm
        return {"status": "error", "reason": repr(e)[:160]}


def render_ascii(scene, which="ndvi"):  # debugging aid for terminals
    chars = " .:-=+*#%@"
    g = getattr(scene, which)
    rows = []
    for y in range(scene.size - 1, -1, -1):
        rows.append("".join("?" if g[x][y] is None else chars[min(9, max(0, int((g[x][y] + 0.1) / 1.0 * 9)))] for x in range(scene.size)))
    return "\n".join(rows)


__all__ = [
    "Scene",
    "ImageryUnavailable",
    "field_bbox",
    "search",
    "read_scene",
    "fetch",
    "analyze",
    "init_field",
    "crosscheck",
    "refresh",
    "load_scenes",
]
