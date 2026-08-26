#!/usr/bin/env python3
"""Assemble the narrated video from per-scene frames + narration audio.

Each scene is rendered as its own short clip (static frame, matched-length
narration), then concatenated via the ffmpeg concat demuxer (stream copy,
no re-encode) since every clip shares codec/resolution/fps. A single global
fade in/out is applied to the final file.
"""
import json
import subprocess
from pathlib import Path

BUILD = Path(__file__).parent
AUDIO = BUILD.parent / "audio"
OUT = BUILD / "clips"
OUT.mkdir(exist_ok=True)

FRAME_MAP = {
    "01_title": "slide_01.png",
    "02_problem": "slide_02.png",
    "03_architecture": "slide_03.png",
    "04_ai_logic": "slide_04.png",
    "05_risk_gates": "slide_05.png",
    "06_infra": "slide_06.png",
    "07_dashboard": "slide_07.png",
    "08_code": "slide_08_code.png",
    "09_results": "slide_08.png",
    "10_closing": "slide_09.png",
}

script = json.loads((BUILD / "script.json").read_text())

clip_paths = []
for scene in script:
    sid = scene["id"]
    frame = BUILD / FRAME_MAP[sid]
    audio = AUDIO / f"{sid}.aiff"
    clip = OUT / f"{sid}.mp4"
    assert frame.exists(), f"missing frame {frame}"
    assert audio.exists(), f"missing audio {audio}"

    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-i", str(frame),
        "-i", str(audio),
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-vf", "scale=1920:1080",
        "-shortest",
        str(clip),
    ], check=True)
    clip_paths.append(clip)
    print(f"  {sid:<16} -> {clip.name}")

filelist = OUT / "concat.txt"
filelist.write_text("\n".join(f"file '{p.resolve()}'" for p in clip_paths) + "\n")

concat_out = BUILD / "video_concat.mp4"
subprocess.run([
    "ffmpeg", "-y", "-loglevel", "error",
    "-f", "concat", "-safe", "0", "-i", str(filelist),
    "-c", "copy",
    str(concat_out),
], check=True)

final_out = BUILD / "sentinel_demo.mp4"
# global fade in/out for polish; re-encode once here only
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
     "-of", "default=noprint_wrappers=1:nokey=1", str(concat_out)],
    capture_output=True, text=True)
duration = float(probe.stdout.strip())
fade_out_start = max(0, duration - 0.6)

subprocess.run([
    "ffmpeg", "-y", "-loglevel", "error",
    "-i", str(concat_out),
    "-vf", f"fade=t=in:st=0:d=0.5,fade=t=out:st={fade_out_start:.2f}:d=0.6",
    "-af", f"afade=t=in:st=0:d=0.4,afade=t=out:st={fade_out_start:.2f}:d=0.6",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
    str(final_out),
], check=True)

print(f"\nDuration: {duration:.1f}s = {duration/60:.2f} min")
print(f"Final: {final_out}")
