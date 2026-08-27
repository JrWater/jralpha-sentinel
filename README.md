# Sentinel

**An autonomous options trading agent whose language model cannot place an order.**

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon), 28 Aug – 4 Sep 2026. Runs on Alpaca paper trading with simulated funds.

---

## The claim

The usual shape of an LLM trading agent is: give the model a broker tool, let it call the tool. The model decides, and the model executes.

Sentinel splits those apart.

```
Claude ──> Proposal (structured, inert)
              │
              ▼
        16 gates, 5 dimensions, deterministic code
              │  BLOCKING / ATTENTION / INFO
              ▼
        Executor ──> Alpaca Trading API
```

The model has no broker credentials and no submission tool. It emits a proposal — a symbol, a structure, a stated maximum loss. Sixteen deterministic gates then decide whether that proposal is allowed to become an order. The model cannot argue with them, route around them, or be prompted out of them.

This matters because the failure mode of an LLM trader is not that it is wrong. It is that it is *confidently* wrong at 3× normal size, in a symbol it invented, using an order type nobody authorized. Every one of those is a gate here, and every one of them is refused at the boundary rather than discovered in the fill report.

## The five dimensions

A gate belongs to exactly one operational dimension, and its severity answers exactly one question: *it is red, so why should the agent not open new exposure right now?* A check that cannot answer that does not get to be BLOCKING.

| Dimension | Asks |
|---|---|
| **Process Health** | Did the machinery obey its operational contract? |
| **Data Readiness** | Is every input the decision needs actually present? |
| **Delivery Health** | If something breaks, will anyone find out? |
| **Release Integrity** | Is the running code the code that was verified? |
| **Entry Authority** | Is *this* account, in *this* mode, allowed to trade? |

Process Health and Data Readiness are deliberately separate. A data job can obey every operational contract — exit 0, no quota breach, no disk warning — and still leave the dataset incomplete. Treating "the job succeeded" as "the data is ready" is how an agent ends up making a confident decision on a half-loaded chain.

## Entry Maintenance

When evidence goes stale, Sentinel does not halt and it does not liquidate. It enters **Entry Maintenance**: new exposure is forbidden, while reconciliation, protective exits, and risk-reducing orders keep working normally.

Both halves matter. An agent that keeps trading on stale data is the obvious hazard. An agent that dumps its book the moment a feed hiccups is the less obvious one — panic-liquidating at a threshold is itself a strategy, and not one this policy authorizes.

## The manifest is the parameter authority

Every strategy parameter lives in [`policy/manifest.json`](policy/manifest.json). Code reads values from it and never restates them. The manifest is hashed into an identity like `SENTINEL-OPTIONS-V1@1.0.0+60661d355c69`, and that identity is stamped into every decision record and into the entry permit itself.

So "which parameters was the agent running when it made that trade?" is answerable from the trade record alone. A version string cannot do that — it is a promise a human made, and humans edit files without bumping versions. A SHA changes whether or not anyone remembered to.

One consequence, enforced in [`gates/safety_gate.py`](gates/safety_gate.py): a permit issued under one manifest SHA does not transfer to a different one. Edited parameters are a different experiment, not a continuation of the same one.

## Designing around a 15-minute delay

Alpaca's free tier serves options quotes from an indicative feed delayed 15 minutes. Rather than treat that as a handicap to route around, the policy is built to be insensitive to it:

- short strikes at ~0.13 delta, far enough out that a 15-minute-old chain selects the same contract
- 1–3 DTE verticals on names with every-weekday expirations, entered on limit orders
- **no declared order shape has `type: "market"`** — on a delayed feed a market order is a blank cheque, so the policy simply never declares one, and `order_shape_declared` makes that structural rather than advisory
- the freshness gate measures against the *declared* feed delay, not against zero, so it can tell "delayed as designed" from "the feed stopped" — only the second is a reason not to trade

## Strategy

**Quadrant** — four vectors, four risk budgets, one hard at-risk cap. Full
spec in [`docs/STRATEGY.md`](docs/STRATEGY.md); every parameter is in
`policy/manifest.json` (identity `SENTINEL-OPTIONS-V2@3.1.1+<sha>`, frozen
2026-08-26).

| Vector | Budget | Structure | When it fires |
|---|---|---|---|
| Trend | 45% | 0–2 DTE credit spreads in the regime's direction (debit verticals as fallback) + one conviction single-leg | Regime risk_on/risk_off + name score ≥ 0.55, RSI ≤ 65, not extended |
| Catalyst | 25% | LULU ATM straddle, expiring after the Sep-3 report (PEAD legs removed: 3-year drift measured negative) | Confirmed in-window calendar entries only |
| Event | 15% | 1-DTE SPY strangle for NFP (09-03); 0-DTE single-leg gap continuation (90% win in the 33-sample study), any-day ≥0.8% gaps, ≤2 per window | August Employment Situation + in-window gap days |
| Vol | 15% | SPY iron condor | Regime chop + IVR ≥ 0.25 |

Every structure is defined risk — no naked shorts, no market orders (declared
order shapes are limit-only). Caps are fractions of *declared starting*
equity, tournament-calibrated (v3.1): max loss per trade $800–$12,000 by
vector (hard cap $12,000), $40,000 portfolio at-risk **enforced at
submission**, daily kill switch at −$12,000, Entry Maintenance at $70,000. The
competition account is mechanically untradeable until kickoff
(`competition_window` gate) and everything is flattened by limit at the
touch before 10:45 ET on 09-04, the submission day — the one exception is
the pre-declared 0-DTE gap continuation (09:30–09:50 ET, ≤2 entries).

