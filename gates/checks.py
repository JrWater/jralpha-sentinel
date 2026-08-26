#!/usr/bin/env python3
"""The gate checks themselves, and the registry instance that binds them.

Every check takes the evaluation context and returns a GateResult. No check
reads global state of its own: the context is passed in, so a test and a live
run take literally the same code path. That is not abstraction for its own
sake — a check that reaches for the real filesystem behind the caller's back
is a check your sandbox does not actually cover, and you find out in
production.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from gates.registry import Gate, GateResult, validate


@dataclass
class EvalContext:
    """Everything the gates are allowed to look at.

    Populated by the runner from the broker session and the local ledger.
    Absent values stay None and the gates fail closed on them — "we could not
    determine it" is treated as "not safe", never as "probably fine".
    """
    manifest: Any
    now_utc: datetime
    account: Any = None                 # Alpaca TradingAccount
    is_paper_session: bool | None = None
    clock: Any = None                   # Alpaca Clock
    positions: list = field(default_factory=list)
    ledger_positions: list | None = None
    option_quote_age_seconds: float | None = None
    underlying_bar_age_seconds: float | None = None
    decision_log_writable: bool | None = None
    git_head: str | None = None
    git_dirty: bool | None = None
    proposal: Any = None                # set for pretrade gates


def _f(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── preflight: may the agent form new exposure at all right now ──────────────

def check_manifest_identity(ctx: EvalContext) -> GateResult:
    m = ctx.manifest
    if m is None:
        return GateResult(False, "manifest not loaded")
    if not m.order_shapes:
        return GateResult(False, "manifest declares no order shapes")
    return GateResult(True, m.identity)


def check_broker_session(ctx: EvalContext) -> GateResult:
    if ctx.account is None:
        return GateResult(False, "no broker account snapshot")
    if getattr(ctx.account, "status", None) != "ACTIVE" and str(
            getattr(ctx.account, "status", "")) != "AccountStatus.ACTIVE":
        return GateResult(False, f"account status {ctx.account.status}")
    if getattr(ctx.account, "trading_blocked", False):
        return GateResult(False, "broker reports trading_blocked")
    return GateResult(True, f"account {ctx.account.account_number} ACTIVE")


def check_account_identity(ctx: EvalContext) -> GateResult:
    """The declared competition account, in paper mode. Both, every cycle.

    Order authority is never carried forward from one account to another. A key
    swapped in the environment must not silently inherit this policy's
    permission — it has to be the account the policy names.
    """
    if ctx.is_paper_session is not True:
        return GateResult(False, "session is not paper; this policy is "
                                 "PAPER-only by declaration")
    declared = ctx.manifest.get("environment", "competition_account_id",
                                default=None)
    if not declared:
        return GateResult(False, "manifest declares no competition_account_id")
    actual = getattr(ctx.account, "account_number", None)
    if actual != declared:
        return GateResult(False, f"account {actual} != declared {declared}")
    return GateResult(True, f"account {actual} matches declaration")


def check_competition_window(ctx: EvalContext) -> GateResult:
    """The competition account stays pristine until judging starts.

    The rules require the submitted account to start at exactly $100,000. Any
    development trade placed on it before kickoff destroys that, silently, and
    the damage is not discovered until a judge looks at the balance.

    The obvious control is "remember to load the dev .env when testing". That
    is a convention, and a convention that depends on a human not making a typo
    at 2am is not a control. This gate makes it mechanical: while the clock is
    before the declared start, the one account that may NOT be traded is
    precisely the competition account. Any other paper account is fine.
    """
    declared = ctx.manifest.get("environment", "competition_account_id",
                                default=None)
    actual = getattr(ctx.account, "account_number", None)
    if actual != declared:
        return GateResult(True, f"{actual} is not the competition account")
    starts = ctx.manifest.get("session", "competition_starts_utc", default=None)
    if not starts:
        return GateResult(False, "manifest declares no competition_starts_utc")
    start = datetime.fromisoformat(starts)
    if ctx.now_utc < start:
        remaining = start - ctx.now_utc
        return GateResult(False, f"competition account is pristine until "
                                 f"{start:%Y-%m-%d %H:%M}Z "
                                 f"({remaining.days}d {remaining.seconds//3600}h "
                                 f"away); trade the dev account instead")
    return GateResult(True, f"competition open since {start:%Y-%m-%d %H:%M}Z")


def check_options_level(ctx: EvalContext) -> GateResult:
    required = ctx.manifest.get("environment", "required_options_level")
    actual = getattr(ctx.account, "options_trading_level", None)
    if actual is None:
        return GateResult(False, "broker did not report options_trading_level")
    if int(actual) < int(required):
        return GateResult(False, f"options level {actual} < required {required}")
    return GateResult(True, f"options level {actual}")


def check_equity_floor(ctx: EvalContext) -> GateResult:
    """Below the floor: no new exposure, but exits keep working.

    Crossing this does not flatten the book. Panic-liquidating at a threshold
    is itself a strategy, and not one this policy authorizes.
    """
    start = float(ctx.manifest.get("environment", "required_starting_equity"))
    floor_frac = float(ctx.manifest.get("risk_caps", "equity_floor_fraction"))
    equity = _f(getattr(ctx.account, "equity", None))
    if equity is None:
        return GateResult(False, "no equity reported")
    floor = start * floor_frac
    if equity < floor:
        return GateResult(False, f"equity {equity:,.2f} below floor {floor:,.2f}"
                                 f" -> ENTRY MAINTENANCE")
    return GateResult(True, f"equity {equity:,.2f} >= floor {floor:,.2f}")


def check_market_session(ctx: EvalContext) -> GateResult:
    if ctx.clock is None:
        return GateResult(False, "no market clock")
    if not getattr(ctx.clock, "is_open", False):
        return GateResult(False, "market closed")
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(ctx.manifest.get("session", "timezone"))
    local = ctx.now_utc.astimezone(tz).time()
    open_after = ctx.manifest.get("session", "no_new_exposure_before")
    close_before = ctx.manifest.get("session", "no_new_exposure_after")
    hh, mm = map(int, open_after.split(":"))
    if (local.hour, local.minute) < (hh, mm):
        return GateResult(False, f"{local:%H:%M} before entry window {open_after}")
    hh, mm = map(int, close_before.split(":"))
    if (local.hour, local.minute) >= (hh, mm):
        return GateResult(False, f"{local:%H:%M} after entry window {close_before}")
    return GateResult(True, f"{local:%H:%M} inside entry window")


def check_underlying_data(ctx: EvalContext) -> GateResult:
    """Fresh bars matter only while the market is open.

    After the close the feed legitimately stops producing intraday bars and
    quote timestamps stop; the market_session gate already forbids entries
    then, so this gate reports PASS with a note instead of a misleading
    "feed stopped" red that the operator would learn to ignore.
    """
    clock_open = getattr(getattr(ctx, "clock", None), "is_open", None)
    if clock_open is False:
        return GateResult(True, "market closed; intraday freshness n/a")
    limit = float(ctx.manifest.get("data", "max_underlying_bar_age_seconds"))
    age = ctx.underlying_bar_age_seconds
    if age is None:
        return GateResult(False, "underlying bar age unknown")
    if age > limit:
        return GateResult(False, f"underlying bars {age:.0f}s old > {limit:.0f}s")
    return GateResult(True, f"underlying bars {age:.0f}s old")


def check_option_chain_data(ctx: EvalContext) -> GateResult:
    """Freshness measured against the *declared* feed delay, not against zero.

    The free indicative feed is a 15-minute-delayed OPRA derivative. Demanding
    sub-minute quotes here would keep the gate permanently red and teach us to
    ignore it. The threshold is delay + tolerance: past that, the feed has
    stopped, which is a different fact entirely. After the close (clock not
    open) the feed is legitimately silent, so the gate steps aside - entries
    are already forbidden by market_session.
    """
    clock_open = getattr(getattr(ctx, "clock", None), "is_open", None)
    if clock_open is False:
        return GateResult(True, "market closed; quote freshness n/a")
    limit = float(ctx.manifest.get("data", "max_option_quote_age_seconds"))
    age = ctx.option_quote_age_seconds
    if age is None:
        return GateResult(False, "option quote age unknown")
    if age > limit:
        return GateResult(False, f"option quotes {age:.0f}s old > {limit:.0f}s"
                                 f" -> feed stalled, not merely delayed")
    return GateResult(True, f"option quotes {age:.0f}s old (feed delay is 900s)")


def check_position_reconcile(ctx: EvalContext) -> GateResult:
    """Local ledger must agree with the broker before we add to the book.

    Disagreement means we do not know our own exposure. Sizing the next trade
    off a number we cannot confirm is how a risk cap becomes decorative.
    """
    if ctx.ledger_positions is None:
        return GateResult(False, "local ledger unavailable")
    broker = sorted((p.symbol, int(float(p.qty))) for p in ctx.positions)
    local = sorted((p["symbol"], int(p["qty"])) for p in ctx.ledger_positions)
    if broker != local:
        return GateResult(False, f"ledger {local} != broker {broker}")
    return GateResult(True, f"{len(broker)} position(s) reconciled")


def check_decision_log(ctx: EvalContext) -> GateResult:
    """If reporting is broken, a failure happens and nobody finds out."""
    if ctx.decision_log_writable is not True:
        return GateResult(False, "decision log not writable")
    return GateResult(True, "decision log writable")


def check_release_integrity(ctx: EvalContext) -> GateResult:
    """Is the running code the code that was verified?

    ATTENTION, not BLOCKING, and the calibration is deliberate. In the
    production system this design comes from, this gate blocks: that system
    trades one scheduled window a day and an unreviewed edit reaching it is
    unacceptable. Here the agent runs during an active seven-day build, where
    editing between cycles is the normal mode of work. A gate that would be red
    all week is a gate the operator learns to ignore, and an ignored BLOCKING
    gate is worse than an honest ATTENTION one. It still reports, and the dirty
    state is stamped into every decision record.
    """
    if ctx.git_head is None:
        return GateResult(False, "git head unknown")
    if ctx.git_dirty:
        return GateResult(False, f"worktree dirty at {ctx.git_head[:12]}")
    return GateResult(True, f"clean at {ctx.git_head[:12]}")


# ── pretrade: may THIS proposal become an order ──────────────────────────────

def check_order_shape_declared(ctx: EvalContext) -> GateResult:
    """An undeclared wire shape is refused before submission, not after.

    Note what is absent from the declared shapes: 'market'. On a 15-minute
    delayed feed a market order is a blank cheque, so the policy simply never
    declares one and this gate makes that structural.
    """
    p = ctx.proposal
    if p is None:
        return GateResult(False, "no proposal")
    shape = ctx.manifest.find_shape(
        order_class=p.order_class, type=p.type,
        time_in_force=p.time_in_force, legs=len(p.legs))
    if shape is None:
        return GateResult(False, f"undeclared shape: {p.order_class}/{p.type}/"
                                 f"{p.time_in_force}/{len(p.legs)}leg")
    return GateResult(True, f"shape {shape.id}")


def check_symbol_declared(ctx: EvalContext) -> GateResult:
    p = ctx.proposal
    declared = ctx.manifest.declared_symbols()
    if p.underlying not in declared:
        return GateResult(False, f"{p.underlying} not in declared universe")
    return GateResult(True, f"{p.underlying} declared")


def check_position_caps(ctx: EvalContext) -> GateResult:
    p = ctx.proposal
    max_total = int(ctx.manifest.get("risk_caps", "max_concurrent_positions"))
    max_per = int(ctx.manifest.get("risk_caps", "max_positions_per_underlying"))
    if len(ctx.positions) >= max_total:
        return GateResult(False, f"{len(ctx.positions)} positions >= cap {max_total}")
    same = sum(1 for pos in ctx.positions
               if getattr(pos, "symbol", "").startswith(p.underlying))
    if same >= max_per:
        return GateResult(False, f"{same} {p.underlying} positions >= cap {max_per}")
    return GateResult(True, f"{len(ctx.positions)}/{max_total} total, "
                            f"{same}/{max_per} on {p.underlying}")


def check_per_trade_risk(ctx: EvalContext) -> GateResult:
    """Caps are fractions of DECLARED STARTING equity, not current equity.

    A drawdown must shrink absolute risk. Sizing off current equity rescales
    the bet after a loss and quietly keeps the same relative aggression on the
    way down.
    """
    p = ctx.proposal
    start = float(ctx.manifest.get("environment", "required_starting_equity"))
    frac = float(ctx.manifest.get("risk_caps", "max_loss_per_position_fraction"))
    cap = start * frac
    risk = _f(getattr(p, "max_loss_dollars", None))
    if risk is None:
        return GateResult(False, "proposal does not state max_loss_dollars")
    if risk <= 0:
        return GateResult(False, f"implausible max loss {risk}")
    if risk > cap:
        return GateResult(False, f"max loss {risk:,.2f} > cap {cap:,.2f}")
    return GateResult(True, f"max loss {risk:,.2f} <= cap {cap:,.2f}")


def check_buying_power(ctx: EvalContext) -> GateResult:
    p = ctx.proposal
    bp = _f(getattr(ctx.account, "options_buying_power", None))
    if bp is None:
        bp = _f(getattr(ctx.account, "buying_power", None))
    if bp is None:
        return GateResult(False, "no options buying power reported")
    need = _f(getattr(p, "max_loss_dollars", None)) or 0.0
    if bp < need:
        return GateResult(False, f"buying power {bp:,.2f} < required {need:,.2f}")
    return GateResult(True, f"buying power {bp:,.2f} covers {need:,.2f}")


GATES = (
    Gate("manifest_identity", check_manifest_identity, "preflight",
         "BLOCKING", "Release Integrity",
         "Parameters must come from one machine-verifiable authority; a run "
         "whose manifest will not load has no defined semantics."),
    Gate("broker_session", check_broker_session, "preflight",
         "BLOCKING", "Entry Authority",
         "No session, no authority. Also catches broker-side trading blocks "
         "before we spend a decision cycle on a book we cannot touch."),
    Gate("account_identity", check_account_identity, "preflight",
         "BLOCKING", "Entry Authority",
         "Order authority is bound to one named account in paper mode. A key "
         "swapped in the environment must not inherit this policy's permit."),
    Gate("competition_window", check_competition_window, "pretrade",
         "BLOCKING", "Entry Authority",
         "The submitted account must start at exactly $100,000. A development "
         "trade placed on it before kickoff destroys that silently, and nobody "
         "finds out until a judge reads the balance."),
    Gate("options_level", check_options_level, "preflight",
         "BLOCKING", "Entry Authority",
         "A spread submitted to a level-2 account fails at the broker after we "
         "have already committed to one side of it."),
    Gate("equity_floor", check_equity_floor, "preflight",
         "BLOCKING", "Entry Authority",
         "The Entry Maintenance trip: below the floor the agent stops creating "
         "exposure while exits and reconciliation stay fully operational."),
    Gate("market_session", check_market_session, "preflight",
         "BLOCKING", "Process Health",
         "Entries are forbidden in the opening and closing 30 minutes, where a "
         "delayed chain is least representative of what will actually fill."),
    Gate("underlying_data", check_underlying_data, "preflight",
         "BLOCKING", "Data Readiness",
         "The directional signal reads underlying bars; stale bars produce a "
         "confident signal about a market that has moved on."),
    Gate("option_chain_data", check_option_chain_data, "preflight",
         "BLOCKING", "Data Readiness",
         "Distinguishes 'delayed as designed' from 'feed stopped'. Only the "
         "second one is a reason not to trade, and only this gate can tell."),
    Gate("position_reconcile", check_position_reconcile, "preflight",
         "BLOCKING", "Process Health",
         "If the ledger and the broker disagree we do not know our exposure, "
         "and every downstream risk cap is computed off a number we cannot "
         "confirm."),
    Gate("decision_log", check_decision_log, "preflight",
         "BLOCKING", "Delivery Health",
         "If reporting is broken, a failure happens and nobody finds out. That "
         "is the one failure mode that hides all the others."),
    Gate("release_integrity", check_release_integrity, "preflight",
         "ATTENTION", "Release Integrity",
         "Running code should be verified code. ATTENTION rather than BLOCKING "
         "because editing between cycles is the normal mode during an active "
         "build; the dirty state is stamped into every decision record."),

    Gate("order_shape_declared", check_order_shape_declared, "pretrade",
         "BLOCKING", "Release Integrity",
         "One declaration both builds an order and validates it, so an "
         "undeclared shape is refused before submission rather than discovered "
         "after. 'market' is deliberately never declared."),
    Gate("symbol_declared", check_symbol_declared, "pretrade",
         "BLOCKING", "Entry Authority",
         "The universe is a policy decision, not a model decision. An LLM that "
         "invents a ticker gets refused, not filled."),
    Gate("position_caps", check_position_caps, "pretrade",
         "BLOCKING", "Entry Authority",
         "Concentration limits are the difference between a diversified theta "
         "book and one gap-risk bet wearing twelve costumes."),
    Gate("per_trade_risk", check_per_trade_risk, "pretrade",
         "BLOCKING", "Entry Authority",
         "Caps are fractions of declared starting equity, so a drawdown "
         "shrinks absolute risk instead of merely rescaling it."),
    Gate("buying_power", check_buying_power, "pretrade",
         "BLOCKING", "Entry Authority",
         "A rejected order still consumes a decision cycle, and in a five-day "
         "competition cycles are the scarce resource."),
)

validate(GATES)
