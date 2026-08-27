#!/usr/bin/env python3
"""Narration/timeline compiler for the Sentinel demo composition.

Authored narration metadata in (narration.json + the ElevenLabs alignment
files) -> static HyperFrames timing out (scene windows, audio placements, the
T map, caption cues, scene header ranges), plus bound checks on the motion
cues that stay hand-authored.

Every timing fact in index.html is derived here. Nothing is hand-typed twice.

  python3 tools/timeline.py            # write index.html
  python3 tools/timeline.py --check    # verify it is current; nonzero if not
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "index.html")


def scene_file(key):
    """Path of a scene's sub-composition, if it has been split out yet."""
    path = os.path.join(ROOT, "compositions", "scene-%s.html" % key[1:])
    return path if os.path.exists(path) else None

BEGIN = "// <<< GENERATED %s -- edit narration.json, then `npm run timing`"
END = "// >>> GENERATED %s"


# ---------------------------------------------------------------- formatting

def f3(v):
    """Attribute/T-map form: fixed millisecond precision, bare zero."""
    return "0" if abs(v) < 5e-4 else "%.3f" % v


def fs(v):
    """Cue/caption-array form: millisecond precision, trailing zeros stripped."""
    return ("%.3f" % v).rstrip("0").rstrip(".") or "0"


# ------------------------------------------------------------------- loading

def load():
    cfg = json.load(open(os.path.join(ROOT, "narration.json")))
    script = {s["id"]: s for s in json.load(open(os.path.join(ROOT, cfg["script"])))}
    for scene in cfg["scenes"]:
        path = os.path.join(ROOT, cfg["alignDir"], scene["id"] + ".json")
        a = json.load(open(path))
        scene["text"] = "".join(a["characters"])
        scene["cstart"] = a["character_start_times_seconds"]
        scene["cend"] = a["character_end_times_seconds"]
        scene["raw"] = a["character_end_times_seconds"][-1]
        if scene["id"] not in script:
            die("%s is missing from the narration script" % scene["id"])
    return cfg, script


def die(msg):
    sys.stderr.write("timeline: %s\n" % msg)
    sys.exit(1)


# ------------------------------------------------------------------ compiler

def compile_timeline(cfg, captions):
    """Pure: metadata + alignment + caption text -> the timing model.

    Scene starts AND ends both come from cumulative raw narration time divided
    by the speed factor, so durations tile the root exactly with no 1ms gap or
    overlap between neighbours.
    """
    speed = cfg["speed"]
    scenes, cum = [], 0.0
    start = 0.0
    for s in cfg["scenes"]:
        cum += s["raw"]
        end = round(cum / speed, 3)
        scenes.append({
            "key": s["key"], "id": s["id"], "raw": s["raw"],
            "rawStart": cum - s["raw"],
            "start": start, "duration": round(end - start, 3), "end": end,
        })
        start = end
    total = scenes[-1]["end"]
    by_key = {s["key"]: s for s in scenes}

    # Caption times: the text stays hand-authored in index.html; its timing is
    # searched out of the alignment, so a re-record retimes captions for free
    # and a caption that no longer matches the narration is a hard failure.
    cap_out = []
    cursor = {}
    for cid, text in captions:
        key = "s" + cid.split("-")[1][1:]
        src = next(s for s in cfg["scenes"] if s["key"] == key)
        needle = text.strip()
        at = src["text"].find(needle, cursor.get(key, 0))
        if at < 0:
            at = src["text"].find(needle)
            if at < 0:
                die("caption %s is not in the %s narration: %r" % (cid, src["id"], needle))
        cursor[key] = at + len(needle)
        base = by_key[key]["rawStart"]
        cap_out.append({
            "id": cid, "scene": key,
            "start": round(base + src["cstart"][at], 3),
            "end": round(base + src["cend"][at + len(needle) - 1], 3),
        })

    # Phrase-anchored motion cues, scene-relative raw seconds.
    cues = {}
    for key, items in cfg.get("cues", {}).items():
        src = next(s for s in cfg["scenes"] if s["key"] == key)
        out = []
        for item in items:
            at = src["text"].find(item["phrase"])
            if at < 0:
                die("cue phrase for %s is not in the %s narration: %r"
                    % (item["target"], src["id"], item["phrase"]))
            t = round(src["cstart"][at], 2)
            if t >= src["raw"]:
                die("cue %s lands at %.2fs, past the %.2fs end of %s"
                    % (item["target"], t, src["raw"], src["id"]))
            out.append((item["target"], t))
        cues[key] = out

    # Named phrase anchors for the cues that are not array-shaped.
    anchors = {}
    for key, items in cfg.get("anchors", {}).items():
        src = next(s for s in cfg["scenes"] if s["key"] == key)
        out = []
        for item in items:
            at = src["text"].find(item["phrase"])
            if at < 0:
                die("anchor %s is not in the %s narration: %r"
                    % (item["name"], src["id"], item["phrase"]))
            t = round(src["cstart"][at], 2)
            if t >= src["raw"]:
                die("anchor %s lands at %.2fs, past the %.2fs end of %s"
                    % (item["name"], t, src["raw"], src["id"]))
            out.append((item["name"], t, item["phrase"]))
        anchors[key] = out

    return {"speed": speed, "scenes": scenes, "total": total, "captions": cap_out,
            "cues": cues, "anchors": anchors, "byKey": by_key}


