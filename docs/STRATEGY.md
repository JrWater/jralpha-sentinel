# Sentinel Quadrant — Strategy Specification

**Competition:** Alpaca AI Trading Agents Hackathon (lablab.ai × Alpaca), 28 Aug 2026 15:00 UTC – 4 Sep 2026 15:00 UTC.
**Account:** PA3K3A9ZBCBI (paper, $100,000.00, options level 3) — verified READY 2026-08-25.
**Policy id:** `SENTINEL-OPTIONS-V2@2.0.0` — every parameter lives in `policy/manifest.json`.

---

## The claim in one paragraph

The model proposes, the gates dispose, and the strategy never leaves the
defined-risk options alphabet: **four vectors, four risk budgets, one hard
at-risk cap.** Everything long is long *up to* a known dollar number, and the
only P&L sources in a five-day window are the ones that can be argued for:
trend continuation, scheduled earnings, scheduled macro, and expensive
premium. No naked shorts. No market orders. No 3am improvisation.

## The four vectors

| Vector | Budget | Trade | When it fires | Exit |
|---|---|---|---|---|
| **Trend** | 45% | 0–2 DTE debit verticals in the regime's direction (or credit verticals when IV is rich) | Regime = risk_on/risk_off AND name score ≥ 0.55 | +60% TP / −50% SL / time-stop |
| **Catalyst** | 25% | LULU ATM straddle (entered 09-02, **expiry 09-04 — the first expiry AFTER the after-close report**) + PEAD verticals on NVDA/CRM/CRWD gaps from the 08-26 earnings | Confirmed calendar entries only | +80% TP / −45% SL / post-event time-stop |
| **Event** | 15% | 1-DTE SPY strangle for NFP (entered 09-03, structure-exited 09:35 ET on 09-04); **0-DTE single-leg long** in the 09-04 gap direction (risk = debit, upside uncapped) — the ONE declared exception to the final-day freeze, entering 09:30–09:50 | BLS August Employment Situation | hard flat 10:40 ET on 09-04 |
| **Vol** | 15% | SPY iron condor, 0–2 DTE | Regime = chop AND IVR ≥ 0.25 | 50% TP / 2× credit SL |

## Why this wins a five-day *judged* window

1. **P&L is criterion #1** and the window has **six scheduled events** with
   real distribution: NVDA/CRM/CRWD earnings (08-26, feeding PEAD from 08-28),
   PCE (08-28), ISM/JOLTS (09-01), ADP (09-02), ISM Services + LULU earnings
   (09-03), NFP (09-04 08:30 ET). The agent is never directionally agnostic on
   a scheduled catalyst day.
2. **The theta-only plan cannot win:** selling 0.13-delta spreads on SPY/QQQ
   over 4.5 sessions realizes low single digits at best, and the P&L
   criterion is a leaderboard. The Trend and Catalyst vectors exist because
   the dispersion in this tape (as of 08-25: MSFT +25%/20d, PLTR +39%,
   COIN +28%/5d, AAPL −8.9%) is the raw material of a leaderboard.
3. **Every structure is defined-risk**, which is what the one-page write-up's
   "risk gates" section is judged on — and it is the honest answer, not a
   hedge. Max loss per structure is known at entry; the portfolio at-risk cap
   is $13,000; the Entry Maintenance trip is $92,000.
4. **The variance is intentional.** To *win* one of these you need the top of
   the P&L distribution, and top-of-distribution comes from the catalyst
   legs — but you never need a single leg to be right: four uncorrelated
   vectors, each capped, means the account survives being wrong twice.

## Signal definitions (all in `strategy/`)

- **Regime** (`regime.py`): SPY/QQQ vs EMA20/EMA50, 5-day momentum, RSI-14,
  universe breadth (fraction above EMA50). Output: risk_on / risk_off / chop
  with confidence. Longs require risk_on, shorts risk_off — **the agent does
  not fight the tape.**
