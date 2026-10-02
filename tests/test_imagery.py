import json
import os
from datetime import date

import pytest

from agridrone import imagery
from agridrone.config import load_policy
from agridrone.imagery import ImageryUnavailable, Scene
from agridrone.sim import Farm

np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin  # noqa: E402

N = 8
LOC = {"lat": 41.98, "lon": -93.70}
PX = 0.0001  # ~10 m


def policy(**img):
    p = load_policy()
    p["field"].update(size=N, cell_m=50, location=LOC)
    p["imagery"] = {"enabled": True, **img}
    return p


def bbox():
    return imagery.field_bbox(LOC, N, 50)


def write_tif(path, arr, bb, dtype="uint16"):
    h, w = arr.shape
    t = from_origin(bb[0] - 5 * PX, bb[3] + 5 * PX, PX, PX)  # a little margin around the field
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1, dtype=dtype, crs="EPSG:4326", transform=t, nodata=0) as ds:
        ds.write(arr.astype(dtype), 1)
    return t


def make_item(tmp_path, name="S2X_TEST", ndvi_fn=None, cloud_block=None, boa_applied=True, date_str="2026-08-26", with_swir=True):
    """A STAC-like item whose assets are small local GeoTIFFs (EPSG:4326, ~10 m pixels) over the field."""
    bb = bbox()
    cols = int((bb[2] - bb[0]) / PX) + 10
    rows = int((bb[3] - bb[1]) / PX) + 10
    t = from_origin(bb[0] - 5 * PX, bb[3] + 5 * PX, PX, PX)
    rr, cc = np.mgrid[0:rows, 0:cols]
    lon, lat = t.c + (cc + 0.5) * t.a, t.f + (rr + 0.5) * t.e
    u = np.clip((lon - bb[0]) / (bb[2] - bb[0]), 0, 1)  # 0 at the west edge .. 1 at the east edge
    v = np.clip((lat - bb[1]) / (bb[3] - bb[1]), 0, 1)  # 0 at the south edge .. 1 at the north edge
    ndvi = ndvi_fn(u, v) if ndvi_fn else 0.2 + 0.5 * u + 0.2 * v
    nir_refl = 0.5
    red_refl = nir_refl * (1 - ndvi) / (1 + ndvi)
    off = 0.0 if boa_applied else 0.1  # when the BOA offset is NOT baked in, DN = (refl + 0.1) / 1e-4
    dn = lambda refl: np.round((refl + off) / 1e-4)
    scl = np.full((rows, cols), 4)
    if cloud_block:
        (x0, x1), (y0, y1) = cloud_block  # in field fractions
        scl[(u > x0) & (u < x1) & (v > y0) & (v < y1)] = 9
    assets = {}
    for nm, arr in (
        ("red", dn(red_refl)),
        ("nir", dn(np.full_like(ndvi, nir_refl))),
        ("scl", scl),
        ("swir16", dn(np.full_like(ndvi, 0.3))),
    ):
        if nm == "swir16" and not with_swir:
            continue
        pth = tmp_path / f"{name}_{nm}.tif"
        write_tif(pth, arr, bb)
        assets[nm] = {"href": str(pth), "raster:bands": [{"scale": 1e-4, "offset": -0.1, "nodata": 0}]}
    return {
        "id": name,
        "bbox": [bb[0] - 0.01, bb[1] - 0.01, bb[2] + 0.01, bb[3] + 0.01],
        "properties": {
            "datetime": f"{date_str}T17:00:00Z",
            "eo:cloud_cover": 12.0,
            "s2:processing_baseline": "05.12",
            "earthsearch:boa_offset_applied": boa_applied,
        },
        "assets": assets,
    }


def test_field_bbox_matches_the_policy_geometry():
    w, s, e, n = bbox()
    assert (w, s) == (LOC["lon"], LOC["lat"])
    assert abs((n - s) * 111_320 - N * 50) < 0.5 and 0.004 < e - w < 0.006


def test_scene_grid_is_oriented_x_east_y_north(tmp_path):
    sc = imagery.read_scene(make_item(tmp_path), bbox(), N)
    for x in range(N):
        for y in range(N):
            want = 0.2 + 0.5 * (x + 0.5) / N + 0.2 * (y + 0.5) / N
            assert abs(sc.ndvi[x][y] - want) < 0.04, (x, y, sc.ndvi[x][y], want)
    assert sc.valid_frac == 1.0 and sc.date == "2026-08-26" and sc.size == N


def test_reflectance_offset_follows_the_items_own_flag(tmp_path):
    """Applying the -0.1 offset on top of an already-offset DN gave NDVI > 1 on real tiles; both encodings must agree."""
    a = imagery.read_scene(make_item(tmp_path, "A", boa_applied=True), bbox(), N)
    b = imagery.read_scene(make_item(tmp_path, "B", boa_applied=False), bbox(), N)
    assert max(abs(a.ndvi[x][y] - b.ndvi[x][y]) for x in range(N) for y in range(N)) < 0.01
    assert all(-1 <= v <= 1 for _, _, v in a.cells())