## Running it

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python alpaca-py streamlit pytest anthropic
cp .env.example .env      # fill in the competition account's keys
.venv/bin/python scripts/verify_account.py --compare-legacy
.venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/backtest_signals.py      # signal-quality check
.venv/bin/python scripts/run_cycle.py --dry-run   # gates + candidates, no writes
.venv/bin/python scripts/run_cycle.py             # live cycle (permit-bound)
.venv/bin/python scripts/status.py                # what a judge would see
```

`verify_account.py` runs first and is loud, because the competition rules are
unforgiving about the account: it must be brand new, start at exactly
$100,000, and carry options level 3. Getting that wrong is not a bug you
find on day three — it is a week of work that scores zero.

Schedule the live cycle with cron (closest 30-min marks inside the entry
window: 10:05/10:35/11:05/.../15:05 ET on weekdays 28 Aug – 4 Sep, plus the
final-day runs `09:35` and `10:45` on Sep 4 — the 10:45 run is the flatten).

**A bare `TZ=` line in a crontab does not retime the scheduler** — macOS's
cron (vixie-cron derived) evaluates every schedule field in the *system's*
local timezone regardless; `TZ=` only sets that variable in the job's own
environment. A crontab installed with the ET-literal numbers below and
`TZ=America/New_York` at the top will fire three hours off real ET on any
Mac whose system timezone is Pacific — confirmed from an actual fire during
build: the "15:05 ET" line ran at 15:05 PDT (18:05 ET, two hours after the
16:00 ET close). `run_cycle.py` itself is unaffected either way — it
computes `now_et` via `zoneinfo` on real UTC and never reads `$TZ` — so the
gates always judged the real market correctly; only the schedule's fire
times were off, which meant most cycles landed outside market hours
entirely.

The ET-intent schedule, for reference:

```cron
*/30 10-14 * * 1-5  cd ~/jralpha-sentinel && .venv/bin/python scripts/run_cycle.py >> logs/cycle.log 2>&1
5,35 15 * * 1-5     cd ~/jralpha-sentinel && .venv/bin/python scripts/run_cycle.py >> logs/cycle.log 2>&1
35 9 * * 1-5        cd ~/jralpha-sentinel && .venv/bin/python scripts/run_cycle.py >> logs/cycle.log 2>&1
45 10 * * 1-5       cd ~/jralpha-sentinel && .venv/bin/python scripts/run_cycle.py >> logs/cycle.log 2>&1
```

**Install the version matching your system's actual timezone**, not this one
verbatim. On a Pacific-timezone Mac (PDT, this project's build machine),
subtract 3 hours from every hour field instead:

```cron
*/30 7-11 * * 1-5   cd ~/jralpha-sentinel && .venv/bin/python scripts/run_cycle.py >> logs/cycle.log 2>&1
5,35 12 * * 1-5     cd ~/jralpha-sentinel && .venv/bin/python scripts/run_cycle.py >> logs/cycle.log 2>&1
35 6 * * 1-5        cd ~/jralpha-sentinel && .venv/bin/python scripts/run_cycle.py >> logs/cycle.log 2>&1
45 7 * * 1-5        cd ~/jralpha-sentinel && .venv/bin/python scripts/run_cycle.py >> logs/cycle.log 2>&1
```

Verify with `crontab -l` after installing, and sanity-check the very first
fire against `date` before trusting the rest of the schedule.

Research reads go through Alpaca's MCP server (`.mcp.json`), status through
the Alpaca CLI (`brew install alpacahq/tap/cli` then `alpaca account get`).

## Layout

```
policy/manifest.json     the single parameter authority
policy/loader.py         identity hashing, declared order shapes
gates/registry.py        gate metadata; six mandatory fields
gates/checks.py          the 16 checks and their rationales
gates/safety_gate.py     the durable, fail-closed entry permit
strategy/                indicators, regime, signals, catalysts, structures,
                         sizing, engine, data (Alpaca plumbing)
agent/                   executor (limit-only), ledger, LLM proposer
scripts/                 verify_account, run_cycle, backtest_signals, status
docs/                    STRATEGY.md, ONE_PAGE_WRITEUP.md, SOCIAL_POSTS.md
tests/                   what each gate is proven to refuse; strategy units
```

## Provenance

The gate architecture is ported from JrAlpha, a private personal trading system that has run against a broker paper account for months. What is published here is the *design* — the five dimensions, the mandatory-field registry, the fail-closed permit, the Entry Maintenance state — reimplemented for Alpaca and options. The strategy parameters, data sources, and broker bindings of that system are not part of this repository.

Several comments in this code describe specific ways the design has failed in practice: a severity default that produced a gate which could never block, and a name-based exemption that never matched anything but stood ready to disable a safety gate the day someone renamed a check. Both are locked by tests here. They are kept in the comments because a gate that stops stopping things does so silently, and the reasoning is the only thing that prevents the next person from reintroducing it.

## Licence

MIT. Paper trading only — nothing in this repository is investment advice, and options trading is not suitable for all investors.
