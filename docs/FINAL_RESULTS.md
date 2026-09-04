# Final Results — Alpaca AI Trading Agents Hackathon (2026)

> Filled in from the live account after the window closed, as the deck promised.
> Account PA3K3A9ZBCBI (paper, started exactly $100,000.00, kickoff 2026-08-28 15:00 UTC).

## Final state

| Metric | Value |
|---|---|
| Final equity (2026-09-04 08:00 PDT deadline) | **$98,667.19** |
| Total return | **−1.33%** |
| Intra-window equity path | $100,000 → $96,613 (Aug 31) → **$90,827 (Sep 2, trough −9.2%)** → $98,667 (Sep 4) |
| Positions at deadline | 1 × LULU 260904 C122 (value $0.00, expired worthless same day); everything else flat by limit order before 10:45 ET |
| Realized via live orders | All option positions closed by DAY limit orders — no market orders were ever emitted |

## The trades

* Trend vector legs (Aug 28 – Sep 2) — the drawdown to −9.2%. Cross-validated on four
  independent engines as noise (spot port: −6.65% vs SPY +23.9%, PF 0.92), the trend
  engine was disabled for the final window on evidence, per the owner's approval.
* **LULU straddle (Sep 2 entry, 8× C122/P122, expiring Sep 4 — the first expiry after
  the after-close report)** — the window's one confirmed earnings event. Q2 FY26 landed
  Sep 3 16:30 ET with the stock down ~17% overnight. The put side was closed Sep 4
  09:35 ET at the post-event structure exit (net $20.62 per spread): **realized ≈**
  **+$11.6k** on the put leg against −$4.4k on the (now worthless) call leg.
* NFP gap continuation (Sep 4 09:30–09:50 ET, $10k × 2 allowed) — **no entry**: the
  engine generated zero candidates; the opening gap did not reach the 0.8% threshold
  that the 33-sample study (9/10 continuation) conditions on. Discipline held: the best
  trade in the book does not exist when its precondition does not.

## Post-mortem honesty (same standard as the build)

* The signal-only research phase overestimated the credit-spread edge (87% → 69%
  roundtrip win on an independent engine, PF 1.06 after costs).
* A latent `state.now_et` crash cost the Sep 3 NFP strangle entry — found and fixed
  (453a102) with a regression test two hours before the final morning.
* The account never traded the competition account before kickoff, never used a market
  order, never naked-shorted, and never traded a position it had not gated.

## Artifacts (all in this repository)

* Live gate matrix history: jralpha-sentinel.streamlit.app
* Decision log: `state/decisions.jsonl` (order-level, thesis-per-trade)
* Strategy spec: `docs/STRATEGY.md` · One-page write-up: `docs/ONE_PAGE_WRITEUP.md`
* Cross-validation research: `/Users/jrwater/Documents/Alpaca AI Trading/research/vibe-trading/RESULTS.md`
