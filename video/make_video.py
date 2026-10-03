"""Render the plain-English explainer video (1280x720, MP4) from the project's REAL data:
  * the farm replay is an actual simulated day (3 flight waves, wind, a GPS loss) produced by the autopilot code,
  * the satellite map is real Sentinel-2 imagery cached by the system,
  * the results chart is the measured benchmark (docs/benchmark.json).
Narration is generated with macOS `say`; scene length follows the narration. Subtitles are burned in.

  python video/make_video.py            # full video -> video/AgriDroneAI-explainer.mp4
  python video/make_video.py --preview  # a few PNG frames per scene -> video/build/preview/ (fast, no audio)"""

import json
import math
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["AGRIDRONE_OFFLINE"] = "1"
LANG = "ta" if "--lang" in sys.argv and sys.argv[sys.argv.index("--lang") + 1] == "ta" else "en"
if LANG == "ta":
    import script_ta  # noqa: E402
    from script_ta import SCENES  # noqa: E402
else:
    from script import SCENES  # noqa: E402

W, H, FPS = 1280, 720, 20
BUILD_ROOT = ROOT / "video" / "build"
BUILD = BUILD_ROOT / ("ta" if LANG == "ta" else "")
BG, FG, MUT = (15, 24, 18), (236, 244, 238), (160, 180, 166)
GREEN, BLUE, ORANGE, RED, YEL, BROWN = (95, 208, 133), (122, 162, 255), (251, 146, 60), (239, 68, 68), (214, 196, 74), (155, 106, 58)
DRONE_COL = [(255, 255, 255), (125, 211, 252), (253, 230, 138)]
ARB = "/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf"
ARR = "/System/Library/Fonts/Supplemental/Arial.ttf"
_fonts = {}


def font(size, bold=True):
    k = (size, bold)
    if k not in _fonts:
        _fonts[k] = ImageFont.truetype(ARB if bold else ARR, size)
    return _fonts[k]


def ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def lerp(a, b, t):
    return a + (b - a) * t


def mix(c1, c2, t):
    return tuple(int(lerp(a, b, t)) for a, b in zip(c1, c2))


_ct_cache = {}


def has_tamil(s):
    return any("\u0b80" <= ch <= "\u0bff" for ch in s)


def tr(s):
    """English UI string -> Tamil (when rendering the Tamil video); unknown strings stay as they are."""
    if LANG != "ta":
        return s
    if s in script_ta.TA:
        return script_ta.TA[s]
    for rx, fn in script_ta.PATTERNS:
        m = rx.fullmatch(s)
        if m:
            return fn(m)
    return s.replace("m/s", "மீ/வி")


def _ct_line(s, size, bold):
    import CoreText
    from Foundation import NSAttributedString

    f = CoreText.CTFontCreateWithName("TamilSangamMN-Bold" if bold else "TamilSangamMN", size, None)
    return NSAttributedString.alloc().initWithString_attributes_(
        s, {CoreText.kCTFontAttributeName: f, CoreText.kCTForegroundColorFromContextAttributeName: True}
    )


def ct_width(s, size, bold=True):
    """Text width in px, measured by macOS's own text engine (correct Tamil shaping)."""
    import CoreText

    line = CoreText.CTLineCreateWithAttributedString(_ct_line(s, size, bold))
    return CoreText.CTLineGetTypographicBounds(line, None, None, None)[0]


def ct_image(s, size, bold, color):
    """Render one line of text to an RGBA image with CoreText (Pillow here has no complex-script shaping)."""
    key = (s, size, bold, color)
    if key in _ct_cache:
        return _ct_cache[key]
    import CoreText
    import Quartz

    w, h = int(ct_width(s, size, bold)) + 8, int(size * 1.3) + 2
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, w * 4, cs, Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetRGBFillColor(ctx, color[0] / 255, color[1] / 255, color[2] / 255, 1.0)
    line = CoreText.CTLineCreateWithAttributedString(_ct_line(s, size, bold))
    Quartz.CGContextSetTextPosition(ctx, 4, size * 0.30)
    CoreText.CTLineDraw(line, ctx)
    data = Quartz.CGBitmapContextGetData(ctx)
    img = Image.frombuffer("RGBa", (w, h), bytes(data.as_buffer(w * h * 4)), "raw", "RGBa", w * 4, 1).convert("RGBA")
    if len(_ct_cache) > 600:
        _ct_cache.clear()
    _ct_cache[key] = img
    return img


def text(d, xy, s, size=28, color=FG, anchor="la", bold=True):
    if LANG == "ta":
        s = tr(s)
        if has_tamil(s):
            img = ct_image(s, int(size * 0.9), bold, tuple(color[:3]))
            w, h = img.size
            ax = {"l": 0.0, "m": 0.5, "r": 1.0}[anchor[0]]
            ay = {"a": 0.0, "t": 0.0, "m": 0.5, "b": 1.0, "s": 0.85}[anchor[1]]
            d._image.paste(img, (int(xy[0] - ax * w), int(xy[1] - ay * h)), img)
            return
    d.text(xy, s, font=font(size, bold), fill=color, anchor=anchor)


def wrap(s, size, maxw, bold=False):
    f, lines, cur = font(size, bold), [], ""
    for w in s.split():
        t = (cur + " " + w).strip()
        if f.getlength(t) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


# ------------------------------------------------------------------------------------------ real data
def build_story():
    """Run the real autopilot to a day with 3 flight waves, wind and a GPS loss (deterministic, cached)."""
    cache = BUILD_ROOT / "story.json"
    if cache.exists():
        return json.loads(cache.read_text())
    from agridrone.agent import run_cycle
    from agridrone.config import load_policy

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    p = load_policy()
    p["field"]["seed"], p["imagery"]["enabled"], p["gps"]["loss_per_flight_hour"] = 1, False, 0.25
    d = tempfile.mkdtemp()
    for _day in range(1, 121):
        r = run_cycle(p, d)
        if r["gps_losses"] >= 1 and r["executed"] >= 20 and r["wind_ms"] > 1.5 and r["sorties"] >= 3:
            break
    mission = json.loads((Path(d) / "last_mission.json").read_text())
    hist = json.loads((Path(d) / "field_history.json").read_text())
    story = {"mission": mission, "after": hist[-1]["cells"], "metrics": r, "size": p["field"]["size"], "fleet": p["fleet"]}
    cache.write_text(json.dumps(story))
    return story


def load_satellite():
    f = ROOT / "state" / "imagery.json"
    return json.loads(f.read_text()) if f.exists() else []


BENCH = json.loads((ROOT / "docs" / "benchmark.json").read_text())