# -------------------------------------------------------------------- reader

def read_captions(cfg):
    """Caption text is hand-authored; collect it per scene from wherever that
    scene currently lives, so the order stays authoring order."""
    root = open(HTML).read()
    out = []
    for s in cfg["scenes"]:
        path = scene_file(s["key"])
        text = open(path).read() if path else root
        out += re.findall(r'class="cap[^"]*" id="(cap-%s-\d+)">([^<]*)</div>' % s["key"], text)
    return out


# ------------------------------------------------------------------- emitter

def block(name, body, indent):
    pad = " " * indent
    return "%s%s\n%s\n%s%s" % (pad, BEGIN % name, body, pad, END % name)


def emit(html, tl):
    scenes = tl["scenes"]

    # root window
    html = re.sub(r'(id="root"[^>]*?data-start=")[\d.]+(" data-duration=")[\d.]+"',
                  lambda m: "%s0%s%s\"" % (m.group(1), m.group(2), f3(tl["total"])),
                  html, count=1)

    for i, s in enumerate(scenes, 1):
        # scene window
        html, n = re.subn(
            r'(id="scene-%s"[^>]*?data-start=")[\d.]+(" data-duration=")[\d.]+"' % s["key"],
            lambda m, s=s: "%s%s%s%s\"" % (m.group(1), f3(s["start"]), m.group(2), f3(s["duration"])),
            html, count=1)
        if n != 1:
            die("could not place the scene window for %s" % s["key"])
        # audio placement
        html, n = re.subn(
            r'(id="a%02d"[^>]*?data-start=")[\d.]+(" data-duration=")[\d.]+"' % i,
            lambda m, s=s: "%s%s%s%s\"" % (m.group(1), f3(s["start"]), m.group(2), f3(s["duration"])),
            html, count=1)
        if n != 1:
            die("could not place the audio clip for %s" % s["key"])
        # scene header comment: keep the authored label, restate the range
        html, n = re.subn(
            r'(<!-- =+ %s · [^(]*?)\([\d.]+ -> [\d.]+\)' % s["key"][1:],
            lambda m, s=s: "%s(%s -> %s)" % (m.group(1), f3(s["start"]), f3(s["end"])),
            html, count=1)
        if n != 1:
            die("could not restate the header range for %s" % s["key"])

    # T map
    row = ", ".join("%s: %s" % (s["key"], f3(s["start"])) for s in scenes)
    html = replace_block(html, "T", 6,
                         "      const T = {\n        %s, end: %s,\n      };"
                         % (row, f3(tl["total"])))

    # phrase-anchored cue arrays and named anchors the root still owns
    for key in sorted(set(tl["cues"]) | set(tl["anchors"])):
        if scene_file(key):
            continue
        parts = []
        if key in tl["cues"]:
            rows = "\n".join('          ["%s", %s],' % (t, fs(v)) for t, v in tl["cues"][key])
            parts.append("        const CUES_%s = [\n%s\n        ];" % (key.upper(), rows))
        for name, value, phrase in tl["anchors"].get(key, []):
            parts.append('        const %s = %s;  // "%s"' % (name, fs(value), phrase))
        html = replace_block(html, "cues-" + key, 8, "\n".join(parts))

    # captions the root still owns (a scene that has been split out carries
    # its own captions inside its sub-composition instead)
    rows = [c for c in tl["captions"] if scene_file(c["scene"]) is None]
    body = ",\n".join('        {id:"%s", start:%s, end:%s}'
                       % (c["id"], fs(c["start"]), fs(c["end"])) for c in rows)
    html = replace_block(html, "captions", 8,
                         "        const CAPTIONS = [\n%s\n        ];" % body)
    return html


def emit_scene(text, tl, key):
    """Scene-local timing for a split-out scene. The sub-composition timeline
    starts at the scene's own t=0, so its captions are scene-relative."""
    base = tl["byKey"][key]["rawStart"]
    rows = [c for c in tl["captions"] if c["scene"] == key]
    body = ",\n".join('          {id:"%s", start:%s, end:%s}'
                       % (c["id"], fs(round(c["start"] - base, 3)),
                          fs(round(c["end"] - base, 3))) for c in rows)
    text = replace_block(text, "captions", 10,
                         "          const CAPTIONS = [\n%s\n          ];" % body)

    if key in tl["cues"] or key in tl["anchors"]:
        parts = []
        if key in tl["cues"]:
            rows = "\n".join('            ["%s", %s],' % (t, fs(v)) for t, v in tl["cues"][key])
            parts.append("          const CUES_%s = [\n%s\n          ];" % (key.upper(), rows))
        for name, value, phrase in tl["anchors"].get(key, []):
            parts.append('          const %s = %s;  // "%s"' % (name, fs(value), phrase))
        text = replace_block(text, "cues-" + key, 10, "\n".join(parts))
    return text


