"""Open-format export, so the system's plans open in standard tools instead of locking you in.

  * QGroundControl .plan (JSON): one file per flight: takeoff, waypoints (treatment waypoints hover for the dwell time), return-to-launch,
    plus a geofence (the field boundary as an inclusion zone, every no-fly cell as an exclusion zone). Opens in QGroundControl / Mission
    Planner-compatible tools and can be uploaded to PX4 or ArduPilot vehicles.
  * GeoJSON prescription map: one polygon per treated patch with the action, order, drone and wave: loads in QGIS, Google Earth and most
    farm-management / variable-rate software.
  * CSV of the same rows.
All coordinates come from the same patch -> lat/lon mapping the ground station uses (geo.cell_to_latlon)."""

import csv
import io
import json
import math
from pathlib import Path

from .geo import M_PER_DEG_LAT, cell_to_latlon

CMD_WAYPOINT, CMD_TAKEOFF, CMD_RTL = 16, 22, 20
FRAME_REL_ALT = 3  # MAV_FRAME_GLOBAL_RELATIVE_ALT


def origin(policy):
    """Field south-west corner: the hardware origin if set, else the field location."""
    return policy["hardware"].get("origin") or policy["field"].get("location") or {"lat": 0.0, "lon": 0.0}


def _corner(cell, o, cell_m, dx, dy):
    """Lat/lon of a patch corner (dx, dy in {-0.5, +0.5} from the patch centre). cell_to_latlon returns a patch CENTRE at (index + 0.5)
    cells, so a corner at centre + d is at index + d."""
    return cell_to_latlon((cell[0] + dx, cell[1] + dy), o, cell_m)


def _poly(cell, o, cell_m):
    pts = [_corner(cell, o, cell_m, dx, dy) for dx, dy in ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5))]
    return pts + [pts[0]]


def geofence(policy):
    """QGC geofence: the field as an inclusion polygon, each no-fly cell as an exclusion polygon."""
    o, n, cm = origin(policy), policy["field"]["size"], policy["field"]["cell_m"]
    field = [
        _corner((0, 0), o, cm, -0.5, -0.5),
        _corner((n - 1, 0), o, cm, 0.5, -0.5),
        _corner((n - 1, n - 1), o, cm, 0.5, 0.5),
        _corner((0, n - 1), o, cm, -0.5, 0.5),
    ]
    polys = [{"inclusion": True, "polygon": [list(p) for p in field], "version": 1}]
    for c in policy["no_fly_cells"]:
        polys.append({"inclusion": False, "polygon": [list(p) for p in _poly(tuple(c[:2]), o, cm)[:-1]], "version": 1})
    return {"circles": [], "polygons": polys, "version": 2}


def qgc_plan(mission, policy):
    """One flight as a QGroundControl plan dict."""
    o, cm = origin(policy), policy["field"]["cell_m"]
    dwell = float(policy.get("hardware", {}).get("dwell_s", 4))
    pad = mission.pad if mission.pad >= 0 else mission.drone
    pad_cell = (pad * policy["fleet"].get("pad_spacing_m", 40.0) / cm - 0.5, -0.5)  # pads sit just south-west of patch (0,0)
    plat, plon = cell_to_latlon(pad_cell, o, cm)
    alt = float(mission.altitude_m)
    items, jid = [], 1

    def item(cmd, params, a=alt):
        nonlocal jid
        it = {
            "AMSLAltAboveTerrain": None,
            "Altitude": a,
            "AltitudeMode": 1,
            "autoContinue": True,
            "command": cmd,
            "doJumpId": jid,
            "frame": FRAME_REL_ALT,
            "params": params,
            "type": "SimpleItem",
        }
        jid += 1
        items.append(it)

    item(CMD_TAKEOFF, [0, 0, 0, None, plat, plon, alt])
    for cell, action in mission.targets:
        lat, lon = cell_to_latlon(tuple(cell), o, cm)
        item(CMD_WAYPOINT, [0 if action == "via" else dwell, 2, 0, None, lat, lon, alt])
    item(CMD_RTL, [0, 0, 0, 0, 0, 0, 0], 0)
    return {
        "fileType": "Plan",
        "geoFence": geofence(policy),
        "groundStation": "AgriDroneAI",
        "mission": {
            "cruiseSpeed": float(policy["fleet"].get("speed_ms", 10.0)),
            "firmwareType": 12,
            "hoverSpeed": 5,
            "items": items,
            "plannedHomePosition": [plat, plon, 0],
            "vehicleType": 2,
            "version": 2,
        },
        "rallyPoints": {"points": [], "version": 2},
        "version": 1,
    }


def prescription_rows(missions):
    """One row per treated patch, in flight order."""
    rows = []
    for m in sorted(missions, key=lambda m: (m.sortie, m.drone)):
        k = 0
        for cell, action in m.targets:
            if action == "via":
                continue
            k += 1
            rows.append(
                {
                    "wave": m.sortie + 1,
                    "drone": "ABCDEFGH"[m.drone % 8],
                    "order": k,
                    "action": action,
                    "cell_x": cell[0],
                    "cell_y": cell[1],
                    "altitude_m": m.altitude_m,
                }
            )
    return rows


def geojson(missions, policy):
    o, cm = origin(policy), policy["field"]["cell_m"]
    feats = []
    for r in prescription_rows(missions):
        lat, lon = cell_to_latlon((r["cell_x"], r["cell_y"]), o, cm)
        ring = [[lon_, lat_] for lat_, lon_ in _poly((r["cell_x"], r["cell_y"]), o, cm)]  # GeoJSON order is lon, lat
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {**r, "centre_lat": round(lat, 7), "centre_lon": round(lon, 7), "hectares": round((cm / 100.0) ** 2, 3)},
            }
        )
    return {"type": "FeatureCollection", "name": "agridrone-prescription", "features": feats}


def to_csv(missions):
    rows = prescription_rows(missions)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["wave", "drone", "order", "action", "cell_x", "cell_y", "altitude_m"])
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def write_all(missions, policy, out_dir, day=None):
    """Write every flight's .plan plus the prescription map to out_dir. Returns the list of files."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    files = []
    for m in missions:
        f = d / f"drone{'ABCDEFGH'[m.drone % 8]}_wave{m.sortie + 1}.plan"
        f.write_text(json.dumps(qgc_plan(m, policy), indent=1))
        files.append(f)
    for name, text in (("prescription.geojson", json.dumps(geojson(missions, policy))), ("prescription.csv", to_csv(missions))):
        (d / name).write_text(text)
        files.append(d / name)
    return files


def bbox_of(plan):
    pts = [it["params"][4:6] for it in plan["mission"]["items"] if it["command"] == CMD_WAYPOINT]
    lats, lons = [p[0] for p in pts], [p[1] for p in pts]
    return (min(lats), min(lons), max(lats), max(lons)) if pts else None


__all__ = ["qgc_plan", "geojson", "to_csv", "write_all", "geofence", "M_PER_DEG_LAT", "math"]