# ------------------------------------------------------------------------------------------ drawing kit
def cell_color(c, wet=False):
    n, m, p = c
    if m < 28 or p > 50:
        return BROWN
    if m < 34 or p > 38:
        return YEL
    t = max(0.0, min(1.0, (n - 40) / 50))
    return (int(52 + (1 - t) * 20), int(138 + t * 30), int(66 - t * 12))


class Field:
    """A square grid of patches drawn from real snapshots (index = x*N + y, y = north)."""

    def __init__(self, cells, n, x, y, size):
        self.cells, self.n, self.x, self.y, self.size, self.s = cells, n, x, y, size, size / n

    def at(self, cx, cy):
        return (self.x + (cx + 0.5) * self.s, self.y + self.size - (cy + 0.5) * self.s)

    def draw(self, d, override=None, reveal=1.0, color_fn=cell_color):
        s, n = self.s, self.n
        for cx in range(n):
            for cy in range(n):
                if reveal < 1.0 and (cx + cy) / (2 * n) > reveal:
                    continue
                c = (override or {}).get((cx, cy)) or color_fn(self.cells[cx * n + cy])
                x0, x1 = round(self.x + cx * s), round(self.x + (cx + 1) * s)
                y0, y1 = round(self.y + self.size - (cy + 1) * s), round(self.y + self.size - cy * s)
                d.rectangle([x0, y0, x1 - 2, y1 - 2], fill=c)


def drone(d, x, y, col=(255, 255, 255), r=13, t=0.0, lift=0, label=None, red=False):
    d.ellipse([x - r * 1.2 + lift * 0.4, y + r * 0.5 + lift * 0.6, x + r * 1.2 + lift * 0.4, y + r * 1.4 + lift * 0.6], fill=(0, 0, 0, 70))
    for ox, oy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        px, py = x + ox * r, y + oy * r
        d.line([x, y, px, py], fill=(20, 20, 20), width=3)
        a = t * 30 + ox * 1.3
        d.line(
            [px - math.cos(a) * r * 0.7, py - math.sin(a) * r * 0.7, px + math.cos(a) * r * 0.7, py + math.sin(a) * r * 0.7],
            fill=(255, 255, 255, 200),
            width=2,
        )
    d.ellipse([x - r * 0.5, y - r * 0.5, x + r * 0.5, y + r * 0.5], fill=col, outline=(20, 20, 20), width=2)
    if red:
        d.ellipse([x - r * 1.9, y - r * 1.9, x + r * 1.9, y + r * 1.9], outline=RED, width=3)
    if label:
        text(d, (x, y - r * 1.8), label, 15, FG, "mb")


def bubble(d, x, y, r, col=(255, 255, 255, 60)):
    d.ellipse([x - r, y - r, x + r, y + r], outline=col, width=2)


def battery(d, x, y, w, h, pct, col=GREEN, label=None):
    d.rounded_rectangle([x, y, x + w, y + h], 6, outline=(200, 210, 204), width=2)
    d.rectangle([x + w, y + h * 0.3, x + w + 5, y + h * 0.7], fill=(200, 210, 204))
    fw = max(0, (w - 6) * pct / 100)
    c = col if pct > 30 else (RED if pct < 26 else ORANGE)
    d.rounded_rectangle([x + 3, y + 3, x + 3 + fw, y + h - 3], 4, fill=c)
    if label:
        text(d, (x + w / 2, y + h / 2), label, int(h * 0.55), (10, 20, 14) if pct > 55 else (255, 255, 255), "mm")


def panel(d, x0, y0, x1, y1, a=170):
    d.rounded_rectangle([x0, y0, x1, y1], 16, fill=(10, 18, 13, a), outline=(60, 80, 66, 255), width=1)


def title(d, s, sub=None):
    text(d, (60, 36), s, 42, FG)
    if sub:
        text(d, (60, 92), sub, 24, MUT, bold=False)


def arrow(d, x0, y0, x1, y1, col=(255, 255, 255), w=4, head=14):
    d.line([x0, y0, x1, y1], fill=col, width=w)
    a = math.atan2(y1 - y0, x1 - x0)
    d.polygon(
        [
            (x1, y1),
            (x1 - head * math.cos(a - 0.45), y1 - head * math.sin(a - 0.45)),
            (x1 - head * math.cos(a + 0.45), y1 - head * math.sin(a + 0.45)),
        ],
        fill=col,
    )


def droplet(d, x, y, col=(80, 140, 255, 220), r=5):
    d.ellipse([x - r, y - r, x + r, y + r + 3], fill=col)


# ------------------------------------------------------------------------------------------ the real mission replay
class Replay:
    """Segments for each flight of the real day, like the dashboard replay; sorties run one after another."""

    SPEED, DWELL, TAKEOFF = 4.2, 0.55, 0.5  # cells/s, seconds hovering, seconds climbing (video time)

    def __init__(self, story, field, waves=(0, 1)):
        self.f, self.plans, self.waves = field, [], waves
        m = story["mission"]
        padc = story["fleet"].get("pad_spacing_m", 40) / 50.0
        off = 0.0
        for k in waves:
            grp = []
            for ms in m["missions"]:
                if ms.get("sortie") != k or (ms.get("fate") or {}).get("status") == "cancelled":
                    continue
                grp.append(self._plan(ms, padc))
            for p in grp:
                p["start"] += off
                p["end"] += off
                for s in p["segs"]:
                    s["t0"] += off
                    s["t1"] += off
                    s["h"] += off
            self.plans += grp
            off = max((p["end"] for p in grp), default=off) + 2.2  # recharging pause between waves
        self.total = off

    def _plan(self, ms, padc):
        pad = ms.get("pad", ms["drone"])
        base = (pad * padc, 0.0)
        tg = ms["targets"]
        fate = ms.get("fate") or {}
        if fate.get("status") not in (None, "completed"):
            tg = tg[: fate.get("flown_targets", len(tg))]
        segs, t, cur = [], self.TAKEOFF, base
        for cx, cy, a in tg:
            dist = math.hypot(cx - cur[0], cy - cur[1])
            t1 = t + dist / self.SPEED
            dw = 0 if a == "via" else self.DWELL
            segs.append({"a": cur, "b": (cx, cy), "t0": t, "t1": t1, "h": t1 + dw, "act": None if a == "via" else a})
            t, cur = t1 + dw, (cx, cy)
        lost = fate.get("status") == "gps_lost"
        if not lost:
            dist = math.hypot(cur[0] - base[0], cur[1] - base[1])
            segs.append({"a": cur, "b": base, "t0": t, "t1": t + dist / self.SPEED, "h": t + dist / self.SPEED, "act": None, "ret": True})
            t += dist / self.SPEED
        else:
            segs.append({"a": cur, "b": cur, "t0": t, "t1": t + 1.5, "h": t + 1.5, "act": None, "gps": True})
            t += 1.5
        return {
            "drone": ms["drone"],
            "pad": pad,
            "base": base,
            "segs": segs,
            "start": 0.0,
            "end": t + 0.4,
            "lost": lost,
            "soc": ms.get("soc"),
            "alt": ms["alt"],
            "trail": [],
        }

    def state(self, p, t):
        if t < p["start"]:
            return {"pos": p["base"], "lift": 0, "idle": True}
        if t < p["start"] + self.TAKEOFF:
            return {"pos": p["base"], "lift": 20 * (t - p["start"]) / self.TAKEOFF}
        for s in p["segs"]:
            if t < s["t1"]:
                u = (t - s["t0"]) / max(1e-6, s["t1"] - s["t0"])
                return {"pos": (lerp(s["a"][0], s["b"][0], u), lerp(s["a"][1], s["b"][1], u)), "lift": 20, "gps": s.get("gps"), "u": u}
            if t < s["h"]:
                return {"pos": s["b"], "lift": 20, "act": s["act"], "hov": (t - s["t1"]) / max(1e-6, s["h"] - s["t1"]), "cell": s["b"]}
        end = p["segs"][-1]["b"]
        return {"pos": end, "lift": 0, "done": True, "gps": p["lost"]}