def test_clouds_are_masked_before_averaging(tmp_path):
    sc = imagery.read_scene(make_item(tmp_path, cloud_block=((0.4, 0.7), (0.4, 0.7))), bbox(), N)
    masked = [(x, y) for x in range(N) for y in range(N) if sc.ndvi[x][y] is None]
    assert masked and all(2 <= x <= 5 and 2 <= y <= 5 for x, y in masked)
    assert 0.5 < sc.valid_frac < 1.0
    assert sc.ndvi[0][0] is not None and sc.ndvi[N - 1][N - 1] is not None


def test_ndmi_is_computed_when_swir_exists(tmp_path):
    sc = imagery.read_scene(make_item(tmp_path), bbox(), N)
    assert abs(sc.ndmi[3][3] - (0.5 - 0.3) / (0.5 + 0.3)) < 0.02
    nos = imagery.read_scene(make_item(tmp_path, "NS", with_swir=False), bbox(), N)
    assert all(v is None for row in nos.ndmi for v in row)


def test_search_retries_then_degrades():
    calls = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps({"features": [{"id": "x"}]}).encode()

    def flaky(req, timeout=0):
        calls.append(1)
        if len(calls) < 3:
            raise OSError("down")
        return R()

    assert imagery.search(bbox(), date(2026, 8, 1), date(2026, 9, 1), opener=flaky) == [{"id": "x"}] and len(calls) == 3

    def dead(req, timeout=0):
        raise OSError("down")

    with pytest.raises(ImageryUnavailable):
        imagery.search(bbox(), date(2026, 8, 1), date(2026, 9, 1), opener=dead)


def fake_scene(sid, d, fn, valid=1.0, size=N):
    g = [[fn(x, y) for y in range(size)] for x in range(size)]
    return Scene(id=sid, date=d, cloud_scene=5.0, valid_frac=valid, size=size, ndvi=g, ndmi=[[0.2] * size for _ in range(size)])


def test_fetch_keeps_clear_scenes_caches_them_and_skips_known_and_partial_tiles(tmp_path):
    p = policy()
    bb = bbox()
    items = [
        {"id": "good1", "bbox": bb},
        {"id": "cloudy", "bbox": bb},
        {"id": "good2", "bbox": bb},
        {"id": "edge", "bbox": [bb[0] + 0.001, bb[1], bb[2], bb[3]]},  # tile does not fully contain the field
    ]
    reads = {"good1": ("2026-08-20", 0.95), "cloudy": ("2026-08-22", 0.2), "good2": ("2026-08-26", 1.0)}
    read_fn = lambda it, b, s: fake_scene(it["id"], reads[it["id"]][0], lambda x, y: 0.7, reads[it["id"]][1])
    scenes, new = imagery.fetch(p, tmp_path, today=date(2026, 9, 1), search_fn=lambda *a: items, read_fn=read_fn)
    assert [s.id for s in new] == ["good1", "good2"] and [s.id for s in scenes] == ["good1", "good2"]
    assert (tmp_path / "imagery.json").exists()
    scenes2, new2 = imagery.fetch(p, tmp_path, today=date(2026, 9, 1), search_fn=lambda *a: items, read_fn=read_fn)
    assert new2 == [] and len(scenes2) == 2  # already cached, nothing re-read


def test_one_unreadable_scene_does_not_lose_the_others(tmp_path):
    p, bb = policy(), bbox()
    items = [{"id": "bad", "bbox": bb}, {"id": "ok", "bbox": bb}]

    def read_fn(it, b, s):
        if it["id"] == "bad":
            raise ValueError("corrupt tile")
        return fake_scene("ok", "2026-08-26", lambda x, y: 0.7)

    _, new = imagery.fetch(p, tmp_path, today=date(2026, 9, 1), search_fn=lambda *a: items, read_fn=read_fn)
    assert [s.id for s in new] == ["ok"]


def test_fetch_requires_a_field_location():
    p = load_policy()
    p["field"].pop("location", None)
    with pytest.raises(ImageryUnavailable, match="location"):
        imagery.fetch(p)


def test_analysis_finds_zones_decline_and_ranks_scouting():
    prev = fake_scene("a", "2026-08-20", lambda x, y: 0.80)
    low = lambda x, y: 0.40 if (2 <= x <= 4 and 2 <= y <= 4) else 0.80  # a struggling block
    cur = fake_scene("b", "2026-08-25", low)
    a = imagery.analyze([prev, cur])
    assert a["zones"] and a["zones"][0]["cells"] == 9 and a["zones"][0]["centre"] == [3, 3]
    assert a["declining"] == 9 and a["scouting"][0]["cell"][0] in (2, 3, 4)
    assert a["ndvi"]["min"] == 0.4 and len(a["series"]) == 2
    assert imagery.analyze([]) == {"scenes": 0}


