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
FEW_WORDS = 15  # a shorter transcript is often Whisper noise on music ("The"): the frames are looked at too
PROMPT_PHOTO = """These are the images of a social media photo post or carousel, in order. In {language}, describe in \
2-5 sentences what they show, and transcribe word for word any text on them. Only what is visible."""
MAX_SLIDES = 4
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
        duration = info.get("duration") or seconds(audio)  # Instagram does not say how long a reel is
        visual = ""
        if needs_frames(transcript, duration):
            try:  # many videos are just music and on-screen text: look at the frames (the transcript is kept)
                visual = llm.describe_images(frames(url, duration, tmp, opts.get("impersonate")),
                                             config.prompt("visual", PROMPT_VISUAL).format(language=config.LANGUAGE))
            except Exception as e:
                logging.getLogger(__name__).warning("frames unavailable: %s", str(e)[:200])
    return {
        "title": info.get("title") or "",
        "author": info.get("uploader") or info.get("channel") or "",
        "duration": int(duration),
        "caption": info.get("description") or "",
        "transcript": transcript,
        "visual": visual,
    }


def seconds(path):
    """Length of a media file in seconds, 0 if ffprobe cannot tell."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0


def needs_frames(transcript, duration):
    """No speech, or so few words they may be noise: the frames say what the video is about."""
    return len(transcript.split()) < FEW_WORDS and 0 < duration <= MAX_SECONDS_VISUAL


def photo_post(url):
    """A photo post or carousel (no video): its caption and a description of its first MAX_SLIDES images.
    Raises if there is no image."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "logger": _QUIET, "ignore_no_formats_error": True}
    if "tiktok.com" in url and config.VIDEO.get("tiktok_impersonate"):
        from yt_dlp.networking.impersonate import ImpersonateTarget
        opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    # a carousel comes as a playlist, one entry per slide
    urls = [e["thumbnail"] for e in info.get("entries") or [info] if e and e.get("thumbnail")][:MAX_SLIDES]
    if not urls:
        raise RuntimeError("no image in the post")
    images = []
    with tempfile.TemporaryDirectory() as tmp:  # any format (webp, heic...) -> JPEG, as describe_images expects
        for n, u in enumerate(urls):
            src, jpg = Path(tmp) / f"image{n}", Path(tmp) / f"image{n}.jpg"
            src.write_bytes(net.request("GET", u, retries=2).content)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(src), "-frames:v", "1", "-vf",
                            "scale='min(1024,iw)':-2", "-y", str(jpg)], check=True, timeout=60)
            images.append(jpg.read_bytes())
        visual = llm.describe_images(images, config.prompt("photo", PROMPT_PHOTO).format(language=config.LANGUAGE))
    return {"title": info.get("title") or "", "author": info.get("uploader") or info.get("channel") or "",
            "duration": 0, "caption": info.get("description") or "", "transcript": "", "visual": visual,
            "photo": True}


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
