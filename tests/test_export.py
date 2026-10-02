import csv
import io
import json
import math

from agridrone import cli, export, planner, store
from agridrone.agent import run_cycle
from agridrone.config import load_policy
from agridrone.geo import cell_to_latlon
from agridrone.hardware import FlightExecutor
from agridrone.safety import Mission


def pol():
    p = load_policy()
    p["hardware"].update(origin={"lat": 41.98, "lon": -93.70})
    return p


def mission():
    return Mission(1, 50, [((5, 5), "irrigate"), ((9, 4), "via"), ((12, 8), "spray")], 20.0, sortie=1, pad=1)


def test_qgc_plan_structure_is_what_qgroundcontrol_expects():
    p, m = pol(), mission()
    plan = export.qgc_plan(m, p)
    json.dumps(plan)  # serialisable
    assert plan["fileType"] == "Plan" and plan["version"] == 1 and plan["mission"]["version"] == 2
    items = plan["mission"]["items"]
    assert [i["command"] for i in items] == [22, 16, 16, 16, 20]  # takeoff, 3 waypoints, return-to-launch
    assert [i["doJumpId"] for i in items] == [1, 2, 3, 4, 5]
    assert all(i["frame"] == 3 and i["type"] == "SimpleItem" and i["autoContinue"] for i in items)
    assert (
        items[1]["params"][0] == p["hardware"]["dwell_s"]
        and items[2]["params"][0] == 0
        and items[3]["params"][0] == p["hardware"]["dwell_s"]
    )  # via: no hover
    assert items[1]["Altitude"] == 50 and items[-1]["command"] == 20
    assert len(plan["mission"]["plannedHomePosition"]) == 3


def test_waypoints_match_what_the_ground_station_would_fly():
    p, m = pol(), mission()
    plan = export.qgc_plan(m, p)
    ex = FlightExecutor(p)
    for it, (lat, lon, alt) in zip(plan["mission"]["items"][1:-1], ex.waypoints(m)):
        assert abs(it["params"][4] - lat) < 1e-9 and abs(it["params"][5] - lon) < 1e-9 and it["Altitude"] == alt


def test_geofence_has_the_field_as_inclusion_and_every_no_fly_cell_as_exclusion():
    p = pol()
    fence = export.geofence(p)
    inc = [z for z in fence["polygons"] if z["inclusion"]]
    exc = [z for z in fence["polygons"] if not z["inclusion"]]
    assert len(inc) == 1 and len(inc[0]["polygon"]) == 4 and len(exc) == len(p["no_fly_cells"])
    lats = [pt[0] for pt in inc[0]["polygon"]]
    lons = [pt[1] for pt in inc[0]["polygon"]]
    n, cm = p["field"]["size"], p["field"]["cell_m"]
    assert abs((max(lats) - min(lats)) * 111_320 - n * cm) < 1.0
    assert abs((max(lons) - min(lons)) * 111_320 * math.cos(math.radians(41.98)) - n * cm) < 1.0
    for z in exc:  # each no-fly polygon sits inside the field
        assert all(min(lats) <= q[0] <= max(lats) and min(lons) <= q[1] <= max(lons) for q in z["polygon"])


def test_patch_polygon_is_exactly_one_cell_and_tiles_with_its_neighbour():
    p = pol()
    cm = p["field"]["cell_m"]
    a = export._poly((5, 5), p["hardware"]["origin"], cm)
    b = export._poly((6, 5), p["hardware"]["origin"], cm)
    lat_w = (max(q[0] for q in a) - min(q[0] for q in a)) * 111_320
    lon_w = (max(q[1] for q in a) - min(q[1] for q in a)) * 111_320 * math.cos(math.radians(41.98))
    assert abs(lat_w - cm) < 0.05 and abs(lon_w - cm) < 0.05
    assert abs(max(q[1] for q in a) - min(q[1] for q in b)) < 1e-9  # neighbours share an edge: no gap, no overlap
    f = export.geofence(p)["polygons"][0]["polygon"]  # the field fence starts exactly at the origin corner
    assert abs(min(q[0] for q in f) - 41.98) < 1e-9 and abs(min(q[1] for q in f) + 93.70) < 1e-9


def test_geojson_prescription_one_closed_polygon_per_treated_patch():
    p = pol()
    gj = export.geojson([mission()], p)
    assert gj["type"] == "FeatureCollection" and len(gj["features"]) == 2  # the via waypoint is not a treated patch
    f = gj["features"][0]
    ring = f["geometry"]["coordinates"][0]
    assert f["geometry"]["type"] == "Polygon" and len(ring) == 5 and ring[0] == ring[-1]
    assert f["properties"]["action"] == "irrigate" and f["properties"]["drone"] == "B" and f["properties"]["wave"] == 2
    lat, lon = cell_to_latlon((5, 5), p["hardware"]["origin"], p["field"]["cell_m"])
    assert abs(f["properties"]["centre_lat"] - lat) < 1e-6 and abs(f["properties"]["centre_lon"] - lon) < 1e-6
    lons = [c[0] for c in ring]
    lats = [c[1] for c in ring]
    assert min(lons) < lon < max(lons) and min(lats) < lat < max(lats)  # GeoJSON is lon,lat and the centre is inside


def test_csv_rows_are_in_flight_order():
    rows = list(csv.DictReader(io.StringIO(export.to_csv([mission()]))))
    assert [r["order"] for r in rows] == ["1", "2"] and rows[1]["action"] == "spray" and rows[0]["cell_x"] == "5"


def test_planner_output_exports_cleanly(tmp_path):
    p = pol()
    import random

    r = random.Random(2)
    tg = sorted([(r.random(), (r.randrange(2, 24), r.randrange(0, 24)), "irrigate") for _ in range(120)], reverse=True)
    ms = planner.plan(tg, p, sorties=2)
    files = export.write_all(ms, p, tmp_path)
    plans = [f for f in files if f.suffix == ".plan"]
    assert len(plans) == len(ms) and all(json.loads(f.read_text())["fileType"] == "Plan" for f in plans)
    assert json.loads((tmp_path / "prescription.geojson").read_text())["features"]
    south, west, north, east = export.bbox_of(json.loads(plans[0].read_text()))
    assert 41.98 <= south and north <= 41.98 + 0.0109 and -93.70 <= west and east <= -93.70 + 0.0146  # inside the 1.2 km field


def test_cli_export_and_economics(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(store, "STATE_DIR", tmp_path / "state")
    p = load_policy()
    p["field"]["size"] = 12
    p["gps"]["loss_per_flight_hour"] = 0
    monkeypatch.setattr("agridrone.agent.load_policy", lambda: p)
    for _ in range(40):
        run_cycle(p, tmp_path / "state")
        if store.load_json("last_mission.json", {}, tmp_path / "state").get("missions"):
            break
    assert cli.main(["export", "--out", str(tmp_path / "out")]) == 0
    assert any((tmp_path / "out").glob("*.plan")) and (tmp_path / "out" / "prescription.csv").exists()
    assert cli.main(["economics"]) == 0
    assert "PLACEHOLDER" in capsys.readouterr().out