def test_analysis_robust_z_ignores_a_few_extreme_patches():
    z = imagery.robust_z([[0.8] * 8 for _ in range(8)], 8)
    assert all(v == 0 for row in z for v in row)
    g = [[0.8 + 0.001 * ((x * 8 + y) % 5) for y in range(8)] for x in range(8)]
    g[0][0] = 0.1
    z = imagery.robust_z(g, 8)
    assert z[0][0] < -5 and abs(z[4][4]) < 3


def test_twin_is_seeded_with_the_real_fields_spatial_pattern():
    farm = Farm.create(N, 1)
    sc = fake_scene("real", "2026-08-26", lambda x, y: 0.5 if x < 3 else 0.85)  # weak vigor in the west
    imagery.init_field(farm, sc)
    west = sum(farm.cells[(x, y)].soil for x in range(3) for y in range(N)) / (3 * N)
    east = sum(farm.cells[(x, y)].soil for x in range(4, N) for y in range(N)) / (4 * N)
    assert west > east and all(0.75 <= c.soil <= 1.3 for c in farm.cells.values())


def test_sensor_crosscheck_flags_only_gross_disagreement():
    sc = fake_scene("s", "2026-08-26", lambda x, y: 0.8)
    out = imagery.crosscheck({(1, 1): 0.78, (2, 2): 0.2, (3, 3): 0.95, (99, 99): 0.1}, sc)
    assert [c for c, _ in out] == [(2, 2)]


def test_refresh_never_raises_and_reports_status(tmp_path):
    p = policy()
    assert imagery.refresh({**p, "imagery": {"enabled": False}}, tmp_path)["status"] == "disabled"

    def boom(*a, **k):
        raise ImageryUnavailable("no network")

    assert imagery.refresh(p, tmp_path, today=date(2026, 9, 1), search_fn=boom)["status"] == "unavailable"
    assert imagery.refresh(p, tmp_path, today=date(2026, 9, 1), search_fn=lambda *a: 1 / 0)["status"] == "error"
    imagery.save_scenes([fake_scene("x", "2026-08-30", lambda x, y: 0.7)], tmp_path)
    assert imagery.refresh(p, tmp_path, today=date(2026, 9, 1))["status"] == "fresh"
    assert imagery.is_stale(imagery.load_scenes(tmp_path), date(2026, 9, 20))


def test_missing_optional_dependency_gives_an_actionable_message(monkeypatch):
    def nodeps():
        raise ImageryUnavailable('needs the optional extra: pip install "agridrone[imagery]"')

    monkeypatch.setattr(imagery, "_deps", nodeps)
    with pytest.raises(ImageryUnavailable, match="imagery"):
        imagery.read_scene({"assets": {}, "properties": {}, "id": "x"}, bbox(), N)


def test_agent_seeds_the_twin_from_cached_satellite_data_once(tmp_path):
    from agridrone import store
    from agridrone.agent import run_cycle

    p = policy()
    p["imagery"] = {"enabled": False, "init_field": True}
    imagery.save_scenes([fake_scene("real1", "2026-08-26", lambda x, y: 0.5 if x < 3 else 0.85)], tmp_path)
    rec = run_cycle(p, tmp_path)
    assert rec["ok"]
    farm = store.load_farm(p["field"]["seed"], N, tmp_path)
    assert farm.init_scene == "real1" and any(
        r["kind"] == "twin_initialized_from_satellite" for r in store.read_jsonl("audit.jsonl", tmp_path)
    )
    run_cycle(p, tmp_path)
    assert sum(1 for r in store.read_jsonl("audit.jsonl", tmp_path) if r["kind"] == "twin_initialized_from_satellite") == 1


@pytest.mark.skipif(not os.environ.get("AGRIDRONE_LIVE"), reason="live network test: set AGRIDRONE_LIVE=1")
def test_live_sentinel2_over_the_default_field(tmp_path):
    """Real Sentinel-2 data end to end. Run: AGRIDRONE_LIVE=1 pytest tests/test_imagery.py -k live"""
    p = load_policy()
    scenes, new = imagery.fetch(p, tmp_path, today=date(2026, 10, 2), days=75, max_new=2)
    assert scenes, "no clear scene found"
    for s in scenes:
        vals = [v for _, _, v in s.cells()]
        assert vals and -0.2 <= min(vals) and max(vals) <= 1.0
        assert 0.1 < sum(vals) / len(vals) < 0.95  # a believable field-mean NDVI, not the >1 values of a double offset
