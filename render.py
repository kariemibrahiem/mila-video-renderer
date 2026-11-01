"""Render a vertical 120-second Mila/Pip episode with lively edits and two voices.

Compatible with the original GitHub Actions render.yml and the existing n8n
payload (eight scenes with ``image_url``, ``narration`` and ``caption``).

Optional v2 scene format::

    {"image_url": "https://...", "dialogue": [
      {"speaker": "mila", "text": "Pip, look!"},
      {"speaker": "pip", "text": "My sensors agree!"}
    ]}

Only the six-second Grok hook contains true character animation. The rest is
edited with changing digital camera moves; FFmpeg cannot animate a still
character's body or lips.
"""

import json
import math
import os
import re
import subprocess
import textwrap
import urllib.request
import wave
from pathlib import Path


ROOT = Path.cwd()
WORK = ROOT / "render_work"
W, H, FPS, SAMPLE_RATE = 1080, 1920, 30, 22050
MAX_SHOT_SECONDS = 5.0
LEGACY_SPEAKERS = (
    ("pip", "mila", "pip"),
    ("pip", "mila"),
    ("mila", "pip"),
    ("pip", "mila"),
    ("pip", "mila"),
    ("pip", "mila"),
    ("mila", "pip"),
    ("mila", "pip"),
)
LEGACY_EXTRAS = (
    "Let's investigate together!",
    "My scanners are finding another signal.",
    "We'll find a way to help everyone.",
    "Keep your eyes on that glowing path.",
    "We're getting closer, Pip!",
    "I think we've finally solved it!",
    "What a wonderful adventure that was!",
    "I can't wait to see what comes next!",
)
_RYAN_READY = None


def command(*argv):
    result = subprocess.run([str(value) for value in argv], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} failed:\n{result.stderr[-4000:]}")


def fetch(url, path, limit=150_000_000):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("A public HTTPS media URL is required")
    request = urllib.request.Request(url, headers={"User-Agent": "MilaRenderer/2.0"})
    with urllib.request.urlopen(request, timeout=120) as source, path.open("wb") as out:
        total = 0
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError("Media file exceeds the download limit")
            out.write(chunk)
    if path.stat().st_size < 1000:
        raise ValueError("Downloaded media is unexpectedly small")


def camera_filter(shot_index):
    """Short, varied virtual-camera moves; no long motionless image hold."""
    scale_w = int(round(W * 1.25 / 2)) * 2
    scale_h = int(round(H * 1.25 / 2)) * 2
    anchor_x = (0.12, 0.78, 0.43, 0.64, 0.23, 0.5)[shot_index % 6]
    anchor_y = (0.24, 0.52, 0.70, 0.35, 0.56, 0.45)[shot_index % 6]
    if shot_index % 2:
        zoom = "max(1.025,1.24-on*0.0014)"
    else:
        zoom = "min(1.24,1.025+on*0.0014)"
    return (
        f"scale={scale_w}:{scale_h}:force_original_aspect_ratio=increase,"
        f"crop={scale_w}:{scale_h},"
        f"zoompan=z='{zoom}':x='(iw-iw/zoom)*{anchor_x}':"
        f"y='(ih-ih/zoom)*{anchor_y}':d=1:s={W}x{H}:fps={FPS},"
        "setsar=1,format=yuv420p"
    )


