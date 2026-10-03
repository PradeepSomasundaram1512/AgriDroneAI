"""The full layman video: the animated story (make_video.py scenes, warm script, natural neural voice) followed by a narrated tour of the
demo dashboard (make_demo_video.py).   python video/make_layman_video.py [--voice en-IN-NeerjaExpressiveNeural]
Output: video/AgriDroneAI-for-everyone.mp4"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import make_demo_video as dv  # noqa: E402
import make_video as mv  # noqa: E402
import story_script  # noqa: E402
import tts  # noqa: E402


def story(voice):
    story_dir = mv.BUILD / "story"
    story_dir.mkdir(parents=True, exist_ok=True)
    mv.SCENES = story_script.SCENES

    def synth_audio():
        durs, wavs = [], []
        for i, (_, txt) in enumerate(mv.SCENES):
            wav = story_dir / f"s{i:02d}.wav"
            tts.synth(txt, wav, voice)
            d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)], capture_output=True, text=True).stdout)
            durs.append(d + 0.7)
            wavs.append(wav)
        return durs, wavs

    mv.synth_audio = synth_audio
    out = mv.ROOT / "video" / "AgriDroneAI-explainer.mp4"
    if (story_dir / "story.mp4").exists() and "--reuse" in sys.argv:
        return story_dir / "story.mp4"
    keep = story_dir / "explainer_backup.mp4"
    if out.exists():
        shutil.copy(out, keep)  # make_video writes to a fixed name: protect the committed explainer
    mv.render()
    dest = story_dir / "story.mp4"
    shutil.move(out, dest)
    if keep.exists():
        shutil.move(keep, out)
    return dest


def main(voice):
    a = story(voice)
    dv.main(voice, story_script.DASH, "AgriDroneAI-dashboard-part.mp4", "d")
    b = HERE / "AgriDroneAI-dashboard-part.mp4"
    lst = dv.BUILD / "parts.txt"
    parts = []
    for i, p in enumerate((a, b)):  # same codec settings on both so they can be joined
        q = dv.BUILD / f"part{i}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(p), "-r", "20", "-s", "1280x720", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "1", str(q)], check=True)
        parts.append(q)
    lst.write_text("".join(f"file '{q}'\n" for q in parts))
    out = HERE / "AgriDroneAI-for-everyone.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)], check=True)
    b.unlink()
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--voice") + 1] if "--voice" in sys.argv else tts.VOICE)