# ------------------------------------------------------------------------------------------ scenes
class Ctx:
    def __init__(self):
        self.story = build_story()
        n = self.story["size"]
        self.n = n
        self.before = self.story["mission"]["before"]
        self.after = self.story["after"]
        self.sat = load_satellite()
        self.fields = {}

    def field(self, key, x, y, size, cells=None):
        k = (key, x, y, size)
        if k not in self.fields:
            self.fields[k] = Field(cells or self.before, self.n, x, y, size)
        return self.fields[k]


def scene_title(im, d, u, t, c):
    f = c.field("title", 640 - 250, 140, 500)
    d.rectangle([0, 0, W, H], fill=BG)
    f.draw(d, reveal=ease(u * 2.2))
    d.rectangle([0, 0, W, H], fill=(10, 18, 13, 170))
    a = ease(u * 3)
    text(d, (W / 2, 250), "AgriDroneAI", int(70 + 14 * a), (*GREEN,), "mm")
    text(d, (W / 2, 335), "a farm that looks after itself", 38, FG, "mm")
    for k in range(3):
        x = lerp(-60, W + 60, (u * 0.8 + k * 0.28) % 1.0)
        drone(d, x, 470 + 40 * math.sin(u * 8 + k * 2), DRONE_COL[k], 15, t * 2 + k)


