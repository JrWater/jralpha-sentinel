#!/usr/bin/env python3
"""Convert HyperFrames word timings into the ElevenLabs-style character alignment.

The narrator text remains the canonical script.  Word timing is used only to
place each matching spoken token; spaces inherit the silence between words.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = json.loads((ROOT / "../media/build/script.json").read_text())
TEXT = next(item["text"] for item in SCRIPT if item["id"] == "04_ai_logic")
WORDS = json.loads((ROOT / "assets/audio/transcript.json").read_text())
OUT = ROOT / "assets/align/04_ai_logic.json"

TOKEN = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
NUMBER_WORDS = {"ten": "10"}


def normalized(token):
    return NUMBER_WORDS.get(token.lower(), token.lower())


script_tokens = list(TOKEN.finditer(TEXT))
spoken_tokens = [word for word in WORDS if TOKEN.search(word["text"])]
EXPANSIONS = {
    "ten-sample": ["10", "sample"],
    "one-d-t-e": ["1dte"],
    "s-p-y": ["spy"],
    "zero-d-t-e": ["0dte"],
    "nine": ["9"],
    "post-earnings": ["post", "earnings"],
}

starts = [0.0] * len(TEXT)
ends = [0.0] * len(TEXT)
previous_end = 0
spoken_index = 0
for index, match in enumerate(script_tokens):
    token = match.group(0)
    wanted = EXPANSIONS.get(token.lower(), [normalized(token)])
    words = spoken_tokens[spoken_index:spoken_index + len(wanted)]
    actual = [normalized(TOKEN.search(word["text"]).group(0)) for word in words]
    if actual != wanted:
        raise SystemExit(f"token mismatch at {index}: {token!r} expects {wanted!r}, got {actual!r}")
    spoken_index += len(wanted)
    begin, finish = float(words[0]["start"]), float(words[-1]["end"])
    width = max(match.end() - match.start(), 1)
    for offset, position in enumerate(range(match.start(), match.end())):
        starts[position] = begin + (finish - begin) * offset / width
        ends[position] = begin + (finish - begin) * (offset + 1) / width
    for position in range(match.start() - 1, previous_end - 1, -1):
        starts[position] = ends[previous_end - 1]
        ends[position] = begin
    previous_end = match.end()

if spoken_index != len(spoken_tokens):
    raise SystemExit(f"unconsumed spoken tokens: {len(spoken_tokens) - spoken_index}")

for position in range(previous_end, len(TEXT)):
    starts[position] = float(WORDS[-1]["end"])
    ends[position] = float(WORDS[-1]["end"])

OUT.write_text(json.dumps({
    "characters": list(TEXT),
    "character_start_times_seconds": starts,
    "character_end_times_seconds": ends,
}, indent=2) + "\n")
