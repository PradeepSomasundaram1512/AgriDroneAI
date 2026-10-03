"""Natural narration: Microsoft neural voices through the `edge-tts` package (the narration TEXT is sent to Microsoft's speech service;
nothing else leaves the machine). Falls back to nothing: callers use macOS `say` if this raises.   pip install edge-tts"""

import asyncio
import subprocess
from pathlib import Path

VOICE = "en-IN-NeerjaExpressiveNeural"


def synth(text, out_wav: Path, voice=VOICE, rate="-6%", pitch="+0Hz"):
    import edge_tts

    mp3 = out_wav.with_suffix(".mp3")

    async def go():
        await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(str(mp3))

    asyncio.run(go())
    trim = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-af", trim, "-ar", "44100", "-ac", "1", str(out_wav)], check=True)  # no dead air at either end
    return out_wav