def replace_block(html, name, indent, body):
    pat = re.compile(r"[ \t]*%s\n.*?[ \t]*%s"
                     % (re.escape(BEGIN % name), re.escape(END % name)), re.S)
    if not pat.search(html):
        die("missing generated block markers for %r" % name)
    return pat.sub(lambda _: block(name, body, indent), html, count=1)


# ----------------------------------------------------------------- validator

STAGGER = re.compile(r"^([\d.]+)\s*\+\s*i\s*\*")


def validate_scene(text, scene, anchors):
    """A split-out scene's timeline runs on its own clock, so its cue offsets
    are bare S(...) literals rather than T.sNN + S(...)."""
    problems, checked, staggered = [], 0, 0
    for m in re.finditer(r"\bS\(([^)]*)\)", text):
        arg = m.group(1).strip()
        stagger = STAGGER.match(arg)
        if stagger:
            value, staggered = float(stagger.group(1)), staggered + 1
        elif re.fullmatch(r"[\d.]+", arg):
            value = float(arg)
        elif arg in anchors:
            value = anchors[arg]
        else:
            # a loop variable or caption field over compiler-generated values,
            # already bound-checked when those values were derived
            continue
        checked += 1
        if value >= scene["raw"]:
            line = text.count("\n", 0, m.start()) + 1
            problems.append("compositions/scene-%s.html:%d cue at +%.2fs is past the %.2fs "
                            "end of the scene" % (scene["key"][1:], line, value, scene["raw"]))
    return problems, checked, staggered


def validate(html, tl):
    """Bound-check the motion cues that stay hand-authored.

    A cue is authored in scene-relative RAW narration seconds inside S(...),
    so it must land inside that scene's raw narration length. This is the
    check that scene 04 needed: cues tuned to a 55.5s take silently fell past
    the end when the script was cut to 38s, and the Vol row never appeared.
    """
    anchors = {name: v for rows in tl["anchors"].values() for name, v, _ in rows}
    problems, checked, staggered, generated = [], 0, 0, 0
    for m in re.finditer(r"T\.(s\d\d) \+ S\(([^)]*)\)", html):
        key, arg = m.group(1), m.group(2).strip()
        scene = tl["byKey"].get(key)
        if scene is None:
            problems.append("unknown scene %s referenced at offset %d" % (key, m.start()))
            continue
        stagger = STAGGER.match(arg)
        if stagger:
            value, staggered = float(stagger.group(1)), staggered + 1
        elif re.fullmatch(r"[\d.]+", arg):
            value = float(arg)
        elif arg in anchors:
            value = anchors[arg]
        elif re.fullmatch(r"[A-Za-z_]\w*", arg):
            # a loop variable over a compiler-generated cue array, already
            # bound-checked when those values were derived
            generated += 1
            continue
        else:
            problems.append("%s: cue offset %r is not a plain number" % (key, arg))
            continue
        checked += 1
        if value >= scene["raw"]:
            line = html.count("\n", 0, m.start()) + 1
            problems.append("index.html:%d %s cue at +%.2fs is past the %.2fs end of the scene"
                            % (line, key, value, scene["raw"]))
    return problems, checked, staggered, generated


# ---------------------------------------------------------------------- main

def main():
    check = "--check" in sys.argv
    cfg, _ = load()
    tl = compile_timeline(cfg, read_captions(cfg))

    root_before = open(HTML).read()
    files = {HTML: (root_before, emit(root_before, tl))}
    split = []
    for s in cfg["scenes"]:
        path = scene_file(s["key"])
        if path:
            was = open(path).read()
            files[path] = (was, emit_scene(was, tl, s["key"]))
            split.append(s["key"])

    after = files[HTML][1]
    problems, checked, staggered, generated = validate(after, tl)
    anchor_values = {n: v for rows in tl["anchors"].values() for n, v, _ in rows}
    for key in split:
        found, n, st = validate_scene(files[scene_file(key)][1], tl["byKey"][key], anchor_values)
        problems += found
        checked += n
        staggered += st
    for p in problems:
        sys.stderr.write("timeline: %s\n" % p)

    if check:
        stale = any(was != now for was, now in files.values())
        print("timeline --check: %d scenes (%d split out), %d captions, %d cues "
              "bound-checked (%d staggered, %d generated)"
              % (len(tl["scenes"]), len(split), len(tl["captions"]),
                 checked, staggered, generated))
        if stale:
            sys.stderr.write("timeline: index.html is out of date; run `npm run timing`\n")
        return 1 if (stale or problems) else 0

    if problems:
        sys.stderr.write("timeline: refusing to write while cues are out of bounds\n")
        return 1
    written = 0
    for path, (was, now) in files.items():
        if was != now:
            open(path, "w").write(now)
            written += 1
    print("timeline: %.3fs total, %d scenes (%d split out), %d captions, %d cues "
          "bound-checked (%d staggered, base only; %d generated); %d file(s) written"
          % (tl["total"], len(tl["scenes"]), len(split), len(tl["captions"]),
             checked, staggered, generated, written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
