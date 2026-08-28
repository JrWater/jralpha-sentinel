# Submission form copy

Ready to paste into the lablab.ai submission form. Not yet posted anywhere —
drafted 2026-08-26, one of the still-outstanding items in
`PLAN_VS_ACTUAL.md` §6. Character/word counts verified against
`lablab.ai/delivering-your-hackathon-solution`'s stated limits.

## Project Title (max 50 chars — this is 26)

```
Sentinel: The Gates Decide
```

## Short Description / Summary (max 255 chars — this is 250)

```
An autonomous options trading agent where the LLM only proposes trades — deterministic safety gates decide. Built on Alpaca's Trading API, MCP server, and CLI, with a credential-free public dashboard showing every gate result and every refused proposal.
```

## Long Description (min 100 words — this is 331, word-count verified)

```
Most agents built for this hackathon will hand a language model a broker
tool and let it call it directly. Sentinel doesn't. The LLM reads ranked
candidates from four quant vectors — Trend, Catalyst, Event, and
Volatility — and writes a structured proposal. It holds no Alpaca
credentials and no order-submission tool. It cannot place a trade.

Deterministic safety gates across five dimensions decide instead. An
unregistered gate defaults to BLOCKING, never to a softer answer. A
durable safety permit binds every decision to the exact git commit and
policy hash that produced it, and expires in 90 minutes. The executor
accepts a proposal only if its order shape — always limit, never
market — is explicitly declared in the policy manifest; nothing else is
structurally submittable.

Every position is defined-risk — verticals, straddles, iron condors, never
a naked short — and every order is a limit order, because no market order
shape is declared anywhere in the manifest. Sizing is tournament-calibrated
rather than timid: a $12,000 hard cap per trade and $40,000 at risk across
the book, against a $12,000 daily kill switch and an Entry Maintenance
floor at $70,000 below which no new exposure opens while exits keep
running. Caps are fractions of *starting* equity, not current, so a
drawdown shrinks absolute risk instead of quietly staying as aggressive.
The discipline is structural, not small: the gates constrain what shape a
trade may take and whether it may open at all, and the model never gets a
vote on either.

On Alpaca's infrastructure: the Trading API for limit-only multi-leg
execution, real-time IEX prices blended with the free 15-minute-delayed
options snapshot (which only ever picks the strike by delta — Black-
Scholes on the real-time underlying sets the price), and both the MCP
server and CLI wired in. A credential-free public dashboard reads a
snapshot the agent writes every cycle, so the core claim — the model
proposes, the gates decide — is checkable in the record, not just
asserted.
```

## Technology & category tags

The exact taxonomy only shows up on the live form, but pick from:
`Alpaca`, `Trading Agent` / `Trading Bot`, `Finance`, `AI Agents`, `MCP`,
`Python`, `Options Trading`, `Autonomous Agent`. Prioritize whichever of
these are literal options on the form over close synonyms — judges filter
by tag.

## Fields this file does NOT cover

Pull these from elsewhere at actual submission time, not from this file:

- **Alpaca paper trading account ID** — `PA3K3A9ZBCBI` (from
  `policy/manifest.json`, required for judging).
- **Application URL** — https://jralpha-sentinel.streamlit.app
- **GitHub repo** — https://github.com/JrWater/jralpha-sentinel
- **Cover image** / **Slide presentation** / **Video presentation** — see
  `media/cover.png`, `media/slides.pdf`, `media/sentinel_demo.mp4`.
- **Social post links (up to 5)** — drafts in `docs/SOCIAL_POSTS.md`, not
  yet posted; the plan's schedule starts these on kickoff day (8/28), not
  before.
