"""Media optimisation: images via Pillow, videos via ffmpeg.

Originals are never touched -- they stay in data/raw so a better encoder can
reprocess them later.
"""
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageSequence

from . import config


@dataclass
class Result:
    media_path: Path
    thumb_path: Path
    width: int
    height: int


class MediaError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 1800) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise MediaError(f"{cmd[0]} failed ({proc.returncode}): {' | '.join(tail)}")
    return proc.stdout


def process_image(src: Path, slug: str) -> Result:
    """Convert to WebP, scale down to IMAGE_MAX_WIDTH, write a thumbnail.

    Animated GIFs keep their animation (WebP supports it); a flattened first
    frame would silently turn a moving banner into a still one.
    """
    out = config.MEDIA_DIR / f"{slug}.webp"
    thumb = config.MEDIA_DIR / f"{slug}_thumb.webp"

    with Image.open(src) as im:
        width, height = im.size
        animated = getattr(im, "n_frames", 1) > 1

        if animated:
            frames = []
            for frame in ImageSequence.Iterator(im):
                frame = frame.convert("RGBA")
                if frame.width > config.IMAGE_MAX_WIDTH:
                    ratio = config.IMAGE_MAX_WIDTH / frame.width
                    frame = frame.resize(
                        (config.IMAGE_MAX_WIDTH, max(1, round(frame.height * ratio))),
                        Image.LANCZOS,
                    )
                frames.append(frame)
            width, height = frames[0].size
            frames[0].save(
                out, "WEBP", save_all=True, append_images=frames[1:],
                duration=im.info.get("duration", 100),
                loop=im.info.get("loop", 0), quality=80, method=4,
            )
            still = frames[0]
        else:
            still = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
            if still.width > config.IMAGE_MAX_WIDTH:
                ratio = config.IMAGE_MAX_WIDTH / still.width
                still = still.resize(
                    (config.IMAGE_MAX_WIDTH, max(1, round(still.height * ratio))),
                    Image.LANCZOS,
                )
            width, height = still.size
            still.save(out, "WEBP", quality=82, method=6)

        t = still.copy()
        if t.width > config.THUMB_WIDTH:
            ratio = config.THUMB_WIDTH / t.width
            t = t.resize((config.THUMB_WIDTH, max(1, round(t.height * ratio))), Image.LANCZOS)
        t.save(thumb, "WEBP", quality=75, method=6)

    return Result(out, thumb, width, height)


def probe_video(src: Path) -> tuple[int, int]:
    raw = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(src),
    ], timeout=120)
    streams = json.loads(raw).get("streams") or []
    if not streams:
        raise MediaError("no video stream found")
    return int(streams[0]["width"]), int(streams[0]["height"])


def process_video(src: Path, slug: str) -> Result:
    """Transcode to web-friendly H.264/AAC MP4 and grab a thumbnail."""
    out = config.MEDIA_DIR / f"{slug}.mp4"
    thumb = config.MEDIA_DIR / f"{slug}_thumb.webp"

    width, height = probe_video(src)
    # Scale down only, and keep both dimensions even -- H.264 requires it.
    scale = (
        f"scale='min({config.VIDEO_MAX_WIDTH},iw)':-2"
        if width > config.VIDEO_MAX_WIDTH
        else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    )

    _run([
        "ffmpeg", "-y", "-i", str(src),
        "-vf", scale,
        "-c:v", "libx264", "-preset", "medium", "-crf", "24",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        # Move the index to the front so the browser can start without the
        # full file -- without this an ad only plays after a full download.
        "-movflags", "+faststart",
        str(out),
    ])

    # Transcoding can inflate an already well-compressed clip. Shipping more
    # bytes than we received would defeat the purpose, so in that case keep the
    # original and only move its index to the front.
    if out.stat().st_size >= src.stat().st_size and _is_web_ready(src):
        _run([
            "ffmpeg", "-y", "-i", str(src), "-c", "copy",
            "-movflags", "+faststart", str(out),
        ])

    _run([
        "ffmpeg", "-y", "-i", str(out), "-vframes", "1",
        "-vf", f"scale={config.THUMB_WIDTH}:-2", str(thumb),
    ], timeout=120)

    final_w, final_h = probe_video(out)
    return Result(out, thumb, final_w, final_h)


# Browsers decode 4:2:0 only. An H.264 file in 4:4:4 or 4:2:2 ("High 4:4:4
# Predictive") plays fine in VLC and shows nothing at all in Chrome -- and it
# fails silently: the video element just never leaves readyState 0.
BROWSER_PIXEL_FORMATS = {"yuv420p", "yuvj420p", "nv12"}


def _is_web_ready(path: Path) -> bool:
    """True when a browser could play the file as-is.

    Codec alone is not enough: pixel format and profile decide whether the
    file plays or hangs. ffmpeg's own testsrc, for example, produces
    yuv444p H.264 by default.
    """
    raw = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,pix_fmt,profile", "-of", "json", str(path),
    ], timeout=120)
    streams = json.loads(raw).get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        return False
    v = video[0]
    if v.get("codec_name") != "h264":
        return False
    if v.get("pix_fmt") not in BROWSER_PIXEL_FORMATS:
        return False
    if "4:4:4" in (v.get("profile") or "") or "4:2:2" in (v.get("profile") or ""):
        return False
    if int(v.get("width") or 0) > config.VIDEO_MAX_WIDTH:
        return False
    return all(a.get("codec_name") == "aac" for a in audio)


def kind_for(suffix: str) -> str | None:
    s = suffix.lower()
    if s in config.IMAGE_EXTENSIONS:
        return "image"
    if s in config.VIDEO_EXTENSIONS:
        return "video"
    return None


def tools_available() -> dict[str, bool]:
    return {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
    }
