"""Grid cell -> WGS84. Local equirectangular projection around the field's south-west corner;
accurate to well under a metre at field scale."""
import math

M_PER_DEG_LAT = 111_320.0


def cell_to_latlon(cell, origin, cell_m):
    """Centre of `cell` (x east, y north). origin = {"lat":..,"lon":..}."""
    north = (cell[1] + 0.5) * cell_m
    east = (cell[0] + 0.5) * cell_m
    lat = origin["lat"] + north / M_PER_DEG_LAT
    lon = origin["lon"] + east / (M_PER_DEG_LAT * math.cos(math.radians(origin["lat"])))
    return lat, lon
