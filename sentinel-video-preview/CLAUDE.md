# HyperFrames Composition Project

## Skills — USE THESE FIRST

**Always invoke the relevant skill before writing or modifying compositions.** Skills encode framework-specific patterns (e.g., `window.__timelines` registration, `data-*` attribute semantics, shader-compatible CSS rules) that are NOT in generic web docs. Skipping them produces broken compositions.

**Doing anything with HyperFrames?** Start at `/hyperframes` — it tells you what HyperFrames can do and which skill or workflow handles your intent (make a video, TTS / BGM, prep footage, author / animate, render, install blocks), confirms your brief up front (the intent layer), and routes every "make me a…" request (a video, a deck, a composition port) to the right workflow. Read it first, especially when there's no project context to orient you. The workflows it routes to:

- `/product-launch-video` — any **website** URL or brief / script → a product launch / SaaS / promo video, or a site tour / showcase featuring the site's own captured visuals.
- `/faceless-explainer` — arbitrary text (topic / article / notes), **no URL, no website capture** → 60-90s faceless explainer.
- `/embedded-captions` — an existing talking-head video (MP4) → the same footage with captions / subtitles added (rail + embed, or pure-cinematic embed); the footage itself is untouched.
- `/talking-head-recut` — an existing talking-head / interview / podcast video (MP4) → the same footage **packaged with designed graphic overlays** (kinetic titles, lower-thirds, data callouts, pull-quotes, side panels, pip) synced to the transcript; the clip plays unchanged underneath. (Plain captions/subtitles → `/embedded-captions`.)
- `/pr-to-video` — a GitHub PR (URL / `owner/repo#N` / "this PR") → 30-90s code-change explainer (changelog / feature reveal / fix / refactor).
- `/motion-graphics` — a short (typically under 10s) design-led **motion graphic**, motion-is-the-message, no narration: kinetic type, a stat / number count-up, a chart, a logo sting, a lower-third / overlay, or an animated tweet / headline / captured-page highlight; rendered to MP4 or a transparent overlay. Longer / narrated / custom → `/general-video`.
- `/music-to-video` — a **music track** (audio file, video to pull audio from, or one generated from a mood brief) → beat-synced video (lyric / slideshow / kinetic promo). Music drives pacing; user-supplied images / videos are cut onto the same beat grid.
- `/slideshow` — a **presentation / pitch deck / interactive deck** — discrete slides, fragment reveals, branching, hotspot navigation, presenter mode. Output is a navigable deck, not a rendered video.
- `/general-video` — fallback for any other video (title card, longer brand / sizzle reel, multi-scene montage, static loop, custom composition) and the home of **companion mode** — co-create with the full HyperFrames toolbox; the original hyperframes authoring flow, any length.

**Porting an existing composition?** `/remotion-to-hyperframes` translates a Remotion (React) composition into HyperFrames HTML — a source migration, separate from the creation workflows above.

The domain skills (`/hyperframes-core`, `/hyperframes-animation`, `/hyperframes-keyframes`, `/hyperframes-creative`, `/hyperframes-cli`, `/media-use`, `/hyperframes-audio`, `/hyperframes-registry`, `/figma`) and the full capability map live inside `/hyperframes` — it is the single source of truth for which skill handles which intent.

**Changing how real footage or images look or reveal?** Load `/media-use` and read its `references/media-treatments.md` before editing, even when the request only says dark, flat, boring, retro, private, or “make the reveal cooler.” It governs how footage is treated, never whether media may be used. Use canonical media treatments and seek-safe motion; do not improvise equivalent CSS/SVG filters or overlays.

> **Tailwind v4 projects** (`hyperframes init --tailwind`): see `/hyperframes-core` → `references/tailwind.md`.

> **Skill missing or stale?** Run `npx hyperframes skills update <name>` to install/refresh
> the specific skill you need (the `/hyperframes` router does this automatically before
> entering a workflow), or bare `npx hyperframes skills update` to refresh the core set plus
> everything already installed — neither pulls the full set. Restart the agent session so
> newly installed skills load.

## Commands

```bash
npm run dev          # human-operated foreground preview (blocks until stopped)
npx hyperframes preview --background  # agent-safe persistent Studio preview
npx hyperframes preview --status      # verify the persistent preview is listening
npx hyperframes preview --stop        # stop it when review is finished
npm run check        # lint + runtime + layout + motion + contrast (one command)
npm run render       # render to MP4
npm run publish      # publish and get a shareable link
npx hyperframes lint --verbose  # include info-level findings
npx hyperframes lint --json     # machine-readable output for CI
npx hyperframes docs <topic> # reference docs in terminal
```

> **Agents must use `npx hyperframes preview --background` for Studio handoff.** Do not rely
> on a shell/tool `run_in_background` wrapper around `npm run dev`: that foreground process
> remains owned by the invoking session and can disappear while the browser stays open,
> leaving refreshes at `ERR_CONNECTION_TIMED_OUT`. Verify with `preview --status`, keep it
> alive through review, and stop it explicitly with `preview --stop` afterward.

