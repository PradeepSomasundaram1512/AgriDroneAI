"""Narrated walkthrough video of the DEMO dashboard (docs/dashboard_demo.html): English, Indian-English macOS voice.
Frames are real screenshots of the page taken with headless Microsoft Edge (the drone replay is captured as it animates); the camera pans
down the page in step with the narration; subtitles are burned in.   Needs: macOS `say`, ffmpeg, Microsoft Edge, Pillow.
  python video/make_demo_video.py [--voice Rishi]  ->  video/AgriDroneAI-demo-dashboard-en-IN.mp4"""

import subprocess
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "video" / "build" / "demo"
EDGE = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
PAGE = ROOT / "docs" / "dashboard_demo.html"
W, H, FPS = 1280, 720, 20
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"

# (page y to show at the top of the screen, narration). y values are card offsets measured at 1280 px wide.
SCENES = [
    (0, "This is AgriDroneAI: a farm looked after by an AI crew of three drones. What you are seeing is a simulation. One hundred and twenty days of a growing season, fast-forwarded in a few minutes."),
    (150, "Every morning the system writes a briefing in plain English. On day one hundred and twenty, eighty-nine percent of the land is fully healthy. The crew used eighty-eight percent less water than watering the whole farm on a fixed schedule, and ninety-two percent less chemical."),
    (640, "Here is one day's mission, replayed. Each drone takes off from its own pad, flies to the patches that need water or spray, treats them, and returns to recharge. Green is healthy, yellow needs attention, and brown is struggling."),
    (1450, "Drag the time-lapse slider to watch the farm change, day by day. On the right, the AI explains its own decisions in simple words, including the time it re-trained itself and checked that the new version was actually better."),
    (3010, "Safety comes first. Every flight plan is replayed second by second in three dimensions, and rejected if two drones would ever come too close. The result: zero close calls. The drones also recharge between flights, and the system tracks the health of every battery."),
    (3440, "It reads the wind forecast, and stays on the ground when flying would be unsafe. If a drone loses its G P S signal, it lands where it is, and the drones below it are ordered home."),
    (4150, "Now the honest part: does it pay? Against a fixed schedule, the drones save a lot of water. But a smart farmer who reads the same soil sensors earns almost the same profit. The real advantage here is automation, not better farming."),
    (4700, "These results come from six simulated farms over a hundred and twenty days. The A I keeps about ninety-nine percent of the crop, using roughly eighty percent less water than the fixed schedule."),
    (5200, "Everything you saw is simulation, not a field trial. The next step is a careful test with real drones and real sensors. Thank you for watching."),
]
REPLAY_SCENE, REPLAY_Y = 2, 640


def run(*a, **k):
    return subprocess.run(a, check=True, capture_output=True, **k)


def shots(n_replay=40, step_ms=450):
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "rp").mkdir(exist_ok=True)

    def shot(out, budget, h):
        run(EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size=1280,{h}", f"--virtual-time-budget={budget}", f"--screenshot={out}", PAGE.as_uri())

    shot(BUILD / "full.png", 9000, 6000)
    im = Image.open(BUILD / "full.png").convert("RGB")
    bg = im.getpixel((5, im.height - 1))
    last = max(y for y in range(im.height) if any(im.getpixel((x, y)) != bg for x in range(0, 1280, 40)))
    im.crop((0, 0, W, min(im.height, last + 60))).save(BUILD / "page.png")
    for i in range(n_replay):  # the replay animates with page time: capture it moving
        shot(BUILD / "rp" / f"f{i:02d}.png", 5000 + i * step_ms, 1500)
    return n_replay


def speak(voice, scenes=None, tag="n"):
    out = []
    for i, (_, t) in enumerate(scenes or SCENES):
        aiff, wav = BUILD / f"{tag}{i}.aiff", BUILD / f"{tag}{i}.wav"
        if voice.startswith("en-"):  # neural voice (video/tts.py)
            sys.path.insert(0, str(Path(__file__).parent))
            import tts

            tts.synth(t, wav, voice)
        else:
            run("say", "-v", voice, "-r", "165", "-o", str(aiff), t)
            run("ffmpeg", "-y", "-i", str(aiff), "-ar", "44100", "-ac", "1", str(wav))
        d = float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)).stdout)
        out.append((wav, d + 0.6))
    return out


