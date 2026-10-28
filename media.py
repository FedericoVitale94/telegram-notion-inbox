"""Videos (yt-dlp, audio only) and voice messages -> transcription with Groq Whisper (free tier)."""
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

import config
import llm
import net

GROQ_STT = "https://api.groq.com/openai/v1/audio/transcriptions"
# Whisper phrases that show up on music or silence ("Thank you.", "The End", subtitle credits...)
PHANTOM = re.compile(r"\W*((the )?thank you( (so much|for watching))?|thanks for watching|the end|you|bye|"
                     r"grazie( a tutti)?( per la visione)?|sottotitoli.*|untertitel.*|sous-titres.*|"
                     r"amara\.org.*)\W*", re.I)
_QUIET = logging.getLogger("yt_dlp")
_QUIET.setLevel(logging.CRITICAL)
MAX_SECONDS_VISUAL = 600  # past 10 minutes a speechless video is rare and frames say little
PROMPT_VISUAL = """These are 4 frames taken at regular intervals from a video without speech. In {language}, \
describe in 3-5 sentences what happens, and transcribe word for word any on-screen text. Only what is visible."""
MAX_SECONDS = 5400  # 90 minutes at 32 kbps mono = ~21MB, under Groq's 25MB limit


def video(url):
    """Metadata and transcript of a video. Raises if the download fails."""
    import yt_dlp

    with tempfile.TemporaryDirectory() as tmp:  # the audio never stays on disk
        opts = {
            "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "format": "bestaudio/best",
            "outtmpl": f"{tmp}/audio.%(ext)s",
            "logger": _QUIET,  # pipeline.py logs the error, once
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "32"}],
            "postprocessor_args": {"extractaudio": ["-ac", "1", "-t", str(MAX_SECONDS)]},
        }
        if "tiktok.com" in url and config.VIDEO.get("tiktok_impersonate"):
            from yt_dlp.networking.impersonate import ImpersonateTarget
            opts["impersonate"] = ImpersonateTarget.from_str("chrome")
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
            except yt_dlp.utils.DownloadError as e:  # short message, without the "please report" boilerplate
                raise RuntimeError(re.sub(r"^ERROR: |;.*$", "", str(e), flags=re.S)) from None
        audio = next(Path(tmp).glob("audio.*"))
        transcript = transcribe(audio.name, audio.read_bytes())
        visual = ""
        if not transcript and 0 < (info.get("duration") or 0) <= MAX_SECONDS_VISUAL:
            try:  # many videos are just music and on-screen text: look at the frames
                visual = llm.describe_images(frames(url, info["duration"], tmp, opts.get("impersonate")),
                                             config.prompt("visual", PROMPT_VISUAL).format(language=config.LANGUAGE))
            except Exception as e:
                logging.getLogger(__name__).warning("frames unavailable: %s", str(e)[:200])
    return {
        "title": info.get("title") or "",
        "author": info.get("uploader") or info.get("channel") or "",
        "duration": int(info.get("duration") or 0),
        "caption": info.get("description") or "",
        "transcript": transcript,
        "visual": visual,
    }


def frames(url, duration, tmp, impersonate=None):
    """4 JPEG frames at regular intervals, from a low-resolution copy of the video."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "logger": _QUIET,
            "format": "bv*[height<=480]/b[height<=480]/wv*/w", "outtmpl": f"{tmp}/video.%(ext)s"}
    if impersonate:
        opts["impersonate"] = impersonate
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    video = next(Path(tmp).glob("video.*"))
    out = []
    for i, fraction in enumerate((0.1, 0.35, 0.6, 0.85)):
        jpg = Path(tmp) / f"f{i}.jpg"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{duration * fraction:.1f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=512:-2", "-y", str(jpg)], check=True, timeout=60)
        out.append(jpg.read_bytes())
    return out


def transcribe(filename, data):
    """Spoken text; empty string when there is no speech (only music or silence)."""
    r = net.request("POST", GROQ_STT, timeout=(5, 300),
                    headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                    files={"file": (filename, data)},
                    data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json"}).json()
    parts = [s["text"].strip() for s in r.get("segments", []) if s.get("no_speech_prob", 0) < 0.6]
    return " ".join(t for t in parts if not PHANTOM.fullmatch(t)).strip()
