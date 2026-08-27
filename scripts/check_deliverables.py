#!/usr/bin/env python3
"""Fail loudly when a submission deliverable states a risk number the manifest
no longer backs.

Why this exists
---------------
On 2026-08-26 the policy went v2.2 -> v2.3 -> v2.4 -> v3.0 -> v3.1 in a single
day. The one-page write-up was updated with it. The slide deck, the narration
script, and the video captions were not, so the package simultaneously claimed
a $2,000 per-trade cap (slide 5, narrated aloud) and a $12,000 one (write-up).
A judge who reads both sees a team that either does not know its own risk
policy or is quoting the conservative version on camera. That is a pure loss:
no upside, entirely self-inflicted, and invisible unless something checks.

Prose cannot be generated from the manifest, so this does the next best thing:
it holds every deliverable to the manifest's arithmetic and refuses to be
silent when they disagree.

Two passes, because either alone has a blind spot:

  1. VALUE  — every dollar figure in a risk-flavoured line must be one the
     manifest can actually produce. Catches retired values outright ($13,000
     at-risk, $92,000 floor).

  2. SLOT   — the four headline caps are matched by the phrase around them, not
     just by value. Catches a number that is still legal somewhere but wrong
     here: $2,000 remains the Trend vector's per-trade cap, so pass 1 waves it
     through, while "hard cap on any single trade: $2,000" is now false.

Usage:
    .venv/bin/python scripts/check_deliverables.py          # report + exit code
    .venv/bin/python scripts/check_deliverables.py --list   # canonical values
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")

# Deliverables a judge actually reads or watches. Each is prose a human edits.
DELIVERABLES = [
    "docs/ONE_PAGE_WRITEUP.md",
    "docs/SUBMISSION_COPY.md",
    "docs/STRATEGY.md",
    "media/build/slides.html",     # source of slides.pdf
    "media/build/script.json",     # source of the narration AUDIO — expensive to redo
]

# Rendered from a source above. They are not scanned directly: the video's
# captions are chunked mid-sentence ("Two thousand dollars is the max on" /
# "any single trade,"), which splits the number from the phrase that gives it
# meaning and would make both passes report a false OK — the worst possible
# answer from a checker. They inherit their source's verdict instead.
DERIVED = {
    "media/build/script.json": [
        "media/audio_el/*.mp3 + sentinel-video/assets/audio/*.mp3 (ElevenLabs)",
        "sentinel-video/index.html (captions)",
        "media/sentinel_demo.mp4",
    ],
    "media/build/slides.html": ["media/slides.pdf", "media/build/slide_*.png"],
}

# Deliberately NOT checked: docs/CLAUDE_CODE_PLAN.md and docs/PLAN_VS_ACTUAL.md
# are historical records. Old numbers in them are the point — they document
# what was planned and what changed. Rewriting history to satisfy a linter
# would destroy the only account of why the policy moved.

DOLLARS = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{4,6})")

# The narration is SPOKEN, so its numbers are words: "Two thousand dollars",
# "ninety-two thousand". The digit regex above sails straight past them — and
# that file is the source of the ElevenLabs audio, i.e. the single most
# expensive deliverable to regenerate and the one that is read aloud to a
# judge. Missing it would defeat the purpose of the check.
_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}

_WORD_NUM = re.compile(
    r"\b((?:" + "|".join(list(_TENS) + list(_UNITS)) + r")"
    r"(?:[-\s](?:" + "|".join(_UNITS) + r"))?)\s+(thousand)\b", re.I)


def _spoken_amounts(line: str) -> list[int]:
    """Integers written as English words, e.g. 'ninety-two thousand' -> 92000."""
    out = []
    for phrase, _scale in _WORD_NUM.findall(line):
        total = 0
        for part in re.split(r"[-\s]+", phrase.lower()):
            if part in _TENS:
                total += _TENS[part]
            elif part in _UNITS:
                total += _UNITS[part]
        if total:
            out.append(total * 1000)
    return out
RISK_WORDS = re.compile(
    r"hard cap|per[- ]trade|max loss|maximum loss|at[- ]risk|kill switch|"
    r"Entry Maintenance|equity floor|exposure cap|book[- ]wide|across the whole book|"
    r"daily exposure|halts new entries",
    re.I)


def canonical(manifest) -> tuple[dict, set]:
    """The four headline slots, and every dollar value the manifest can make."""
    start = float(manifest.get("environment", "required_starting_equity"))
    rc = manifest.get("risk_caps")

    slots = {
        "per_trade_hard_cap": start * float(rc["max_loss_per_position_fraction"]),
        "at_risk_cap": start * float(rc["at_risk_cap_fraction"]),
        "daily_kill": start * float(rc["daily_loss_kill_fraction"]),
        "equity_floor": start * float(rc["equity_floor_fraction"]),
    }

    legal = {round(v) for v in slots.values()}
    legal.add(round(start * float(rc["daily_new_exposure_cap_fraction"])))
    legal.add(round(start))
    # every per-engine cap is a legitimate figure for prose to quote
    for cfg in manifest.get("strategies").values():
        if not isinstance(cfg, dict):
            continue
        for key, val in cfg.items():
            if key.startswith("_") or "fraction" not in key:
                continue
            try:
                legal.add(round(start * float(val)))
            except (TypeError, ValueError):
                continue
    return slots, legal


# phrase -> slot it must agree with. Order matters: first match on a line wins.
SLOT_PHRASES = [
    (re.compile(r"hard cap[^.]{0,40}?single trade|"
                r"single trade[^.]{0,40}?hard cap|"
                r"hard per-trade max loss|"
                r"hard cap \$", re.I), "per_trade_hard_cap"),
    (re.compile(r"at[- ]risk cap|across the whole book|book[- ]wide cap|"
                r"across every\s+open structure|portfolio at[- ]risk", re.I),
     "at_risk_cap"),
    (re.compile(r"kill switch", re.I), "daily_kill"),
    (re.compile(r"Entry Maintenance|equity floor", re.I), "equity_floor"),
]


def money(v: float) -> str:
    return f"${v:,.0f}"


def check(path: Path, slots: dict, legal: set) -> list[str]:
    problems: list[str] = []
    try:
        raw = path.read_text()
    except OSError as exc:
        return [f"could not read: {exc}"]

    if path.suffix == ".json":          # narration: check the spoken text only
        try:
            raw = "\n".join(s.get("text", "") for s in json.loads(raw))
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    for lineno, line in enumerate(raw.splitlines(), 1):
        found = DOLLARS.findall(line)
        values = [int(f.replace(",", "")) for f in found]
        values += _spoken_amounts(line)
        if not values:
            continue

        # pass 1 — value must be producible from the manifest
        if RISK_WORDS.search(line):
            for v in values:
                if v not in legal:
                    problems.append(
                        f"  {RED}retired value{RESET} line {lineno}: {money(v)} "
                        f"is not a figure this manifest can produce\n"
                        f"      {DIM}{line.strip()[:110]}{RESET}")

        # pass 2 — the headline slots must agree by phrase, not just by value
        for pattern, slot in SLOT_PHRASES:
            if not pattern.search(line):
                continue
            want = round(slots[slot])
            if want not in values:
                problems.append(
                    f"  {RED}wrong slot{RESET} line {lineno}: this line is "
                    f"about {slot} (manifest says {money(want)}) but quotes "
                    f"{', '.join(money(v) for v in values)}\n"
                    f"      {DIM}{line.strip()[:110]}{RESET}")
            break
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true",
                    help="print the canonical values and exit")
    args = ap.parse_args()

    from policy.loader import load as load_manifest
    manifest = load_manifest()
    slots, legal = canonical(manifest)

    print(f"\n{DIM}manifest {manifest.identity}{RESET}")
    if args.list:
        print("\ncanonical slots:")
        for k, v in slots.items():
            print(f"  {k:<22} {money(v)}")
        print("\nevery legal dollar figure:")
        print("  " + ", ".join(money(v) for v in sorted(legal)))
        return 0

    print(f"{DIM}checking {len(DELIVERABLES)} deliverables against "
          f"{money(slots['per_trade_hard_cap'])}/"
          f"{money(slots['at_risk_cap'])}/"
          f"{money(slots['daily_kill'])}/"
          f"{money(slots['equity_floor'])}{RESET}\n")

    total = 0
    for rel in DELIVERABLES:
        path = ROOT / rel
        if not path.exists():
            print(f"  [{YELLOW}SKIP{RESET}] {rel} (not present)")
            continue
        problems = check(path, slots, legal)
        total += len(problems)
        mark = f"{GREEN}OK{RESET}" if not problems else f"{RED}STALE{RESET}"
        print(f"  [{mark}] {rel}")
        for p in problems:
            print(p)
        if problems and rel in DERIVED:
            print(f"      {YELLOW}=> also stale, rendered from this file:{RESET}")
            for d in DERIVED[rel]:
                print(f"         {DIM}{d}{RESET}")

    print()
    if total == 0:
        print(f"{GREEN}Every deliverable agrees with the manifest.{RESET}")
        return 0
    print(f"{RED}{total} disagreement(s).{RESET} The manifest is the authority "
          f"— fix the prose, not the policy.")
    print(f"{DIM}Note: regenerating the narration audio and the slide PDF costs "
          f"real time and ElevenLabs credits, so do it ONCE, after the policy "
          f"is frozen — not on every intermediate version.{RESET}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