def create_segment(source, target, duration, shot_index=0, video=False):
    frame_count = int(round(duration * FPS))
    if frame_count <= 0:
        raise ValueError("A clip must contain at least one frame")
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if video:
        args += ["-stream_loop", "-1", "-i", source]
        visual = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                  f"crop={W}:{H},fps={FPS},setsar=1,format=yuv420p")
    else:
        args += ["-loop", "1", "-framerate", str(FPS), "-i", source]
        visual = camera_filter(shot_index)
    args += ["-vf", visual, "-frames:v", str(frame_count), "-an",
             "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "25", "-pix_fmt", "yuv420p", target]
    command(*args)


def ensure_robot_voice():
    """Download Piper's male voice when possible, without editing render.yml."""
    global _RYAN_READY
    if _RYAN_READY is not None:
        return _RYAN_READY
    model = ROOT / "en_US-ryan-medium.onnx"
    if model.exists():
        _RYAN_READY = True
        return True
    python = os.getenv("PIPER_PYTHON", ".venv/bin/python")
    try:
        process = subprocess.run(
            [python, "-m", "piper.download_voices", "en_US-ryan-medium"],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        _RYAN_READY = process.returncode == 0 and model.exists()
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        _RYAN_READY = False
    if not _RYAN_READY:
        print("WARNING: Ryan voice unavailable; using offline male espeak voice")
    return _RYAN_READY


def synthesize(text, speaker, target):
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        raise ValueError("Cannot synthesize empty speech")
    speaker = str(speaker).strip().lower()
    is_robot = speaker in ("pip", "robot")
    use_piper = not is_robot or ensure_robot_voice()
    if use_piper:
        try:
            model = "en_US-ryan-medium" if is_robot else "en_US-ljspeech-medium"
            command(os.getenv("PIPER_PYTHON", ".venv/bin/python"), "-m",
                    "piper", "-m", model, "-f", target, "--", text)
        except (RuntimeError, FileNotFoundError):
            use_piper = False
    if not use_piper:
        voice = "en+m3" if is_robot else "en+f3"
        command("espeak-ng", "-v", voice, "-s", "155", "-w", target, text)

    fixed = target.with_name("normalized_" + target.name)
    effects = ["-af", "highpass=f=180,lowpass=f=4200"] if is_robot else []
    command("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i",
            target, *effects, "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a",
            "pcm_s16le", fixed)
    fixed.replace(target)


def legacy_dialogue(scene, scene_index):
    """Give the *existing* flattened narration two character voices.

    The original 40-node story writer always emits the same dialogue order
    for its eight scene templates. A later n8n update may send explicit
    ``dialogue`` for a more reliable result.
    """
    text = re.sub(r"\s+", " ", str(scene.get("narration") or "")).strip()
    parts = [part.strip() for part in re.findall(r"[^.!?]+(?:[.!?]+|$)", text)
             if part.strip()]
    if not parts:
        parts = ["Mila, look over there!", "I'm on it!"]
    speakers = LEGACY_SPEAKERS[scene_index]
    lines = [{"speaker": speakers[i % len(speakers)], "text": part}
             for i, part in enumerate(parts)]
    if len(lines) <= 2:
        lines.append({"speaker": "mila" if scene_index % 2 else "pip",
                      "text": LEGACY_EXTRAS[scene_index]})
    return lines


def get_dialogue(scene, index):
    raw = scene.get("dialogue") or scene.get("lines")
    if not isinstance(raw, list) or not raw:
        return legacy_dialogue(scene, index)
    lines = []
    for entry in raw:
        if isinstance(entry, str):
            text, speaker = entry, "mila" if len(lines) % 2 == 0 else "pip"
        elif isinstance(entry, dict):
            text = str(entry.get("text") or entry.get("line") or "")
            speaker = str(entry.get("speaker") or "mila").lower()
        else:
            continue
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            lines.append({"speaker": "pip" if speaker in ("pip", "robot") else "mila",
                          "text": text})
    return lines or legacy_dialogue(scene, index)


def timestamp(seconds):
    milliseconds = int(round(seconds * 1000))
    hours, remaining = divmod(milliseconds, 3600000)
    minutes, remaining = divmod(remaining, 60000)
    sec, millis = divmod(remaining, 1000)
    return f"{hours:02}:{minutes:02}:{sec:02},{millis:03}"


def subtitle_text(speaker, text):
    name = "Pip" if speaker == "pip" else "Mila"
    cleaned = re.sub(r"\s+", " ", text).strip()
    return "\n".join(textwrap.wrap(f"{name}: {cleaned}", width=36,
                                   max_lines=2, placeholder="..."))


def create_audio_and_captions(scenes, scene_duration):
    track = bytearray()
    subtitles = []
    subtitle_number = 1
    samples_per_scene = int(round(scene_duration * SAMPLE_RATE))

    for scene_index, scene in enumerate(scenes):
        dialogue = get_dialogue(scene, scene_index)
        rendered = []
        for line_number, line in enumerate(dialogue):
            path = WORK / f"voice_{scene_index:02}_{line_number:02}.wav"
            synthesize(line["text"], line["speaker"], path)
            with wave.open(str(path), "rb") as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SAMPLE_RATE):
                    raise ValueError("Speech must be 16-bit mono WAV at 22050 Hz")
                frames = wav.readframes(wav.getnframes())
            rendered.append((line, frames))

        total_samples = sum(len(frames) // 2 for _, frames in rendered)
        # Avoid chopping an unusually verbose script: use FFmpeg to speed up
        # the generated speech only when it cannot fit into a scene.
        available = max(1, samples_per_scene - int(0.6 * SAMPLE_RATE))
        if total_samples > available:
            ratio = total_samples / available
            adjusted = []
            for number, (line, frames) in enumerate(rendered):
                input_file = WORK / f"voice_{scene_index:02}_{number:02}.wav"
                shortened = WORK / f"short_voice_{scene_index:02}_{number:02}.wav"
                # FFmpeg atempo supports 0.5..100 in current Ubuntu FFmpeg.
                command("ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", input_file, "-af", f"atempo={ratio:.6f}",
                        "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", shortened)
                with wave.open(str(shortened), "rb") as wav:
                    frames = wav.readframes(wav.getnframes())
                adjusted.append((line, frames))
            rendered = adjusted
            total_samples = sum(len(frames) // 2 for _, frames in rendered)

        free_samples = max(0, samples_per_scene - total_samples)
        gap = min(int(0.9 * SAMPLE_RATE), free_samples // max(1, len(rendered) + 1))
        position = min(int(0.35 * SAMPLE_RATE), free_samples - gap * max(0, len(rendered) - 1))
        position = max(0, position)
        scene_audio = bytearray(samples_per_scene * 2)

        for line, frames in rendered:
            start = position
            length = min(len(frames) // 2, samples_per_scene - start)
            if length <= 0:
                break
            scene_audio[start * 2:(start + length) * 2] = frames[:length * 2]
            begin = scene_index * scene_duration + start / SAMPLE_RATE
            end = begin + length / SAMPLE_RATE
            if end - begin > 0.12:
                subtitles.extend([str(subtitle_number),
                                  f"{timestamp(begin)} --> {timestamp(end)}",
                                  subtitle_text(line["speaker"], line["text"]), ""])
                subtitle_number += 1
            position = start + length + gap

        track.extend(scene_audio)

    out = WORK / "dialogue.wav"
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(track)
    sub = WORK / "captions.srt"
    sub.write_text("\n".join(subtitles), encoding="utf-8")
    return out, sub


def render(scenes, motion, scene_duration, hook_duration):
    clips = []
    number = 0
    for index, scene in enumerate(scenes):
        if index == 0:
            intro = WORK / "cut_000.mp4"
            create_segment(motion, intro, hook_duration, video=True)
            clips.append(intro)
            number += 1

        still_seconds = scene_duration - (hook_duration if index == 0 else 0)
        shots = max(1, math.ceil(still_seconds / MAX_SHOT_SECONDS))
        total_frames = int(round(still_seconds * FPS))
        first, leftovers = divmod(total_frames, shots)
        for shot in range(shots):
            frames = first + (1 if shot < leftovers else 0)
            target = WORK / f"cut_{number:03}.mp4"
            create_segment(WORK / f"image_{index:02}.png", target,
                           frames / FPS, number)
            clips.append(target)
            number += 1

    playlist = WORK / "cuts.txt"
    playlist.write_text("\n".join(f"file '{clip.name}'" for clip in clips) + "\n",
                        encoding="utf-8")
    joined = WORK / "joined.mp4"
    command("ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", playlist,
            "-c", "copy", "-movflags", "+faststart", joined)

    audio, srt = create_audio_and_captions(scenes, scene_duration)
    style = ("FontName=DejaVu Sans,FontSize=24,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=2,"
             "Shadow=1,Alignment=2,MarginV=170")
    command("ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", joined, "-i", audio,
            "-vf", f"subtitles={srt}:force_style='{style}'",
            "-map", "0:v:0", "-map", "1:a:0",
            "-t", str(scene_duration * len(scenes)),
            "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "24", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-b:a", "128k", "-movflags", "+faststart", ROOT / "final.mp4")
    print(f"SUCCESS: {len(clips)} edited shots, two character voices, captions")


def main():
    payload = json.loads(os.environ["RENDER_PAYLOAD"])
    scenes = payload.get("scenes")
    if not isinstance(scenes, list) or len(scenes) != 8:
        raise ValueError("Exactly eight scenes are required")
    duration = float(payload.get("scene_duration", 15))
    hook_duration = float(payload.get("hook_duration", 6))
    if not (1 < duration <= 15 and 0 < hook_duration < duration):
        raise ValueError("Invalid scene or hook duration")
    motion_url = payload.get("video_url")
    if not isinstance(motion_url, str) or not motion_url.startswith("https://"):
        raise ValueError("A public HTTPS Grok hook video is required")

    WORK.mkdir(exist_ok=True)
    fetched = {}
    for index, scene in enumerate(scenes):
        url = scene.get("image_url")
        if url not in fetched:
            original = WORK / f"download_{len(fetched):02}.img"
            fetch(url, original)
            fetched[url] = original
        command("ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", fetched[url], "-frames:v", "1",
                WORK / f"image_{index:02}.png")
    motion = WORK / "motion.mp4"
    fetch(motion_url, motion)
    render(scenes, motion, duration, hook_duration)

    process = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(ROOT / "final.mp4")],
        capture_output=True, text=True, check=True,
    )
    actual = float(process.stdout.strip())
    if abs(actual - 8 * duration) > 0.7:
        raise RuntimeError(f"Unexpected final duration: {actual:.2f} seconds")
    print(f"FINAL MP4: {actual:.2f} seconds, {W}x{H}, {FPS} fps")


if __name__ == "__main__":
    main()
