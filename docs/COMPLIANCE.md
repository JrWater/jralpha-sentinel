# Rule compliance — evidence, not claims

Every line below is reproducible from this repo. Where a rule can be checked
mechanically, the command that checks it is given; where it needs an artifact,
the artifact is named. Rules quoted from the event page,
<https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon>.

## Core requirements

**"Autonomous agents — participants must build autonomous AI trading agents
using Alpaca's Trading API."**
`cron` fires `scripts/cycle_window.py` on the 30-minute marks through the entry
window; it hands off to `scripts/run_cycle.py`, which runs the gates and calls
`agent/executor.py`. No human approves an order, and the language model has no
broker tool — it ranks candidates the quant engine already built. Verified end
to end today with real fills; see *Auto-trade path* below.

**"MCP or CLI — projects must utilize either Alpaca's MCP server or its CLI
tools."**
Both. The MCP server is declared in `.mcp.json` (`uvx alpaca-mcp-server`,
toolsets `accounts,trading,options,market,data`) for research and agent reads.
The Alpaca CLI carries ops and status:

```bash
alpaca account get -p competition
```

That one call is also the evidence for three of the account rules below —
account identity, freshness, and options level — so it is the check to run
first if anything looks wrong.

**"Options trading — all strategies must incorporate options trading."**
Every declared order shape in `policy/manifest.json` is an option structure:
vertical, straddle, strangle, iron condor, single long. No shape has
`type: "market"`, and none is undefined-risk. `gates/checks.py`'s
`order_shape_declared` refuses anything not on that list, so this is
structural rather than advisory.

## Account requirements

**"For your final submission, create a brand-new Alpaca paper trading account
dedicated to this hackathon. Projects run on an existing or reused account will
not be eligible for judging."**
`PA3K3A9ZBCBI`, `created_at: 2026-08-25T21:26:25Z` — opened for this event and
never traded before kickoff. Development ran on a separate legacy paper
account, which the rules explicitly allow ("Use any paper account you like
during development"). Two independent mechanisms keep them apart:
`check_account_identity` refuses any account that is not the declared one, and
`check_competition_window` makes the competition account untradeable until
`session.competition_starts_utc` (2026-08-28T15:00Z = 08:00 PDT).

**"Competition account starting balance must be set to $100,000."**
`equity: 100000` at kickoff, and `check_equity_floor` reads the floor as a
fraction of the *declared starting* equity rather than current equity, so a
drawdown shrinks absolute risk instead of rescaling the bet.

**"One-page write-up covering your AI logic, risk gates, and Alpaca
infrastructure implementation."**
`docs/ONE_PAGE_WRITEUP.md`. `scripts/check_deliverables.py` fails the build if
its risk figures disagree with `policy/manifest.json`.

## Auto-trade path — verified with real fills

Run on the isolated legacy paper account through the production
`run_cycle -> gates -> Executor` path (`scripts/test_paper_auto_cycle.py`,
which needs an explicit `--confirm-paper-order`):

```
open   MLEG  FILLED  net -0.63   SELL NVDA260828P00222500 @0.87 / BUY NVDA260828P00217500 @0.24
close  MLEG  FILLED  net  0.66   SELL NVDA260828P00217500 @0.23 / BUY NVDA260828P00222500 @0.89
ledger back to flat
```

That exercises what nothing else could: Alpaca accepting a multi-leg limit at a
negative price, the Executor's own authority check, and `manage_exits` — the
only exit path here, and the one the 09-04 flatten depends on.

`scripts/dress_rehearsal.py` covers the same chain against the *competition*
account without sending anything: the kickoff is backdated, `submit_order` is
recorded rather than sent, every disk write is stubbed, and the run fails if
any watched state file moves.

## Reproducing the checks

```bash
.venv/bin/python -m pytest tests/ -q          # full automated suite
.venv/bin/python scripts/check_deliverables.py # docs vs manifest
.venv/bin/python scripts/verify_account.py     # account vs the rules
.venv/bin/python scripts/dress_rehearsal.py    # full chain, nothing sent
alpaca account get -p competition              # CLI, and three account rules
```
