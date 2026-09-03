---
name: Sentinel Whiteboard Explainer
colors:
  background: "#fafaf7"
  surface: "#f2f1ec"
  text: "#191a1d"
  muted: "#6b6f76"
  ink: "#191a1d"
  paper: "#ffffff"
  blue: "#2f5fd6"
  red: "#d43b3b"
  green: "#2f5fd6"
  amber: "#d43b3b"
typography:
  headline:
    fontFamily: Inter
    fontWeight: 800
  body:
    fontFamily: Inter
    fontWeight: 400
  note:
    fontFamily: Caveat, Georgia
    fontWeight: 700
motion:
  energy: moderate
  character: whiteboard marker draw-on, deliberate, precise
---

## Overview

Sentinel should feel like a presenter who just finished drawing this on a
clean whiteboard: a dry-erase marker, a little hand-shake in every line,
information laid out with maximum legibility and minimum ornament. This is a
**style pass only** — a preview render exploring a whiteboard-explainer
grammar in place of the notebook-and-paper grammar. Every fact, every number,
every second of narration timing carries over unchanged; only how it looks
changes.

Adapted from the `story-to-handdrawn-video` skill's `whiteboard-explainer`
recipe (best_for: 教程, 商业解释, 时间线, 因果关系 — tutorial, business
explanation, timeline, cause-and-effect — an exact match for this content).
That recipe targets a single illustrated panel; this composition translates
its grammar into a moving, narrated, 1920×1080 explainer, which the source
recipe does not itself attempt.

## Palette discipline (the hard constraint)

Two accents, used semantically, not decoratively:

- **Blue** = confirmed / passing / the gates deciding correctly. Replaces the
  old green.
- **Red** = risk / refused / a limit being enforced. Unchanged in meaning
  from before, just a marker-red instead of a coral.

No amber, no warm gold, no green. Everything else on screen is black ink on
a clean off-white board. Per-engine or per-vector identity is carried by
**label text and shape**, never by adding a third or fourth accent color —
color-coding six engines would both violate the two-accent discipline and
read as noisier, not clearer, than the whiteboard-explainer grammar intends.

## Components

- Cards are white panels with a medium, slightly hand-shaken black ink
  border — no paper-tan fill, no warm gradient wash, no offset colored
  shadow. A light, flat gray drop shadow is the only depth cue.
- Notes and callouts keep the Caveat hand-lettering (a marker-handwriting
  stand-in) but sit on white, not tan paper, with a black or single-accent
  underline instead of a taped paper-strip background.
- Replace notebook-specific decoration (torn-paper corners, tape marks,
  paper-strip frames) with whiteboard-specific decoration: hand-drawn circles
  around a number, an arrow pointing at a claim, a timeline stroke, a box
  drawn around a group of items. Every mark should look like it was just
  drawn, not like it was always printed there.
- Where an element already reveals by fading in, prefer a stroke draw-on
  (`stroke-dasharray`/`stroke-dashoffset` animated to 0) for outline shapes —
  it is the signature whiteboard-explainer motion and this project already
  has a working, tested implementation of it (see scene-01's callout). Do not
  invent a new technique; adapt the existing one per scene.
- The Sentinel robot mark keeps its silhouette and its bounded appearances
  (open and close only) but its accent switches from green to blue and its
  fill from paper-tan to white.

## Do's and Don'ts

- Do keep dashboards, code, account identifiers, limits, and evidence crisp
  and exactly as authored — this is a visual pass, not a content pass.
- Do keep motion deterministic and synchronized to narration; do not move,
  add, or remove a single timing offset.
- Do use the draw-on stroke technique for new/replacement decorative marks.
- Don't introduce a third accent color, a photographic whiteboard texture, a
  corner icon-library glyph, or dense annotation clutter — the source recipe
  explicitly rules these out.
- Don't turn a caption, a dollar figure, or an account number into
  handwriting; keep those in the crisp system font, unchanged from the
  current composition.
- Don't touch a GENERATED block, a caption id, or a `data-start`/`data-duration`.
</content>
