"""Offline MP4 renderer for an n8n/GitHub Actions eight-scene short film.

RENDER_PAYLOAD is a JSON object containing 8 HTTPS image URLs, 8 short
English narrations/captions and one HTTPS AI-motion video URL.
"""

import json
import os
import re
import subprocess
import urllib.request
import wave
from pathlib import Path

ROOT = Path.cwd()
WORK = ROOT / "render_work"
W, H, FPS, SAMPLE_RATE = 1080, 1920, 30, 22050
TRANSITION = 0.5


def command(*argv):
    p = subprocess.run(list(map(str, argv)), text=True, capture_output=True)
    if p.returncode:
        raise RuntimeError(f"{argv[0]} failed:\n{p.stderr[-4000:]}")


def fetch(url, path, limit=120_000_000):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("Public HTTPS media URL required")
    req = urllib.request.Request(url, headers={"User-Agent": "MilaRenderer/1.0"})
    with urllib.request.urlopen(req, timeout=90) as source, path.open("wb") as dest:
        size = 0
        while data := source.read(1024 * 1024):
            size += len(data)
            if size > limit:
                raise ValueError("Media file too large")
            dest.write(data)
    if path.stat().st_size < 1000:
        raise ValueError("Downloaded media looks empty")


def create_segment(source, target, duration, index, is_video=False):
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if is_video:
        args += ["-stream_loop", "-1", "-i", source]
        visual = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},fps={FPS},setsar=1,format=yuv420p")
    else:
        args += ["-loop", "1", "-framerate", str(FPS), "-i", source]
        zoom = ("min(1.18,1.01+on*0.00033)" if index % 2 else
                "max(1.01,1.18-on*0.00033)")
        visual = ("scale=1320:2348:force_original_aspect_ratio=increase,"
                  "crop=1320:2348,"
                  f"zoompan=z='{zoom}':x='(iw-iw/zoom)/2':"
                  f"y='(ih-ih/zoom)/2':d=1:s={W}x{H}:fps={FPS},"
                  "setsar=1,format=yuv420p")
    args += ["-t", str(duration), "-vf", visual, "-an", "-r", str(FPS),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
             "-pix_fmt", "yuv420p", target]
    command(*args)


def narrator(text, target):
    text = re.sub(r"\s+", " ", text).strip() or "Mila and Pip continue the adventure."
    try:
        command(os.getenv("PIPER_PYTHON", ".venv/bin/python"), "-m", "piper",
                "-m", "en_US-ljspeech-medium", "-f", target, "--", text)
    except (RuntimeError, FileNotFoundError):
        command("espeak-ng", "-v", "en-us", "-s", "145", "-w", target, text)
    fixed = target.with_name("fixed_" + target.name)
    command("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i",
            target, "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", fixed)
    fixed.replace(target)


def timestamp(seconds):
    msec = int(round(seconds * 1000))
    h, rest = divmod(msec, 3600000)
    m, rest = divmod(rest, 60000)
    s, ms = divmod(rest, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def create_audio_and_captions(scenes, scene_duration):
    all_voice = bytearray()
    srt = []
    for index, scene in enumerate(scenes):
        audio = WORK / f"voice_{index:02}.wav"
        narrator(str(scene.get("narration") or ""), audio)
        with wave.open(str(audio), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
                raise ValueError("Unsupported audio format")
            frames = wav.readframes(wav.getnframes())
        maximum = int(scene_duration * SAMPLE_RATE) * 2
        frames = (b"\0" * int(0.25 * SAMPLE_RATE) * 2 + frames)[:maximum]
        all_voice.extend(frames.ljust(maximum, b"\0"))
        caption = re.sub(r"\s+", " ", str(scene.get("caption") or ""))[:88]
        if caption:
            srt.extend([str(index + 1),
                        timestamp(index * scene_duration) + " --> " +
                        timestamp((index + 1) * scene_duration - 0.12),
                        caption, ""])
    audio_out = WORK / "narration.wav"
    with wave.open(str(audio_out), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(all_voice)
    subs = WORK / "captions.srt"
    subs.write_text("\n".join(srt), encoding="utf-8")
    return audio_out, subs


def render(scenes, motion, scene_duration, hook_duration):
    files, durations = [], []
    if motion:
        hook = WORK / "hook_segment.mp4"
        create_segment(motion, hook, hook_duration, -1, is_video=True)
        files.append(hook)
        durations.append(hook_duration)
    for index in range(8):
        length = scene_duration + TRANSITION - (hook_duration if index == 0 and motion else 0)
        if length <= TRANSITION:
            raise ValueError("Clip duration is shorter than transition")
        target = WORK / f"scene_segment_{index:02}.mp4"
        create_segment(WORK / f"image_{index:02}.png", target, length, index)
        files.append(target)
        durations.append(length)
    voice, subtitles = create_audio_and_captions(scenes, scene_duration)

    ffmpeg = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for file in files:
        ffmpeg += ["-i", file]
    ffmpeg += ["-i", voice]
    filters = [f"[{i}:v]setpts=PTS-STARTPTS[v{i}]" for i in range(len(files))]
    prior, cumulative = "v0", durations[0]
    for i in range(1, len(files)):
        name = f"joined{i}"
        effect = "fade" if i % 2 else "smoothleft"
        filters.append(f"[{prior}][v{i}]xfade=transition={effect}:"
                       f"duration={TRANSITION}:offset={cumulative-TRANSITION:.3f}[{name}]")
        cumulative += durations[i] - TRANSITION
        prior = name
    style = ("FontName=DejaVu Sans,FontSize=48,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H40000000,BackColour=&H80000000,BorderStyle=3,"
             "Outline=2,Shadow=1,Alignment=2,MarginV=220")
    filters.append(f"[{prior}]subtitles={subtitles}:force_style='{style}'[picture]")
    ffmpeg += ["-filter_complex", ";".join(filters), "-map", "[picture]",
               "-map", f"{len(files)}:a:0", "-t", str(scene_duration * 8),
               "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "24", "-pix_fmt", "yuv420p", "-c:a", "aac",
               "-b:a", "128k", "-movflags", "+faststart", ROOT / "final.mp4"]
    command(*ffmpeg)


def main():
    payload = json.loads(os.environ["RENDER_PAYLOAD"])
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != 8:
        raise ValueError("Exactly eight scenes required")
    duration = float(payload.get("scene_duration", 15))
    hook_duration = float(payload.get("hook_duration", 6))
    if not (1 < duration <= 15 and 0 < hook_duration < duration):
        raise ValueError("Invalid scene and hook durations")
    motion_url = payload.get("video_url")
    if not isinstance(motion_url, str) or not motion_url.startswith("https://"):
        raise ValueError("One public HTTPS Grok animation is required")
    WORK.mkdir(exist_ok=True)
    downloaded = {}
    for i, scene in enumerate(scenes):
        url = scene.get("image_url")
        if url not in downloaded:
            original = WORK / f"download_{len(downloaded):02}.img"
            fetch(url, original)
            downloaded[url] = original
        command("ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", downloaded[url], "-frames:v", "1", WORK / f"image_{i:02}.png")
    motion = WORK / "motion.mp4"
    fetch(motion_url, motion, limit=150_000_000)
    render(scenes, motion, duration, hook_duration)
    output = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", ROOT / "final.mp4"],
                            text=True, check=True, capture_output=True)
    actual = float(output.stdout.strip())
    if abs(actual - 8 * duration) > 0.7:
        raise RuntimeError(f"Unexpected MP4 duration: {actual:.2f}s")
    print(f"SUCCESS: created MP4 lasting {actual:.2f} seconds")


if __name__ == "__main__":
    main()