def ease(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def caption(img, s):
    f = ImageFont.truetype(FONT, 25)
    lines = textwrap.wrap(s, 78)[:3] if len(s) < 240 else None
    if lines is None:  # long narration: show the sentence that is being spoken
        lines = textwrap.wrap(s, 78)[:3]
    d = ImageDraw.Draw(img, "RGBA")
    h = 14 + 33 * len(lines)
    d.rectangle((0, H - h - 12, W, H), fill=(0, 0, 0, 185))
    for i, ln in enumerate(lines):
        d.text((W // 2, H - h + 4 + i * 33), ln, font=f, fill=(255, 255, 255), anchor="ma")


def sentences(t):
    parts, cur = [], ""
    for ch in t:
        cur += ch
        if ch in ".?!":
            parts.append(cur.strip())
            cur = ""
    return [p for p in parts + [cur.strip()] if p]


def main(voice="Rishi", scenes=None, out_name="AgriDroneAI-demo-dashboard-en-IN.mp4", tag="n", banner_safe=True):
    scenes = scenes or SCENES
    n_rp = shots() if not (BUILD / "rp" / "f39.png").exists() else 40
    page = Image.open(BUILD / "page.png")
    rp = [Image.open(BUILD / "rp" / f"f{i:02d}.png").convert("RGB") for i in range(n_rp)]
    clips = speak(voice, scenes, tag)
    outv = BUILD / "video_only.mp4"
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(outv)],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    prev_y, tick = 0.0, 0
    for si, ((y, text), (_, dur)) in enumerate(zip(scenes, clips)):
        frames = int(dur * FPS)
        sents = sentences(text)
        total_chars = sum(len(s) for s in sents)
        for k in range(frames):
            p = k / frames
            cy = prev_y + (y - prev_y) * ease(k / (FPS * 1.6)) + p * 40  # glide in, then a slow drift
            cy = max(0, min(cy, page.height - H))
            fr = page.crop((0, int(cy), W, int(cy) + H)).copy()
            if y == REPLAY_Y:  # paste the live-animated replay over its static position
                src = rp[tick % n_rp]
                ry = REPLAY_Y - int(cy)
                if -800 < ry < H:
                    fr.paste(src.crop((0, 640, W, 1460)), (0, ry - 0 + 0)) if False else fr.paste(src.crop((0, 640, W, 1460)), (0, ry))
                tick += 1 if k % 2 == 0 else 0
            # subtitle: the sentence being spoken (by character share)
            acc, shown = 0, sents[-1]
            for s in sents:
                acc += len(s)
                if p * 0.92 * total_chars <= acc:
                    shown = s
                    break
            caption(fr, shown)
            enc.stdin.write(fr.tobytes())
        prev_y = cy
    enc.stdin.close()
    enc.wait()
    lst = BUILD / "audio.txt"
    lst.write_text("".join(f"file '{w}'\n" for w, _ in clips))
    sil = []
    out = ROOT / "video" / out_name
    run("ffmpeg", "-y", "-i", str(outv), *sum([["-i", str(w)] for w, _ in clips], []),
        "-filter_complex", "".join(f"[{i + 1}:a]apad=whole_dur={d:.3f}[a{i}];" for i, (_, d) in enumerate(clips)) + "".join(f"[a{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-shortest", str(out))
    print("wrote", out)


if __name__ == "__main__":
    v = sys.argv[sys.argv.index("--voice") + 1] if "--voice" in sys.argv else "Rishi"
    main(v)