> **Pinned CLI version.** These scripts pin an exact `hyperframes@X.Y.Z` so this project re-renders identically over time. Weeks later that pin lags fixes shipped since. To move up: `npx hyperframes@latest upgrade --project . --check` (shows the delta), then `npx hyperframes@latest upgrade --project .` to rewrite the pins. Always unpinned — the pinned script re-runs the old version against itself.

## Timing — narration is the source of truth

Every timing fact in `index.html` is compiled, not hand-typed. `tools/timeline.py`
reads `narration.json` plus the ElevenLabs alignment files in `assets/align/` and
emits the scene windows, audio placements, the `T` map, the scene header ranges,
and — into each scene module — that scene's caption cues and phrase-anchored
cues, on that scene's own local clock.

```bash
npm run timing         # recompile index.html from the narration
npm run timing:check    # verify it is current and every cue is in bounds
```

- **Never hand-edit anything between `<<< GENERATED` and `>>> GENERATED` markers**,
  and never hand-edit a `data-start` / `data-duration` on a scene or audio clip.
  Change `narration.json` (or re-record) and rerun `npm run timing`.
- Caption **text** stays hand-authored in the HTML; its **timing** is searched out
  of the alignment, so a re-record retimes captions automatically. A caption whose
  text no longer appears in the narration is a hard failure, not a silent drift.
- Scene starts and ends both derive from cumulative raw narration time divided by
  `speed`, so durations tile the root exactly — no 1ms gaps or overlaps.
- The 57 remaining motion cues are deliberately hand-authored (most are aesthetic —
  breathing, floating, staggers — not narration-anchored). They are **bound-checked**,
  not generated: `npm run timing:check` fails if a cue lands past the end of its
  scene. That is the failure that once left scene 04's Vol row invisible.

## Documentation

**For quick reference**, use the local CLI docs command (no network required):

```bash
npx hyperframes docs <topic>
```

Topics: `data-attributes`, `gsap`, `compositions`, `rendering`, `examples`, `troubleshooting`

**For full documentation**, discover pages via the machine-readable index — do NOT guess URLs:

```
https://hyperframes.heygen.com/llms.txt
```

## Project Structure

- `index.html` — host: shared stylesheet, the ten scene slots, the audio track
- `compositions/scene-01.html` … `scene-10.html` — one module per scene, each
  owning its markup, its own paused GSAP timeline on scene-local time, and its
  burned-in captions. Registered as `window.__timelines["scene-sNN"]`
- `narration.json` — authored narration metadata (the timing source)
- `tools/timeline.py` — the narration/timeline compiler
- `meta.json` — project metadata (id, name)

**Scene modules.** A scene's host slot and its file must agree on
`data-composition-id` (`scene-sNN`), and that id is also the timeline key. A
sub-composition timeline only drives its own subtree — it cannot reach host
elements — so all of a scene's motion lives in its own file.

Shared design tokens (`:root`, `.scene`, `.cap`, `.sketch-note`, …) stay in the
host stylesheet and still apply, because scene content is cloned into the host
document. A scene file's own `<style>` is scoped to its `data-composition-id`,
so put scene-specific CSS there and style its root with `#root`, never a class.

**Never add a `#scene-sNN::before` or `::after` rule.** `.scene::before` and
`.scene::after` already claim both pseudo-elements for the decorative frame and
the grain overlay. An id rule targeting the same pseudo-element merges with
them rather than replacing them: scene 08's badge did this and inherited
`.scene::before`'s `inset: 34px`, so it stretched into an opaque full-frame
cream box that hid the code screenshot for the whole scene. Add a real element
inside the scene module instead.

## Linting — ALWAYS RUN AFTER CHANGES

After creating or editing any `.html` composition, **always** run the full check before considering the task complete:

```bash
npm run check
```

Fix all errors before presenting the result. Warnings should be reviewed before rendering.

## Key Rules

1. Every timed element needs `data-start` and a duration. `data-start` is what marks it as timed; `data-track-index` is an optional Studio display lane the render never reads
2. Give timed visual elements `class="clip"`. The framework keys visibility off `data-start`, not the class, but the shared `.clip` CSS is what gives a scene its full-frame box, and `lint` warns without it
3. Timelines must be paused and registered on `window.__timelines`:
   ```js
   window.__timelines = window.__timelines || {};
   window.__timelines["composition-id"] = gsap.timeline({ paused: true });
   ```
4. Videos use `muted` with a separate `<audio>` element for the audio track
5. Sub-compositions use `data-composition-src="compositions/file.html"` to reference other HTML files
6. Only deterministic logic — no `Date.now()`, no `Math.random()`, no network fetches
