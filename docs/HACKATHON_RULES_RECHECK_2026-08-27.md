# Alpaca AI Trading Agents Hackathon — rules recheck (2026-08-27)

This note separates facts currently reproducible from first-party pages from
older repository notes and user-supplied evidence. It does not treat an old
plan as a current rule source.

## Bottom line

- The official event page currently identifies the online build period as
  **28 August–4 September 2026** and describes it as a seven-day online
  hackathon.
- The live first-party event page exposes the exact schedule: kickoff is
  **28 August 2026 at 08:00 PDT** and “End of Submissions” is
  **4 September 2026 at 08:00 PDT**. Its Google Calendar link independently
  encodes the same interval as `20260828T150000Z/20260904T150000Z`.
- Video, slides, cover image, public repository, demo platform and application
  URL are listed as **final-submission materials**. No first-party rule found
  in this review requires them to be finished or uploaded before kickoff.
- The checker rewrite and provenance system are internal evidence controls,
  not named competition deliverables. They need to be complete before the
  final submission relies on their claims, not as an eligibility condition for
  the first trading day.
- The existing video is a prepared repository artifact, **not a lablab
  submission**. The current repository explicitly says the form copy has not
  been posted anywhere.

## Evidence matrix

| Question | Finding | Evidence strength |
|---|---|---|
| Event dates | 28 August–4 September 2026 | First-party event page |
| Exact cutoff clock/timezone | 4 September 2026 at 08:00 PDT / 15:00 UTC | First-party live event page and its calendar link |
| Must presentation work be complete at kickoff? | No such requirement found. The event page lists it under “What to submit,” and separately encourages preparation before kickoff. | First-party event page; absence claim limited to pages checked |
| Video delivery channel | Part of the lablab final project submission, together with cover image and slides | First-party event page |
| Video format/content | Maximum five-minute MP4; introduction, PDF discussion, then functionality showcase | First-party submission guide text preserved from a browser verification in `PLAN_VS_ACTUAL.md`; direct guide currently renders no readable body to the web retriever |
| Current video already submitted? | No evidence of a lablab upload; repository says submission copy is “Not yet posted anywhere” | Repository evidence, not a lablab account-state check |

## 1. Dates and the Stage 2 boundary

The [official Alpaca event page](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
currently says:

> Online Hackathon | Dates: 28 August–4 September 2026

It also describes the format as online and seven days. The live rendered page
lists kickoff at 08:00 PDT on 28 August and “End of Submissions!” at 08:00 PDT
on 4 September. The page's calendar link encodes the same instants in UTC.

The same official page places these items under “What to submit”:

- project title, short description, long description, and tags;
- cover image, video presentation, and slide presentation;
- public GitHub repository, demo application platform, and application URL.

Consequently, the rules evidence supports this boundary:

- **System readiness for autonomous paper trading:** needed at the start of the
  trading period for the project's intended operation.
- **Video/copy/slides/cover:** final-submission materials, due with the final
  project submission by 4 September at 08:00 PDT / 15:00 UTC.
- **Deliverable checker and provenance:** project-defined safeguards, not
  separately named official deliverables. They should be finished before the
  project uses them to certify the final artifacts.

Nothing checked says the presentation package must be frozen before kickoff.
In fact, the official page tells participants that before kickoff they may read
the available material and “get a head start on your project.” This supports
preparation before the event; it does not create an early presentation
deadline.

Operational recommendation (not a rule): do not wait until the final calendar
day. Complete Stage 2 early enough to re-render, review the real output, and
upload while the authenticated submission form is still available.

## 2. When and how the video is submitted

The official event page lists “Video presentation” as part of the final
submission package on lablab.ai. It does not describe a separate pre-kickoff
video hand-in.

The first-party generic submission guide is
[Delivering your hackathon solution](https://lablab.ai/delivering-your-hackathon-solution).
Its rendered article was rechecked in a real browser and states:

> Video Presentation: A maximum 5-minute video in MP4 format. Begin with an
> introduction, discuss your PDF presentation, then showcase your project's
> functionalities.

The same guide lists the cover image, video, slide deck, public GitHub
repository, demo platform and application URL in its submission checklist.

Repository state establishes only artifact preparation:

- `media/sentinel_demo.mp4` first entered Git history on 25 August and its
  current tracked version was committed on 26 August.
- `docs/SUBMISSION_COPY.md` says the form copy was drafted on 26 August and is
  “Not yet posted anywhere.”
- A Git commit is not proof of a lablab upload. This review found no repository
  receipt, application URL, or other evidence that the video has been submitted
  to lablab.

Therefore the defensible answer is: **the video has been prepared, but should
be uploaded through the lablab final-submission flow with the rest of the
project package; it has not been proven submitted yet.** The current canonical
video should not be uploaded until its hard-coded gate count and other Stage 2
consistency issues are corrected.

## 3. Manual actions before kickoff

### Required participation/account checks

The official page says the event runs on the lablab platform and lablab Discord
and asks participants to register for both, enroll from the event page, and
participate in teams of one to six people. Before kickoff, the account owner
should visually confirm in the authenticated UI:

1. lablab enrollment is still active;
2. the one-person team exists and the user is its leader/member;
3. Discord registration/access works, so schedule corrections and sponsor
   announcements are visible;
4. the dedicated competition paper account ID is the one registered for the
   project and its starting equity remains exactly USD 100,000.

The rendered official event page explicitly requires a brand-new dedicated
Alpaca paper account for the final submission and a starting balance of exactly
USD 100,000. The user's email screenshot independently repeats those terms.
They should still be rechecked at kickoff because the official page warns that
event terms may change.

### Questions to resolve through the official schedule/Discord

These are clarification checks, not reasons to run a manual trade:

- the precise P&L measurement start and end convention;
- whether the one-account-per-email wording has any team-level implication;
- how open positions are valued at judging and whether an all-cash terminal
  state is required.

### What the user should not need to do

No rule found requires the user to manually invoke a trading command before the
event. A manual order would not prove autonomous scheduling and could damage
the required pristine competition-account state. Runtime readiness should be
verified by the system's no-order checks, schedule inspection, broker read-only
reconciliation, and fail-closed gates.

## 4. Evidence boundary and sources

### First-party sources checked

- [Alpaca AI Trading Agents Hackathon event page](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
  — date range, online format, participation guidance, submission categories,
  and technology theme.
- [lablab Hackathon Rules](https://lablab.ai/hackathon-rules) — current public
  retriever returned only an iframe shell; no event-specific deadline could be
  extracted.
- [Delivering your hackathon solution](https://lablab.ai/delivering-your-hackathon-solution)
  — rendered article rechecked in a real browser; video format and submission
  checklist confirmed.

### Supplemental, lower-level evidence

- `docs/COMPLIANCE.md` — repository-preserved event-page quotations.
- `docs/PLAN_VS_ACTUAL.md` — historical browser-verification record and prior
  readiness checklist; useful context, not current official truth.
- `docs/SUBMISSION_COPY.md` — proves the project's own draft/submission state,
  not lablab server state.
- User-provided Alpaca/lablab email screenshot — supports the dedicated fresh
  account and exact USD 100,000 requirement, but does not provide a submission
  cutoff time.
