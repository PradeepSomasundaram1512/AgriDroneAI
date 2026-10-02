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


BIAS_TOL, BIAS_MIN_PASSES, BIAS_KEEP = 0.09, 3, 4


def _bias_check(by, reference, st):
    """Independent cross-check: a sensor that is consistently wetter/drier than the satellite proxy (relative to the field-wide offset
    between them) has drifted. Slow calibration drift is invisible to every in-field check because its neighbours look 'smooth'.
    -> {cell: estimated bias}."""
    res = {c: by[c]["moisture"] - reference[c] for c in by if c in reference and math.isfinite(by[c]["moisture"])}
    if res:
        off = _median(list(res.values()))
        for c, r in res.items():
            key = f"{c[0]},{c[1]}"
            st["r"][key] = (st["r"].get(key, []) + [round(r - off, 4)])[-BIAS_KEEP:]
    out = {}
    for c in by:
        h = st["r"].get(f"{c[0]},{c[1]}", [])
        if len(h) >= BIAS_MIN_PASSES:
            for sign in (1, -1):  # most recent passes only: a drift that started recently must not be diluted by older, healthy ones
                if sum(sign * r > BIAS_TOL for r in h) >= BIAS_MIN_PASSES:
                    out[c] = _median([r for r in h if sign * r > BIAS_TOL])
    return out


def clean(obs, state_dir=None, enabled=True, reference=None):
    """obs: [{'cell','moisture','ndvi','pest',...}] for the whole field. reference: optional {cell: moisture} from an independent source
    (a satellite pass; {} on a day without one) used to catch slow sensor bias. -> (cleaned obs, report dict). Never raises on bad data."""
    st = load_json("quality.json", {"h": {}, "s": {}}, state_dir)
    st.setdefault("r", {})
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
    bias = _bias_check(by, reference, st) if reference is not None else {}  # {} = check on, no pass today: judge on remembered passes
    for cell in bias:
        flagged.setdefault(cell, {})["moisture"] = "satellite_bias"
        reasons["satellite_bias"] = reasons.get("satellite_bias", 0) + 1
    # strikes over a rolling window -> quarantine (a sensor that keeps misbehaving is treated as failed, even on 'good' days)
    quarantined = set()
    hard = {c for c, b in flagged.items() if any(v != "satellite_bias" for v in b.values())}  # a correctable drift is not a failed sensor
    for cell in by:
        key = f"{cell[0]},{cell[1]}"
        s = (st["s"].get(key, []) + [1 if cell in hard else 0])[-WINDOW:]
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
            if cell in bias and cell not in quarantined:
                o2["moisture"] = o["moisture"] - bias[cell]  # known drift: subtract it rather than discard the reading
                chans = [c for c in chans if c != "moisture"]
                o2["repaired"] = True
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
            o2["repaired"] = bool(chans) or o2.get("repaired", False)
            out.append(o2)
    else:
        out = [dict(o, repaired=False) for o in obs]
    save_json("quality.json", st, state_dir)
    return out, {"flagged": len(flagged), "quarantined": sorted(quarantined), "reasons": reasons, "cells": sorted(flagged)}