def scene_problem(im, d, u, t, c):
    title(d, "Watering everything on a schedule wastes water", "…versus watering only what needs it")
    fa = c.field("pa", 90, 165, 420)
    fb = c.field("pb", 770, 165, 420)
    ov_a, ov_b = {}, {}
    cyc = (t * 0.55) % 1.0
    for cx in range(c.n):
        for cy in range(c.n):
            if cyc < 0.35:
                ov_a[(cx, cy)] = mix(cell_color(c.before[cx * c.n + cy]), (60, 120, 230), math.sin(cyc / 0.35 * math.pi))
    need = [(i // c.n, i % c.n) for i, cc in enumerate(c.before) if cc[1] < 34 or cc[2] > 38][:60]
    for cx, cy in need:
        if cyc < 0.35:
            ov_b[(cx, cy)] = mix(cell_color(c.before[cx * c.n + cy]), (60, 120, 230), math.sin(cyc / 0.35 * math.pi))
    fa.draw(d, ov_a)
    fb.draw(d, ov_b)
    text(d, (300, 145), "Fixed schedule: every patch, every week", 24, FG, "mm")
    text(d, (980, 145), "Smart crew: only patches that need it", 24, GREEN, "mm")
    a = ease(u * 1.6)
    wa, wb = BENCH["strategies"][1]["water_mm"], next(x for x in BENCH["strategies"] if x["id"] == "agent-3")["water_mm"]
    text(d, (300, 595), f"{int(wa * a)} mm of water", 40, ORANGE, "mm")
    text(d, (980, 595), f"{int(wb * a)} mm of water", 40, GREEN, "mm")
    text(d, (640, 595), "vs", 28, MUT, "mm")


def scene_farm(im, d, u, t, c):
    title(d, "A pretend farm of 576 patches", "Sensors read moisture, pests and greenness every day")
    f = c.field("farm", 70, 125, 480)
    f.draw(d, reveal=ease(u * 1.8))
    healthy = sum(1 for x in c.before if not (x[1] < 34 or x[2] > 38 or x[1] < 28 or x[2] > 50))
    watch = sum(1 for x in c.before if (x[1] < 34 or x[2] > 38) and not (x[1] < 28 or x[2] > 50))
    bad = sum(1 for x in c.before if x[1] < 28 or x[2] > 50)
    for i, (col, lab, n) in enumerate(
        ((cell_color((70, 40, 10)), "healthy", healthy), (YEL, "keep an eye on it", watch), (BROWN, "struggling", bad))
    ):
        y = 190 + i * 90
        d.rounded_rectangle([700, y, 740, y + 40], 8, fill=col)
        text(d, (760, y + 20), f"{n}", 40, FG, "lm")
        text(d, (860, y + 20), lab, 28, MUT, "lm", bold=False)
    for k, (cx, cy, lab) in enumerate(((5, 17, "soil moisture"), (14, 9, "pests"), (19, 18, "greenness"))):
        a = ease(u * 4 - 1.2 - k * 0.4)
        if a > 0:
            x, y = f.at(cx, cy)
            d.ellipse([x - 9, y - 9, x + 9, y + 9], fill=(255, 255, 255), outline=(20, 20, 20), width=3)
            d.ellipse(
                [x - 22 * a - 9, y - 22 * a - 9, x + 22 * a + 9, y + 22 * a + 9], outline=(255, 255, 255, int(160 * (1 - a * 0.5))), width=2
            )
            text(d, (x + 30, y - 30), lab, 22, FG, "lm")
            d.line([x + 9, y - 9, x + 28, y - 28], fill=(255, 255, 255), width=2)


def scene_decide(im, d, u, t, c):
    title(d, "Every morning the AI decides who needs help", "Ranked from most urgent to least")
    f = c.field("decide", 70, 125, 480)
    f.draw(d)
    need = sorted(((cc[1] - 0.0 + (1 - cc[2] / 100) * 0, i) for i, cc in enumerate(c.before) if cc[1] < 34 or cc[2] > 38))
    ranked = [(i // c.n, i % c.n) for _, i in need][:14]
    for k, (cx, cy) in enumerate(ranked):
        a = ease(u * 3.2 - k * 0.14)
        if a > 0:
            x, y = f.at(cx, cy)
            r = 6 + 9 * a
            d.ellipse([x - r, y - r, x + r, y + r], outline=(255, 255, 255), width=3)
            if a > 0.8:
                text(d, (x, y - 20), str(k + 1), 16, FG, "mb")
    if u > 0.45:
        a = ease((u - 0.45) * 3)
        panel(d, 700, 200 + (1 - a) * 30, 1200, 480)
        text(d, (730, 232), "Weather forecast", 26, FG)
        for k, (day, rain) in enumerate((("Today", "dry"), ("Tomorrow", "rain 14 mm"), ("Day 3", "dry"))):
            x = 760 + k * 150
            text(d, (x, 290), day, 20, MUT, "mm", bold=False)
            if rain != "dry":
                d.ellipse([x - 28, 318, x + 28, 358], fill=(150, 170, 200))
                for j in range(3):
                    droplet(d, x - 14 + j * 14, 378 + (t * 60 + j * 14) % 22)
            else:
                d.ellipse([x - 22, 320, x + 22, 364], fill=(255, 210, 80))
            text(d, (x, 430), rain, 20, FG, "mm", bold=False)
        if u > 0.7:
            text(d, (950, 510), "Rain is coming: skip the watering, save the water", 22, GREEN, "mm")


def scene_crew(im, d, u, t, c):
    title(d, "The drone crew goes to work", "Real flights from a simulated day · own pads · waves with recharging")
    f = c.field("crew", 70, 125, 480)
    rep = c.__dict__.setdefault("rep", Replay(c.story, f))
    now = u * rep.total
    ov = {}
    for p in rep.plans:
        for s in p["segs"]:
            if s["act"] and now > s["t1"] + (s["h"] - s["t1"]) * 0.5:
                tgt = (int(s["b"][0]), int(s["b"][1]))
                if 0 <= tgt[0] < c.n and 0 <= tgt[1] < c.n:
                    ov[tgt] = (
                        cell_color(c.after[tgt[0] * c.n + tgt[1]]) if p["lost"] is False else cell_color(c.after[tgt[0] * c.n + tgt[1]])
                    )
    f.draw(d, ov)
    padc = c.story["fleet"].get("pad_spacing_m", 40) / 50.0
    for i in range(3):
        px, py = f.at(i * padc - 0.5, -0.0)
        d.ellipse([px - 11, py - 11, px + 11, py + 11], fill=(255, 255, 255, 230))
        text(d, (px, py), "ABC"[i], 14, (20, 30, 24), "mm")
    for p in rep.plans:
        st = rep.state(p, now)
        if st.get("idle"):
            continue
        x, y = f.at(*st["pos"])
        p["trail"].append((x, y)) if (not p["trail"] or math.hypot(x - p["trail"][-1][0], y - p["trail"][-1][1]) > 3) and not st.get(
            "done"
        ) else None
        if len(p["trail"]) > 1:
            d.line(p["trail"][-90:], fill=(*DRONE_COL[p["drone"] % 3], 120), width=2)
        if st.get("act"):
            for j in range(3):
                ph = (t * 1.8 + j / 3) % 1
                if st["act"] == "irrigate":
                    droplet(d, x + (j - 1) * 7, y + 6 + ph * 22)
                else:
                    r = 8 + ph * 24
                    d.ellipse([x - r, y - r + 8, x + r, y + r + 8], fill=(245, 158, 11, int(110 * (1 - ph))))
        drone(
            d,
            x,
            y - st["lift"] * 0.4,
            DRONE_COL[p["drone"] % 3],
            12,
            t * 3,
            st["lift"],
            "ABC"[p["drone"] % 3],
            red=bool(st.get("gps")) and st.get("done"),
        )
    # right: wave + battery panel
    wave = 1 + sum(1 for p in rep.plans if p["start"] <= now and p["start"] > 0.5)
    panel(d, 680, 150, 1230, 560)
    text(d, (710, 180), "Wave 1" if now < rep.total * 0.5 else "Wave 2", 32, FG)
    for i in range(3):
        y = 250 + i * 95
        mine = [p for p in rep.plans if p["drone"] == i]
        cur = next((p for p in mine if now < p["end"]), mine[-1] if mine else None)
        text(d, (710, y), f"Drone {'ABC'[i]}", 26, FG)
        d.ellipse([850, y + 4, 868, y + 22], fill=DRONE_COL[i])
        if cur and cur["soc"]:
            frac = max(0.0, min(1.0, (now - cur["start"]) / max(0.1, cur["end"] - cur["start"])))
            pct = 100 * (cur["soc"]["soc_start"] - (cur["soc"]["soc_start"] - cur["soc"]["soc_end"]) * frac) / 100
            if now < cur["start"] and cur is not mine[0]:
                prev = mine[mine.index(cur) - 1]
                uu = max(0.0, min(1.0, (now - prev["end"]) / max(0.1, cur["start"] - prev["end"])))
                pct = prev["soc"]["soc_end"] + (cur["soc"]["soc_start"] - prev["soc"]["soc_end"]) * uu
                battery(d, 710, y + 36, 300, 34, pct, BLUE, f"{int(pct)}%  charging")
                text(d, (1030, y + 52), "recharging", 20, BLUE, "lm", bold=False)
            else:
                battery(d, 710, y + 36, 300, 34, pct, GREEN, f"{int(pct)}%")
                text(d, (1030, y + 52), "flying", 20, MUT, "lm", bold=False)


def scene_charge(im, d, u, t, c):
    title(d, "Recharge between flights, and overnight", "The plan depends on what the battery can really hold")
    soc = [p["soc"] for p in c.story["mission"]["missions"] if p.get("soc")][:9]
    d.rectangle([0, 130, W, H], fill=BG)
    for i in range(3):
        x = 110 + i * 390
        text(d, (x + 130, 160), f"Drone {'ABC'[i]}", 30, FG, "mm")
        d.ellipse([x + 20, 150, x + 38, 168], fill=DRONE_COL[i])
        mine = [s for s in c.story["mission"]["missions"] if s["drone"] == i and s.get("soc")][:3]
        for k, ms in enumerate(mine):
            s = ms["soc"]
            ph = ease(u * 3 - k)
            y = 205 + k * 100
            text(d, (x, y + 18), f"Flight {k + 1}", 22, MUT, "lm", bold=False)
            pct = lerp(s["soc_start"], s["soc_end"], ph)
            battery(d, x + 100, y, 230, 36, pct, GREEN, f"{int(pct)}%")
            if k and ph < 1 and ph > 0:
                pass
    a = ease(u * 1.5)
    panel(d, 90, 515, 1190, 610)
    text(d, (120, 540), "Charger: 90 W for 45 minutes between flights (a slower charger means smaller later flights).", 23, FG, bold=False)
    text(
        d, (120, 575), "Overnight: everything back to 100%.   Batteries wear slowly: about 95% health after a season.", 23, MUT, bold=False
    )
    bx, by = 1140, 560
    d.ellipse([bx - 24, by - 24, bx + 24, by + 24], fill=(60, 70, 110))
    d.ellipse([bx - 8, by - 24, bx + 32, by + 16], fill=BG)


def scene_collide(im, d, u, t, c):
    title(d, "Drones must never bump into each other", "Own pads · own heights · every plan replayed second by second in 3D")
    d.rectangle([0, 120, W, H], fill=(14, 26, 32))
    gy = 585
    d.rectangle([0, gy, W, H], fill=(46, 84, 52))

    def X(m):
        return 140 + m * 8.2  # metres -> px

    def Z(m):
        return gy - m * 5.0

    pads = [0, 40, 80]
    for i, px in enumerate(pads):
        d.rectangle([X(px) - 20, gy - 4, X(px) + 20, gy + 4], fill=(255, 255, 255))
        text(d, (X(px), gy + 28), f"pad {'ABC'[i]}", 20, FG, "mm")
    for z in (30, 50, 70):
        d.line([100, Z(z), W - 60, Z(z)], fill=(255, 255, 255, 40), width=1)
        text(d, (W - 55, Z(z)), f"{z} m", 18, MUT, "lm", bold=False)
    # phases: 0-0.3 explain layers, 0.3-0.6 conflict, 0.6-1 fix
    if u < 0.3:
        for i, z in enumerate((30, 50, 70)):
            a = ease(u * 8 - i * 0.6)
            x = X(pads[i]) + 60 + math.sin(t * 1.5 + i) * 10
            drone(d, x, Z(z * a), DRONE_COL[i], 20, t * 3, label="ABC"[i])
            bubble(d, x, Z(z * a), 38)
        text(d, (W / 2, 160), "Each drone has its own height, at least 15 m apart", 30, FG, "mm")
    elif u < 0.62:
        v = (u - 0.3) / 0.32
        text(d, (W / 2, 160), "The risky moment: climbing through another drone's height", 30, ORANGE, "mm")
        ax = X(lerp(0, 55, ease(v)))
        drone(d, ax, Z(30), DRONE_COL[0], 20, t * 3, label="A")
        bz = 50 * ease(v * 1.15)
        drone(d, X(40), Z(bz), DRONE_COL[1], 20, t * 3, label="B")
        close = abs(ax - X(40)) < 150 and abs(Z(30) - Z(bz)) < 80
        bubble(d, ax, Z(30), 40, (255, 80, 80, 255) if close else (255, 255, 255, 80))
        bubble(d, X(40), Z(bz), 40, (255, 80, 80, 255) if close else (255, 255, 255, 80))
        if close:
            text(d, (W / 2, 220), "TOO CLOSE", 56, RED, "mm")
    else:
        v = (u - 0.62) / 0.38
        text(d, (W / 2, 160), "The fix: B waits a few seconds on its pad, then climbs", 30, GREEN, "mm")
        ax = X(lerp(0, 90, ease(min(1, v * 1.2))))
        drone(d, ax, Z(30), DRONE_COL[0], 20, t * 3, label="A")
        wait = ease((v - 0.45) * 2.2)
        drone(d, X(40), Z(50 * wait), DRONE_COL[1], 20, t * 3, label="B")
        text(d, (X(40), Z(50 * wait) - 50), f"waits {max(0, int(5 * (1 - min(1, v / 0.45))))} s" if v < 0.45 else "go", 20, FG, "mm")
        if v > 0.75:
            text(d, (W / 2, 240), "Safe  ✔", 52, GREEN, "mm")
        if v > 0.85:
            text(d, (W / 2, 300), "No safe overlap? The drones simply fly one at a time.", 24, MUT, "mm", bold=False)


def scene_wind(im, d, u, t, c):
    title(d, "Wind changes everything", "Headwinds drain the battery · too windy and the drones stay home")
    d.rectangle([0, 120, W, H], fill=BG)
    if u < 0.4:
        v = u / 0.4
        d.rounded_rectangle([140, 330, 1140, 360], 8, fill=(40, 60, 46))
        text(d, (140, 395), "pad", 22, FG, "lm", bold=False)
        text(d, (1140, 395), "target", 22, FG, "rm", bold=False)
        for k in range(7):
            x = ((t * 120 + k * 150) % 1100) + 100
            arrow(d, x, 230, x + 70, 230, (140, 180, 255), 3, 10)
        text(d, (640, 190), "wind 4 m/s", 24, BLUE, "mm")
        out = v < 0.5
        prog = ease(v / 0.5) if out else ease((v - 0.5) / 0.5)
        x = lerp(160, 1120, prog) if out else lerp(1120, 160, prog)
        drone(d, x, 345, DRONE_COL[0], 20, t * 3)
        text(d, (x, 300), "tailwind: fast, saves battery" if out else "headwind: slow, drains battery", 22, GREEN if out else ORANGE, "mm")
        battery(d, 500, 450, 280, 38, 100 - 17 * (min(v, 0.5) / 0.5) - (40 * ((v - 0.5) / 0.5) if v > 0.5 else 0), GREEN, None)
        text(d, (640, 520), "battery", 22, MUT, "mm", bold=False)
    elif u < 0.7:
        v = ease((u - 0.4) / 0.25)
        text(d, (640, 170), "A plan that ignores the wind runs out of battery", 32, FG, "mm")
        rows = [("1 m/s", 1), ("2 m/s", 6), ("3 m/s", 16), ("4 m/s", 36)]
        for i, (lab, e) in enumerate(rows):
            y = 250 + i * 80
            text(d, (230, y + 22), f"wind {lab}", 28, FG, "rm")
            d.rounded_rectangle([260, y, 260 + 7 * e * v + 4, y + 44], 8, fill=ORANGE if e > 10 else BLUE)
            text(d, (275 + 7 * e * v, y + 22), f"+{int(e * v)}% energy", 26, FG, "lm")
        text(d, (640, 585), "So every leg of every flight is costed with the wind.", 26, GREEN, "mm", bold=False)
    else:
        v = (u - 0.7) / 0.3
        text(d, (640, 165), "Wind limits", 34, FG, "mm")
        gx = 140
        for lab, lim, y, col in (("Spraying stops above", 4.5, 260, BLUE), ("Flying stops above", 6.0, 340, ORANGE)):
            d.rounded_rectangle([gx + 300, y, gx + 300 + 700, y + 34], 8, fill=(40, 50, 44))
            cur = 8 * ease(v * 1.5)
            d.rounded_rectangle([gx + 300, y, gx + 300 + 700 * min(1, cur / 8), y + 34], 8, fill=col if cur <= lim else RED)
            x = gx + 300 + 700 * lim / 8
            d.line([x, y - 8, x, y + 42], fill=FG, width=4)
            text(d, (gx + 280, y + 17), f"{lab} {lim} m/s", 24, FG, "rm")
        # spray drift
        tx = 640
        d.rectangle([tx - 70, 500, tx + 70, 535], fill=(80, 120, 60))
        text(d, (tx, 560), "target patch", 20, FG, "mm", bold=False)
        for k in range(14):
            ph = (t * 0.9 + k / 14) % 1
            drift = 140 * ph if v > 0.45 else 0
            x = tx - 40 + k * 6 + drift
            droplet(d, x, 420 + ph * 80, (245, 158, 11, 220), 4)
        if v > 0.45:
            text(d, (640, 598), "wind blows the spray off target", 22, ORANGE, "mm")


def scene_gps(im, d, u, t, c):
    title(d, "If a drone loses GPS", "It cannot navigate home, so it does something safer")
    d.rectangle([0, 120, W, H], fill=(14, 26, 32))
    gy = 585
    d.rectangle([0, gy, W, H], fill=(46, 84, 52))

    def Z(m):
        return gy - m * 5.2

    alts = (30, 50, 70)
    xs = (300, 640, 980)
    lost_u = 0.22
    for i, z in enumerate(alts):
        x = xs[i] + math.sin(t * 1.4 + i) * 8
        zz = z
        col = DRONE_COL[i]
        status, sc = "GPS ok", GREEN
        if i == 2:
            if u > lost_u:
                status, sc = "GPS LOST", RED
                if u > lost_u + 0.08:
                    status = "hold…"
                cd = 10 * (1 - (u - lost_u - 0.08) / 0.3)
                if u > lost_u + 0.38:
                    zz = z * (1 - ease((u - lost_u - 0.38) / 0.3))
                    status = "landing in place"
                if u > 0.9:
                    status = "waiting to be picked up"
        elif u > lost_u + 0.4 and i < 2:
            status, sc = "ordered home", ORANGE
            x = lerp(xs[i], 160 + i * 60, ease((u - lost_u - 0.4) / 0.35))
        y = Z(zz)
        drone(d, x, y, col, 20, t * 3, label="ABC"[i], red=(i == 2 and u > lost_u))
        text(d, (x, y - 100), status, 22, sc, "mm")
        # signal bars
        for b in range(4):
            on = (i != 2) or u < lost_u
            d.rectangle([x - 24 + b * 12, y - 52 - b * 6, x - 16 + b * 12, y - 46], fill=GREEN if on else (90, 90, 90))
        if i == 2 and u > lost_u:
            d.line([x - 28, y - 62, x + 28, y - 36], fill=RED, width=4)
            d.rounded_rectangle([x + 40, y - 40, x + 110, y - 10], 6, outline=RED, width=2)
            text(d, (x + 75, y - 25), "spray off", 16, RED, "mm", bold=False)
    for z in alts:
        d.line([100, Z(z), W - 60, Z(z)], fill=(255, 255, 255, 30), width=1)
    msg = (
        "Normal flight",
        "GPS signal lost: spraying stops instantly",
        "Holds 10 s hoping the signal returns",
        "Lands safely where it is",
        "Drones BELOW it go home at once",
        "A person picks it up; the work is rescheduled",
    )
    k = min(5, int(u * 6))
    text(d, (W / 2, 160), msg[k], 30, FG if k < 4 else GREEN, "mm")
    if lost_u + 0.08 < u < lost_u + 0.38:
        r = 40
        cx, cy = xs[2] + 130, Z(70) - 80
        d.arc([cx - r, cy - r, cx + r, cy + r], -90, -90 + 360 * (1 - (u - lost_u - 0.08) / 0.3), fill=RED, width=6)
        text(d, (cx, cy), str(max(0, int(10 * (1 - (u - lost_u - 0.08) / 0.3)) + 1)), 34, FG, "mm")


def ndvi_color(v):
    if v is None:
        return (125, 131, 138)
    t = max(0.0, min(1.0, (v - 0.15) / 0.75))
    return (155, 106, 58) if t < 0.25 else (214, 196, 74) if t < 0.5 else mix((100, 170, 70), (30, 120, 60), (t - 0.5) / 0.5)


def scene_satellite(im, d, u, t, c):
    title(d, "Real satellite photos of a real field", "Sentinel-2, Iowa · weak spots circled so a person knows where to check first")
    sc = c.sat
    if not sc:
        text(d, (W / 2, H / 2), "(no satellite data cached)", 30, MUT, "mm")
        return
    k = min(len(sc) - 1, int(u * len(sc) * 1.15))
    s = sc[k]
    n = s["size"]
    size = 440
    x0, y0 = 90, 140
    cs = size / n
    for cx in range(n):
        for cy in range(n):
            col = ndvi_color(s["ndvi"][cx][cy])
            d.rectangle([x0 + cx * cs, y0 + size - (cy + 1) * cs, x0 + (cx + 1) * cs - 1, y0 + size - cy * cs - 1], fill=col)
    text(d, (x0 + size / 2, y0 + size + 26), f"Satellite pass: {s['date']}", 24, FG, "mm")
    from agridrone import imagery
    from agridrone.imagery import Scene

    scenes = [Scene(**q) for q in sc]
    an = imagery.analyze(scenes)
                ("Smart farmer", st["triggered"]["yield"] * 100, YEL, "%"),
    if u > 0.78:
        for i, t0 in enumerate(an.get("scouting", [])[:5]):
            a = ease((u - 0.78) * 8 - i * 0.4)
            cx, cy = t0["cell"]
            px, py = x0 + (cx + 0.5) * cs, y0 + size - (cy + 0.5) * cs
            r = 8 + 14 * a
            d.ellipse([px - r, py - r, px + r, py + r], outline=(255, 255, 255), width=4)
    panel(d, 680, 150, 1220, 480)
    ser = an["series"]
    text(d, (710, 175), "Average crop vigor over time", 24, FG)
                ("Smart farmer", st["triggered"]["water_mm"], YEL, ""),
    if len(ser) > 1:
        vs = [q["ndvi"] for q in ser]
        lo, hi = min(vs) - 0.02, max(vs) + 0.02
        pts = [(720 + i * 460 / (len(vs) - 1), 450 - (v - lo) / (hi - lo) * 220) for i, v in enumerate(vs)]
        d.line(pts[: max(2, k + 1)], fill=GREEN, width=5)
        for i in range(min(len(pts), k + 1)):
            d.ellipse([pts[i][0] - 6, pts[i][1] - 6, pts[i][0] + 6, pts[i][1] + 6], fill=GREEN)
    for i, (col, lab) in enumerate(
        (
            (ndvi_color(0.8), "healthy, dense crop"),
            (ndvi_color(0.4), "weaker"),
            (ndvi_color(0.2), "bare / stressed"),
            (ndvi_color(None), "hidden by cloud"),
        )
    ):
        d.rectangle([700 + (i % 2) * 260, 505 + (i // 2) * 34, 722 + (i % 2) * 260, 527 + (i // 2) * 34], fill=col)
        text(d, (732 + (i % 2) * 260, 516 + (i // 2) * 34), lab, 19, MUT, "lm", bold=False)


def scene_sensors(im, d, u, t, c):
    title(d, "Sensors break. The system notices.", "A dead sensor reads zero and would waterlog a healthy patch")
    d.rectangle([0, 130, W, H], fill=BG)
    rnd = random.Random(4)
    gx0, gy0, cell = 120, 150, 70
    vals = [[0.30 + rnd.uniform(-0.02, 0.02) for _ in range(6)] for _ in range(6)]
    bad = (3, 2)
    for i in range(6):
        for j in range(6):
            x, y = gx0 + i * cell, gy0 + j * cell
            d.rounded_rectangle([x, y, x + cell - 8, y + cell - 8], 10, fill=(40, 70, 48))
            v = vals[i][j]
            col = FG
            s = f"{v:.2f}"
            if (i, j) == bad:
                if u < 0.35:
                    v, s, col = 0.0, "0.00", RED
                    d.rounded_rectangle([x, y, x + cell - 8, y + cell - 8], 10, outline=RED, width=4)
                elif u < 0.65:
                    v, s, col = 0.0, "0.00", RED
                    d.rounded_rectangle([x, y, x + cell - 8, y + cell - 8], 10, outline=ORANGE, width=4)
                    for ni, nj in ((2, 2), (4, 2), (3, 1), (3, 3)):
                        arrow(d, gx0 + ni * cell + 34, gy0 + nj * cell + 34, x + 34 + (ni - 3) * 18, y + 34 + (nj - 2) * 18, ORANGE, 3, 10)
                else:
                    s, col = f"{vals[i][j]:.2f}", GREEN
                    d.rounded_rectangle([x, y, x + cell - 8, y + cell - 8], 10, outline=GREEN, width=4)
            text(d, (x + (cell - 8) / 2, y + (cell - 8) / 2), s, 22, col, "mm")
    panel(d, 660, 170, 1220, 470)
    text(d, (690, 200), "What the system does", 28, FG)
    steps = ("1  Spots a reading that makes no sense", "2  Repairs it from the neighbours", "3  Tells the farmer which sensor to fix")
    for k, s in enumerate(steps):
        a = ease(u * 3.4 - k * 0.9)
        if a > 0:
            text(d, (690, 265 + k * 62), s, 24, FG if a > 0.8 else MUT, bold=False)
    text(d, (690, 500), "Measured: catches 100% of dead sensors,", 22, GREEN, bold=False)
    text(d, (690, 530), "and 98% of stuck ones", 22, GREEN, bold=False)


def scene_results(im, d, u, t, c):
    title(d, "Does it work? (simulated seasons)", f"{BENCH['seeds']} different farms · same weather for every strategy")
    st = {s["id"]: s for s in BENCH["strategies"]}
    groups = (
        (
            "Crop kept",
            [
                ("Do nothing", st["none"]["yield"] * 100, ORANGE, "%"),
                ("Fixed schedule", st["calendar"]["yield"] * 100, BLUE, "%"),
                ("AI drone crew", st["agent-3"]["yield"] * 100, GREEN, "%"),
            ],
            100.0,
            100,
        ),
        (
            "Water used (mm)",
            [
                ("Do nothing", st["none"]["water_mm"], ORANGE, ""),
                ("Fixed schedule", st["calendar"]["water_mm"], BLUE, ""),
                ("AI drone crew", st["agent-3"]["water_mm"], GREEN, ""),
            ],
            650.0,
            0,
        ),
    )
    for gi, (gt, bars, mx, _) in enumerate(groups):
        x0 = 90 + gi * 600
        text(d, (x0 + 280, 150), gt, 32, FG, "mm")
        for bi, (lab, val, col, unit) in enumerate(bars):
            a = ease(u * 1.8 - bi * 0.25 - gi * 0.2)
            bx = x0 + bi * 140
            h = 320 * (val / mx) * a
            d.rounded_rectangle([bx, 530 - h, bx + 120, 530], 10, fill=col)
            text(d, (bx + 60, 530 - h - 20), f"{val * a:.0f}{unit}" if unit == "" else f"{val * a:.1f}%", 26, FG, "mm")
            text(d, (bx + 60, 558), lab, 18, MUT, "mm", bold=False)
    lw = int(100 * (1 - st["agent-3"]["water_mm"] / st["calendar"]["water_mm"]))
    if u > 0.65:
        text(d, (W / 2, 598), f"{lw}% less water than the fixed schedule. A smart farmer with the same sensors does about as well.", 25, GREEN, "mm")


def scene_auto(im, d, u, t, c):
    title(d, "It runs by itself, every day", "…and a person always holds the stop button")
    d.rectangle([0, 130, W, H], fill=BG)
    steps = ("Wake up\n(6 am)", "Read\nthe data", "Plan\nflights", "Safety\ncheck", "Fly\n(simulated)", "Save &\nreport")
    for i, s in enumerate(steps):
        x = 150 + i * 195
        a = ease(u * 6.5 - i * 0.9)
        col = GREEN if a > 0.5 else (60, 80, 66)
        d.ellipse([x - 50, 280, x + 50, 380], fill=col if a > 0.2 else (40, 55, 46))
        text(d, (x, 330), str(i + 1), 40, (10, 20, 14) if a > 0.5 else MUT, "mm")
        for li, line in enumerate(s.split("\n")):
            text(d, (x, 415 + li * 28), line, 24, FG if a > 0.5 else MUT, "mm", bold=False)
        if i < 5:
            arrow(d, x + 56, 330, x + 138, 330, col, 4, 12)
    pulse = 1 + 0.08 * math.sin(t * 5)
    cx, cy, r = 1080, 540, 52 * pulse
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=RED, outline=(255, 255, 255), width=4)
    text(d, (cx, cy), "STOP", 32, (255, 255, 255), "mm")
    text(d, (cx - 80, cy), "Kill switch:", 26, FG, "rm")
    text(d, (120, 540), "Weekly reports · alarms if anything goes wrong · every decision logged", 24, MUT, "lm", bold=False)


def scene_close(im, d, u, t, c):
    d.rectangle([0, 0, W, H], fill=BG)
    f = c.field("close", 740, 150, 440)
    f.draw(d, reveal=1.0)
    d.rectangle([0, 0, W, H], fill=(10, 18, 13, 190))
    text(d, (80, 120), "An honest summary", 46, FG)
    items = (
        ("Simulated farm", "so we can test safely, thousands of times", ORANGE),
        ("Real satellite photos", "of a real field", GREEN),
        ("Tested on a real autopilot", "simulator for crashes, wind and GPS loss", BLUE),
        ("Next: a real pilot", "a few drones on one real field", FG),
    )
    for i, (a, b, col) in enumerate(items):
        al = ease(u * 4.5 - i * 0.7)
        if al > 0:
            y = 230 + i * 100
            d.ellipse([84, y + 6, 112, y + 34], fill=col)
            text(d, (130, y + 4), a, 32, col)
            text(d, (130, y + 44), b, 22, MUT, bold=False)
    text(d, (80, 50), "AgriDroneAI", 30, GREEN)


SCENE_FN = {
    "title": scene_title,
    "problem": scene_problem,
    "farm": scene_farm,
    "decide": scene_decide,
    "crew": scene_crew,
    "charge": scene_charge,
    "collide": scene_collide,
    "wind": scene_wind,
    "gps": scene_gps,
    "satellite": scene_satellite,
    "sensors": scene_sensors,
    "results": scene_results,
    "auto": scene_auto,
    "close": scene_close,
}


# ------------------------------------------------------------------------------------------ narration + assembly
def caption(d, narration, u):
    words = narration.split()
    per = 7 if LANG == "ta" else 10
    chunks = [" ".join(words[i : i + per]) for i in range(0, len(words), per)]
    k = min(len(chunks) - 1, int(u * len(chunks)))
    if LANG == "ta":
        lines, cur = [], ""
        for w in chunks[k].split():
            t = (cur + " " + w).strip()
            if ct_width(t, 26, False) <= W - 220:
                cur = t
            else:
                lines.append(cur)
                cur = w
        lines = lines + ([cur] if cur else [])
    else:
        lines = wrap(chunks[k], 28, W - 200)
    h = 22 + 38 * len(lines)
    d.rounded_rectangle([90, H - h - 14, W - 90, H - 14], 14, fill=(0, 0, 0, 190))
    for i, ln in enumerate(lines):
        text(d, (W / 2, H - h - 14 + (11 if LANG == "ta" else 20) + i * 38), ln, 28, (255, 255, 255), "mt", bold=False)


def synth_audio():
    BUILD.mkdir(parents=True, exist_ok=True)
    durs, wavs = [], []
    for i, (name, txt) in enumerate(SCENES):
        aiff, wav = BUILD / f"s{i:02d}.aiff", BUILD / f"s{i:02d}.wav"
        if not wav.exists():
            subprocess.run(
                ["say", "-v", "Vani" if LANG == "ta" else "Samantha", "-r", "165" if LANG == "ta" else "158", "-o", str(aiff), txt],
                check=True,
            )
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-ar", "44100", "-ac", "1", str(wav)], check=True)
        dur = float(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)], capture_output=True, text=True
            ).stdout
        )
        durs.append(dur + 0.9)
        wavs.append(wav)
    return durs, wavs


def render(preview=False):
    ctx = Ctx()
    durs, wavs = synth_audio()
    out = ROOT / "video" / ("AgriDroneAI-explainer-tamil.mp4" if LANG == "ta" else "AgriDroneAI-explainer.mp4")
    if preview:
        pdir = BUILD / "preview"
        pdir.mkdir(parents=True, exist_ok=True)
        for i, (name, txt) in enumerate(SCENES):
            for j, u in enumerate((0.15, 0.5, 0.9)):
                im = Image.new("RGB", (W, H), BG)
                d = ImageDraw.Draw(im, "RGBA")
                SCENE_FN[name](im, d, u, u * durs[i], ctx)
                caption(d, txt, u)
                im.save(pdir / f"{i:02d}_{name}_{j}.png")
        print("preview frames in", pdir)
        return
    # audio: scene narrations with a short pause after each, concatenated
    listf = BUILD / "audio.txt"
    sil = BUILD / "sil.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.9", str(sil)], check=True
    )
    listf.write_text("".join(f"file '{w}'\nfile '{sil}'\n" for w in wavs))
    full = BUILD / "narration.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listf), "-c", "copy", str(full)], check=True
    )
    ff = subprocess.Popen(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{W}x{H}",
            "-r",
            str(FPS),
            "-i",
            "-",
            "-i",
            str(full),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            str(out),
        ],
        stdin=subprocess.PIPE,
    )
    total = sum(durs)
    done = 0
    for i, (name, txt) in enumerate(SCENES):
        n = int(round(durs[i] * FPS))
        for fr in range(n):
            u, t = fr / n, fr / FPS
            im = Image.new("RGB", (W, H), BG)
            d = ImageDraw.Draw(im, "RGBA")
            SCENE_FN[name](im, d, u, t, ctx)
            caption(d, txt, u)
            if fr < 8 or fr > n - 8:  # quick fade between scenes
                a = (8 - fr) / 8 if fr < 8 else (fr - (n - 8)) / 8
                im = Image.blend(im, Image.new("RGB", (W, H), BG), max(0.0, min(0.6, a)))
            ff.stdin.write(im.tobytes())
        done += durs[i]
        print(f"scene {i + 1}/{len(SCENES)} {name}: {durs[i]:.1f}s  ({done / total * 100:.0f}%)", flush=True)
    ff.stdin.close()
    ff.wait()
    print("wrote", out, f"({total:.0f} s)")


if __name__ == "__main__":
    render(preview="--preview" in sys.argv)