- **Name score** (`signals.py`): 0.40·trend (vs EMA50) + 0.25·momentum (5d) +
  0.20·relative strength (5d vs SPY) + 0.15·RSI drift. The relative-strength
  term is what makes this a dispersion strategy rather than a beta trade.
- **Breakout override**: a SPY 20-day-high breakout overrides a chop regime for ONE long position at reduced conviction — the record-high tape's main continuation scenario.
- **Pullback-entry filter** (`engine.py`, manifest `max_*_entry` keys):
  score ≥ 0.55 AND RSI-14 ≤ 65 AND |5d momentum| ≤ 6% AND 20d momentum ≤ 25%.
  *Why: measured 2026-08-25 over ~120 sessions, then **re-measured** the same
  day after fixing the RSI defect described below.* The unfiltered score
  chased extended names — PLTR, COIN and MSFT all show *negative* forward
  edges (−2.4% to −3.1% at d3), which the re-measurement confirmed.

  Re-measured aggregate at the manifest's 0.55 threshold, 498 signals:
  **+0.22% (d1) / +0.51% (d2) / +0.75% (d3)** vs SPY. The edge is monotonic
  in the threshold (+0.57% → +0.75% → +0.84% at d3 for 0.45 / 0.55 / 0.65),
  which is the evidence that the score ranks rather than merely fires.

  **Two honest caveats, because a judge can re-run `scripts/backtest_signals.py`:**
  (a) An earlier version of this document claimed +1.25% at d3. That number
  was measured with a broken RSI-14 — it read the *first* fourteen bars of the
  series instead of the last, so the `RSI ≤ 65` filter was gating on a random
  historical fortnight. Corrected, the edge is about 60% of what was claimed,
  and one named standout (CRWD, +2.8% at d2) flips sign to −1.0%.
  (b) **95% of the positive edge comes from three names — DELL, AMD and MU.**
  Only 6 of 15 universe names show a positive d3 edge. This is one regime and
  a small sample; it is a reason to size the Trend vector as one of four
  budgets rather than to believe it in isolation.

  The filter is still what separates "buy the move" from "buy the move the
  market already finished" — it is just worth less than first measured.
- **Gap detector**: last close vs 20-day mean. |gap| ≥ 6% = post-earnings gap;
  the catalyst engine buys (or sells) the *drift*, not the gap.

## Structure selection (all in `structures.py`)

Strikes are chosen by **delta** (from the snapshot greeks), **priced with
Black-Scholes on the real-time IEX underlying** and the snapshot IV, and
submitted as **DAY limit orders at fair value with a touch cushion**. The
15-minute indicative option feed is therefore used for volatility and
selection, never for the price. This is the entire engineering answer to the
"delayed quotes" problem — and it is why the market_session gate forbids the
first and last 30 minutes (the only time when a delayed chain is also
*wrong-shaped*).

## Risk gates (the ones the write-up will brag about)

- Per-trade hard cap: **$2,000** (any engine; `risk_caps.max_loss_per_position_fraction`)
- Engine caps: trend $1,000 / $800, catalyst $2,000 (straddle) / $1,500 (PEAD), event $1,500 / $800, vol $800
- Portfolio at-risk cap: **$13,000** = 13% of starting equity, all open structures combined
- Concurrent positions ≤ 10; ≤ 3 structures (≤ 6 contracts) per underlying; ≤ 3 satellites per vector
- **Daily kill switch (enforced):** day P&L ≤ −$3,000 → no new entries for the rest of the day; next day's sizes ×0.5 (`strategy/daystate.py`)
- **Daily exposure cap (enforced):** max $6,000 of new max-loss submitted per day
- **Fire-once guards (enforced):** catalyst/event entries submit once per day per name — later cycles cannot double-buy
- **Structure-level exits (v2.1):** a multi-leg structure is marked and closed as ONE unit; an exit can never manufacture a naked short leg
- **Entry Maintenance:** equity < $92,000 → no new exposure at all; exits and reconciliation keep running
- **No market orders, ever** — declared order shapes are limit-only; `order_shape_declared` makes it structural
- **Account lock:** the competition account is mechanically untradeable before 2026-08-28 15:00 UTC (`competition_window` gate)
- Final day (09-04): no new exposure **except** the pre-declared 0-DTE NFP gap continuation (09:30–09:50 ET); everything flattened by **limit at the touch** before **10:45 ET** (deadline 11:00 ET)

