#!/usr/bin/env python3
"""Remove the obsolete spoken gate count without changing narrator or take.

The two affected phrases are isolated words.  This script cuts exactly their
ElevenLabs character-alignment window from the MP3 and applies the same edit to
the character timeline and authoritative narration script.  It is idempotent:
after the replacement is present, rerunning it is a no-op.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT.parent / "media" / "build" / "script.json"
EDITS = {
    "01_title": ("sixteen ", ""),
    "03_architecture": ("Sixteen ", ""),
}


def edit_scene(scene_id: str, old: str, new: str) -> bool:
    alignment_path = ROOT / "assets" / "align" / f"{scene_id}.json"
    audio_path = ROOT / "assets" / "audio" / f"{scene_id}.mp3"
    alignment = json.loads(alignment_path.read_text())
    text = "".join(alignment["characters"])
    if old not in text:
        if scene_id == "03_architecture" and "gates across" in text:
            index = text.index("gates across")
            alignment["characters"][index] = "G"
            alignment_path.write_text(
                json.dumps(alignment, separators=(",", ":")))
            return True
        if new and new not in text:
            raise RuntimeError(f"{scene_id}: neither source nor replacement found")
        return False

    start_index = text.index(old)
    end_index = start_index + len(old)
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    cut_start = float(starts[start_index])
    cut_end = float(starts[end_index])
    removed = cut_end - cut_start

    replacement_chars = list(new)
    replacement_starts = [cut_start] * len(replacement_chars)
    replacement_ends = [cut_start] * len(replacement_chars)
    alignment["characters"] = (
        alignment["characters"][:start_index] + replacement_chars
        + alignment["characters"][end_index:])
    alignment["character_start_times_seconds"] = (
        starts[:start_index] + replacement_starts
        + [round(float(value) - removed, 3) for value in starts[end_index:]])
    alignment["character_end_times_seconds"] = (
        ends[:start_index] + replacement_ends
        + [round(float(value) - removed, 3) for value in ends[end_index:]])

    output = audio_path.with_suffix(".countless.mp3")
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(audio_path), "-filter_complex",
        (f"[0:a]atrim=end={cut_start},asetpts=PTS-STARTPTS[a0];"
         f"[0:a]atrim=start={cut_end},asetpts=PTS-STARTPTS[a1];"
         "[a0][a1]concat=n=2:v=0:a=1[out]"),
        "-map", "[out]", "-codec:a", "libmp3lame", "-b:a", "128k",
        str(output),
    ], check=True)
    output.replace(audio_path)
    alignment_path.write_text(json.dumps(alignment, separators=(",", ":")))
    return True


def main() -> None:
    script = json.loads(SCRIPT.read_text())
    changed = False
    for scene_id, (old, new) in EDITS.items():
        changed |= edit_scene(scene_id, old, new)
        row = next(item for item in script if item["id"] == scene_id)
        row["text"] = row["text"].replace(old, new)
        if scene_id == "03_architecture":
            row["text"] = row["text"].replace("gates across", "Gates across")
    if changed:
        SCRIPT.write_text(json.dumps(script, indent=2) + "\n")


if __name__ == "__main__":
    main()
