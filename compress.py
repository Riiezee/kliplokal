import json
import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

PRESETS = {"small": "30", "balanced": "25", "high": "20"}
RESOLUTIONS = {"original", "480", "720", "1080"}
HIDE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL_INPUT = [
    "-protocol_whitelist", "file",
    "-format_whitelist", "mov,matroska,webm,avi,mpegts,mpeg,flv,ogg,asf,mxf",
]


def probe_video(ffprobe, source):
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", *LOCAL_INPUT, "-show_entries",
             "stream=codec_type,width,height:stream_disposition=attached_pic:format=duration",
             "-of", "json", "-i", str(source)],
            capture_output=True, text=True, timeout=25, check=True, creationflags=HIDE_WINDOW,
        )
        info = json.loads(result.stdout)
        streams = info["streams"]
        video = next(s for s in streams if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic"))
        duration = float(info["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
        return duration, any(s["codec_type"] == "audio" for s in streams), video["width"], video["height"]
    except (subprocess.SubprocessError, ValueError, KeyError, StopIteration):
        raise RuntimeError("File video atau durasinya tidak bisa dibaca. Coba file MP4, MOV, MKV, atau WebM.") from None


def scale_filter(resolution):
    if resolution == "original":
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    # Use dimensions after FFmpeg's autorotation, including portrait phone videos.
    limit = int(resolution)
    return (
        f"scale=w='if(gte(iw,ih),-2,trunc(min(iw,{limit})/2)*2)'"
        f":h='if(gte(iw,ih),trunc(min(ih,{limit})/2)*2,-2)'"
    )


def encode(args, duration, stage, stages, update):
    start = (stage - 1) * 100 / stages
    message = f"Mengompres video · tahap {stage}/{stages}" if stages > 1 else "Mengompres video."
    update(phase="compressing", message=message, percent=round(start, 1), detail="")
    errors = []
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          encoding="utf-8", errors="replace", creationflags=HIDE_WINDOW) as process:
        for line in process.stdout:
            line = line.strip()
            key, _, value = line.partition("=")
            if key == "out_time_us" and value.lstrip("-").isdigit():
                fraction = min(0.99, max(0, int(value) / 1_000_000 / duration))
                update(percent=round(start + fraction * 100 / stages, 1))
            elif key == "speed" and value.strip() not in {"", "N/A"}:
                update(detail=f"Kecepatan proses {value.strip()}")
            elif line and "=" not in line:
                errors.append(line[:220])
                errors = errors[-3:]
        if process.wait():
            raise RuntimeError("FFmpeg gagal mengompres video." + (" " + " ".join(errors) if errors else ""))


def publish_file(encoded, folder, stem):
    # Claim the destination exclusively so an earlier output is never overwritten.
    for number in range(1, 10001):
        suffix = "" if number == 1 else f" ({number})"
        path = folder / f"{stem}_kompres{suffix}.mp4"
        try:
            path.touch(exist_ok=False)
        except FileExistsError:
            continue
        try:
            os.replace(encoded, path)
        except OSError:
            path.unlink(missing_ok=True)
            raise
        return path
    raise RuntimeError("Terlalu banyak file dengan nama yang sama di folder tujuan.")


def display_size(size):
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1000:
        return f"{size / 1000:.1f} KB"
    return f"{size} B"


def compress_video(source, folder, resolution, mode, preset, target_mb, update):
    try:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise RuntimeError("FFmpeg dan FFprobe belum ditemukan. Pastikan keduanya terpasang di Windows.")
        if not source.is_file():
            raise RuntimeError("File video tidak ditemukan. Pilih file dari laptop lu.")
        size_before = source.stat().st_size
        duration, has_audio, _, _ = probe_video(ffprobe, source)
        audio_kbps = 96 if has_audio else 0
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder, prefix=".streamdrop-") as work:
            output = Path(work) / "encoded.mp4"
            base = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-progress", "pipe:1", "-nostats", *LOCAL_INPUT, "-i", str(source), "-map", "0:V:0",
                "-vf", scale_filter(resolution), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast",
            ]
            audio = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k"] if has_audio else ["-an"]
            finish = [*audio, "-movflags", "+faststart", str(output)]
            if mode == "target":
                video_kbps = int(target_mb * 8000 * 0.96 / duration) - audio_kbps
                if video_kbps < 100:
                    raise RuntimeError("Target ukuran terlalu kecil untuk durasi video ini. Naikkan target MB.")
                rate = ["-b:v", f"{video_kbps}k", "-passlogfile", str(Path(work) / "passes")]
                encode([*base, *rate, "-pass", "1", "-an", "-f", "null", "-"], duration, 1, 2, update)
                encode([*base, *rate, "-pass", "2", *finish], duration, 2, 2, update)
            else:
                encode([*base, "-crf", PRESETS[preset], *finish], duration, 1, 1, update)
            update(phase="saving", message="Menyimpan hasil kompres.", detail="")
            _, _, width, height = probe_video(ffprobe, output)
            result = publish_file(output, folder, source.stem[:140])
        size_after = result.stat().st_size
        detail = f"{display_size(size_before)} → {display_size(size_after)} · {width} × {height}"
        if size_after >= size_before:
            detail += " · Hasil tidak lebih kecil dari sumber."
        if mode == "target" and size_after > target_mb * 1_000_000:
            detail += " · Hasil melebihi target ukuran."
        update(phase="done", message="Kompres video selesai.", percent=100, detail=detail, file=result.name)
    except (OSError, RuntimeError) as exc:
        update(phase="error", message=str(exc)[:600], percent=None, detail="")
