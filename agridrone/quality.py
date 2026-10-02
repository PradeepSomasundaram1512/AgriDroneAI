"""Sensor data-quality layer: detect bad readings, repair them, and report which sensors need maintenance.

A control system that trusts every reading is only as good as its worst sensor: a dead moisture probe reading 0 makes it
irrigate a healthy patch forever, a stuck or biased one hides a real drought. Checks, per patch and per channel:
  range           physically impossible values (NaN, negative, > 1)
  spatial outlier far from the median of its 8 neighbours (soil/crop state is spatially smooth)
  stuck           bit-identical readings for several days
  jump            implausible day-over-day change (faster than rain/ET/irrigation can move soil water)
  quarantine      a patch flagged on >= STRIKES of the last WINDOW days is treated as a failed sensor
Flagged readings are replaced by the median of healthy neighbours. State is small and committed with the rest."""

import math

from .store import load_json, save_json

CH = ("moisture", "ndvi", "pest")
LIMITS = {"moisture": (-0.03, 0.65), "ndvi": (-0.05, 1.0), "pest": (-0.1, 1.1)}
SPATIAL = {"moisture": 0.16, "ndvi": 0.22, "pest": 0.30}  # max plausible deviation from the neighbourhood median
JUMP = {"moisture": 0.22, "ndvi": 0.25, "pest": 0.35}  # max plausible change in one day
HIST, STRIKES, WINDOW = 4, 3, 7


def _median(v):
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def clean(obs, state_dir=None, enabled=True):
    """obs: [{'cell','moisture','ndvi','pest',...}] for the whole field. -> (cleaned obs, report dict). Never raises on bad data."""
    st = load_json("quality.json", {"h": {}, "s": {}}, state_dir)
    by = {tuple(o["cell"]): o for o in obs}
    flagged, reasons = {}, {}
    for cell, o in by.items():
        key = f"{cell[0]},{cell[1]}"
        bad = {}
        for ch in CH:
            v = o[ch]
            hist = st["h"].get(key + ch, [])
            why = None
            if v is None or not math.isfinite(v) or not (LIMITS[ch][0] <= v <= LIMITS[ch][1]):
                why = "range"
            else:
                nb = [
                    by[(cell[0] + dx, cell[1] + dy)][ch]
                    for dx in (-1, 0, 1)
                    for dy in (-1, 0, 1)
                    if (dx or dy) and (cell[0] + dx, cell[1] + dy) in by and math.isfinite(by[(cell[0] + dx, cell[1] + dy)][ch])
                ]
                if len(nb) >= 3 and abs(v - _median(nb)) > SPATIAL[ch]:
                    why = "spatial_outlier"
                elif len(hist) >= HIST - 1 and max(hist + [round(v, 4)]) - min(hist + [round(v, 4)]) < 1e-6:
                    why = "stuck"
                elif hist and abs(v - hist[-1]) > JUMP[ch]:
                    why = "jump"
            if why:
                bad[ch] = why
        if bad:
            flagged[cell] = bad
            for w in bad.values():
                reasons[w] = reasons.get(w, 0) + 1
    # strikes over a rolling window -> quarantine (a sensor that keeps misbehaving is treated as failed, even on 'good' days)
    quarantined = set()
    for cell in by:
        key = f"{cell[0]},{cell[1]}"
        s = (st["s"].get(key, []) + [1 if cell in flagged else 0])[-WINDOW:]
        st["s"][key] = s
        if sum(s) >= STRIKES:
            quarantined.add(cell)
    # remember raw readings (for stuck/jump checks), then repair
    for cell, o in by.items():
        key = f"{cell[0]},{cell[1]}"
        for ch in CH:
            v = o[ch]
            if v is not None and math.isfinite(v):
                st["h"][key + ch] = (st["h"].get(key + ch, []) + [round(v, 4)])[-HIST:]
    out = []
    if enabled:
        for o in obs:
            cell, o2 = tuple(o["cell"]), dict(o)
            chans = list(CH) if cell in quarantined else list(flagged.get(cell, {}))
            for ch in chans:
                good = [
                    by[(cell[0] + dx, cell[1] + dy)][ch]
                    for dx in (-2, -1, 0, 1, 2)
                    for dy in (-2, -1, 0, 1, 2)
                    if (dx or dy)
                    and (cell[0] + dx, cell[1] + dy) in by
                    and (cell[0] + dx, cell[1] + dy) not in flagged
                    and (cell[0] + dx, cell[1] + dy) not in quarantined
                    and math.isfinite(by[(cell[0] + dx, cell[1] + dy)][ch])
                ]
                o2[ch] = _median(good) if good else _median([x[ch] for x in obs if math.isfinite(x[ch])])
            o2["repaired"] = bool(chans)
            out.append(o2)
    else:
        out = [dict(o, repaired=False) for o in obs]
    save_json("quality.json", st, state_dir)
    return out, {"flagged": len(flagged), "quarantined": sorted(quarantined), "reasons": reasons, "cells": sorted(flagged)}