## The AI layer (what the judges see)

`agent/proposer.py` — the LLM receives the ranked candidate list + regime +
portfolio state, selects at most 3, ranks them, and writes a one-sentence
thesis per selection. It holds **no credentials and no order tool**; its
output is validated against the candidate universe. If the model is
unavailable the agent falls back to the engine ranking — it degrades to
boring, not to broken. Research reads go through **Alpaca's MCP server**
(`.mcp.json`), ops/status through the **Alpaca CLI** (`alpaca account get`),
execution through the Trading API.

## Day-0 checklist (Aug 28, before 11:00 ET)

1. `python scripts/verify_account.py --compare-legacy` → all PASS (it did on 08-25)
2. `python scripts/run_cycle.py --dry-run` → competition_window gate shows BLOCK -> then READY at kickoff
3. `python scripts/run_cycle.py` every 30 min 10:05–15:25 ET via cron/launchd
4. Sep 3: LULU straddle 15:00–15:15 + NFP strangle, both 1 DTE
5. Sep 4: 09:30–09:45 NFP gap vertical; 10:45 aggressive flatten; 15:00 UTC submit

## Backtest (model-based simulation, 2026-08-26)

`scripts/backtest_strategy.py` replays the Trend Vector **as the engine trades
it**: regime gate, score threshold, pullback filter, top-3 ranking, $1,000
per-trade cap, $6,000 daily exposure cap, 10 concurrent, debit verticals
priced with Black-Scholes (IV proxied by trailing 30-day realized vol,
clamped 12–80%), TP/SL, expiry at 2-DTE intrinsic. Marks are daily closes
only. 250 sessions, 2025-12-08 .. 2026-08-26, 18 symbols.

| config | trades | win% | avg | total | maxDD |
|---|---|---|---|---|---|
| shipped v2.2 (Δ0.45, TP60%) | 279 | 37% | −$26 | **−$7.3k** | $24.0k |
| **shipped v2.3 (Δ0.40, TP40%)** | 283 | **64%** | **+$112** | **+$31.8k** | **$9.6k** |
| best sweep cell (Δ0.40, TP40%, SL40%) | 283 | 64% | +$112 | +$31.8k | $9.6k |

The direction is economically sensible, not a single lucky cell: lower delta
(0.40 > 0.45 > 0.55) and earlier profit-taking (40% > 60% > 80%) dominate
across the grid. This drove the v2.3 manifest change.

Signal-level companion (`scripts/backtest_signals.py`, now applying the same
shipped entry rules): the raw 3-day forward edge is **mixed by name** —
DELL +4.9%, AVGO +2.1%, AMD +1.4% vs NVDA −1.4%, META −2.0% — which is
exactly why the structure + exit timing, not the raw score, is where the
simulated P&L comes from.

**Caveats, stated plainly:** realized vol is a *proxy* for implied vol; marks
are daily (real cycles mark every 30 min, so TP/SL trigger more often in
both directions); no fills/slippage; the Catalyst/Event/Vol vectors have no
history to replay (they are the specific scheduled events of this window);
parameter selection on one 250-session sample carries overfit risk. The
number is a prior, not a promise — the same words that were true before the
strategy earned a simulated dollar.

## What is *not* claimed

No backtest can establish a "guaranteed" P&L, and anyone who promises one
is selling something. `scripts/backtest_signals.py` measures the signal
edge honestly (forward returns vs SPY); the rest is the structure — defined
risk, bounded drawdown, scheduled catalysts — that maximizes **expected
rank** in a judged, short, paper window.
