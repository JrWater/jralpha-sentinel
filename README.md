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

A barbell, sized to a five-day judging window.

**Core (80% of risk budget)** — 1–3 DTE out-of-the-money vertical credit spreads on SPY and QQQ. Theta decay is the only P&L source in a five-day window that does not require predicting direction, and a vertical caps maximum loss at `width − credit` by construction. No single fill can threaten the account.

**Satellite (20%)** — long single-leg options, 14–35 DTE, on a momentum signal. Long premium only, so maximum loss is the debit paid. Convexity, bounded.

Hard caps, all fractions of *declared starting* equity rather than current equity, so a drawdown shrinks absolute risk instead of quietly rescaling the same aggression on the way down: $500 max loss per position, 12 concurrent positions, 3 per underlying, and an Entry Maintenance trip at $97,000.

## Running it

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python alpaca-py streamlit pytest anthropic
cp .env.example .env      # fill in the competition account's keys
.venv/bin/python scripts/verify_account.py --compare-legacy
.venv/bin/python -m pytest tests/ -q
```

`verify_account.py` runs first and is loud, because the competition rules are unforgiving about the account: it must be brand new, start at exactly $100,000, and carry options level 3. Getting that wrong is not a bug you find on day three — it is a week of work that scores zero.

## Layout

```
policy/manifest.json     the single parameter authority
policy/loader.py         identity hashing, declared order shapes
gates/registry.py        gate metadata; six mandatory fields
gates/checks.py          the 16 checks and their rationales
gates/safety_gate.py     the durable, fail-closed entry permit
agent/                   proposal generation and execution
dashboard/               live gate matrix
tests/                   what each gate is proven to refuse
```

## Provenance

The gate architecture is ported from JrAlpha, a private personal trading system that has run against a broker paper account for months. What is published here is the *design* — the five dimensions, the mandatory-field registry, the fail-closed permit, the Entry Maintenance state — reimplemented for Alpaca and options. The strategy parameters, data sources, and broker bindings of that system are not part of this repository.

Several comments in this code describe specific ways the design has failed in practice: a severity default that produced a gate which could never block, and a name-based exemption that never matched anything but stood ready to disable a safety gate the day someone renamed a check. Both are locked by tests here. They are kept in the comments because a gate that stops stopping things does so silently, and the reasoning is the only thing that prevents the next person from reintroducing it.

## Licence

MIT. Paper trading only — nothing in this repository is investment advice, and options trading is not suitable for all investors.
