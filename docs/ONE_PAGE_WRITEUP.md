# Sentinel — Quadrant: A Defined-Risk Catalyst Hedgehog
### Alpaca AI Trading Agents Hackathon · lablab.ai × Alpaca · 28 Aug–4 Sep 2026

**One page. AI logic, risk gates, Alpaca infrastructure. Account PA3K3A9ZBCBI — flat at $100,000.00 at kickoff, options level 3.**

**Live dashboard:** https://jralpha-sentinel.streamlit.app · **Repo:** https://github.com/JrWater/jralpha-sentinel (MIT)

---

**The design claim.** The failure mode of an LLM trader is not that it is wrong — it is *confidently* wrong at 3× size, in a symbol it invented, with an order type nobody authorized. Sentinel splits the decision from the execution: **the model proposes, 16 deterministic gates dispose, a limit-only executor submits.** The model has no broker credentials and no order tool; an order it never creates cannot be hallucinated.

**AI logic.** A quant engine first: regime classification (SPY/QQQ vs EMA20/50, breadth, RSI) → per-name score (trend, 5d momentum, relative strength vs SPY, RSI drift) → gap detector (close vs 20-day mean). Four vectors consume that signal:
1. **Trend Vector (45%)** — 0–2 DTE debit verticals in the regime's direction, or credit verticals when IV exceeds 1.15× realized vol. Longs only in risk_on, shorts only in risk_off.
2. **Catalyst Vector (25%)** — confirmed in-window events only: LULU Q2 FY26 earnings (Sep 3, 16:30 ET) → ATM straddle entered Sep 2 on the 09-04 expiry (the first expiry after the report — a 09-03 option is already expired when the numbers hit), structure-exited 09:35 ET on Sep 4; post-earnings drift on NVDA/CRM/CRWD (reported Aug 26) → gap-direction verticals while the gap holds.
3. **Event Vector (15%)** — August Employment Situation (Sep 4, 08:30 ET): 1-DTE SPY strangle entered Sep 3, structure-exited 09:35 ET Sep 4; at the Sep 4 open, a 0-DTE single-leg long in the direction of a ≥0.6% gap — risk is the debit, upside uncapped; flat by 10:40 ET.
4. **Vol Vector (15%)** — SPY iron condors when the tape is range-bound and premium is rich.

The LLM layer (Claude, via `agent/proposer.py`) receives the ranked candidates, regime and portfolio state, selects ≤3, ranks them and writes the thesis — its selection is validated against the candidate universe, and with no model the agent falls back to engine ranking. It is a brain, never a finger.

**Risk gates (all enforced in code, none advisory).** Every position is defined-risk: verticals, straddles, iron condors — no naked shorts, no market orders (declared order shapes are limit-only). Per-trade max loss: $800–$2,000 by vector (hard cap $2,000 = 2% of the $100k account). Portfolio at-risk cap $13,000 across all open structures. ≤10 concurrent, ≤3 structures (≤6 contracts) per underlying. Daily kill switch: −$3,000 day-P&L halts new entries and halves next day's sizing; daily exposure cap $6,000; fire-once guards per name; structures are exited as units — never leg by leg. Entry Maintenance: below $92,000, no new exposure — exits and reconciliation keep running. Before kickoff the competition account is *mechanically* untradeable; on submission day (Sep 4) the only new exposure allowed is the pre-declared 0-DTE NFP gap continuation (09:30–09:50 ET), and a limit-at-the-touch flatten completes by 10:45 ET, so the judged number is fully realized.

**Alpaca infrastructure.** Trading API (execution: limit-only, multi-leg via `LimitOrderRequest` with legs, DAY TIF); Market Data IEX stocks in real time + indicative options snapshots 15-min delayed — strikes selected by snapshot delta, **priced with Black-Scholes on the real-time underlying and snapshot IV**, so delayed quotes never set the price; MCP server (research/agent reads, `.mcp.json`, `uvx alpaca-mcp-server`) and Alpaca CLI (ops/status: `alpaca account get`; `--dry-run` previews). Execution pipeline: `scripts/run_cycle.py` every 30 min through the entry window; the same cycle manages take-profit/stop-loss exits from the ledger's per-position metadata. The dashboard renders a credential-free snapshot the agent writes every cycle — gate matrix, every proposal and whether a gate refused it, open structures, equity curve — so "the model cannot place an order" is checkable in the record, not just asserted here.

**Why it can win.** There are six scheduled catalysts inside this exact window, and the entry post-earnings drift window on the biggest AI names (NVDA, CRM, CRWD — reported Aug 26) — the largest surprise-sensitive cohort of the season. The agent is never directionally agnostic on a catalyst day, never risks more than 2% of the account on a single idea, and never holds an undefined-risk structure. The theta-only plans score P&L in the low single digits; the lottery-ticket plans score risk gates at zero. A catalyst-adaptive, defined-risk hedgehog is the only strategy that can be in the top decile of P&L *and* at the top of the risk-policy table at the same time — which is where this competition is won.

*MIT licensed. Paper trading only. Not investment advice.*
