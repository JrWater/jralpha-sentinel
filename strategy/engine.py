#!/usr/bin/env python3
"""The engine: turn a MarketState + policy into ranked candidate proposals.

Four vectors, each with a separate risk budget and position cap:

  Trend Vector   momentum/trend in the regime's direction (debit verticals,
                 or credit verticals when premium is rich)
  Catalyst Vector confirmed schedule catalysts: the LULU straddle, and
                 post-earnings drift on the prior week's big reporters
  Event Vector   the NFP 1-DTE strangle and the gap-day 0-DTE continuation
  Vol Vector     SPY iron condor when premium is rich and the tape is range-bound

Every candidate is a Proposal the gates can refuse; the engine never submits
anything. This file is deterministic and receives *all* its inputs, so it is
testable without a broker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from strategy import catalysts
from strategy.data import MarketState
from strategy.indicators import ivr, realized_vol
from strategy.proposal import Proposal
from strategy.regime import Regime, classify, universe_breadth
from strategy.signals import Signal, conviction, score_symbol
from strategy.sizing import PortfolioState, fixed_quantity
from strategy.structures import (atm_iv, build_credit_vertical,
                                 build_debit_vertical, build_iron_condor,
                                 build_single_long, build_straddle,
                                 build_strangle, pick_expiry)


@dataclass
class Candidate:
    proposal: Proposal
    score: float
    label: str


@dataclass
class EngineContext:
    state: MarketState
    manifest: object
    regime: Regime
    now_et: datetime
    signals: dict[str, Signal] = field(default_factory=dict)
    portfolio: PortfolioState | None = None

    def __post_init__(self):
        self.portfolio = self.portfolio or PortfolioState(
            max_loss_by_underlying={}, max_loss_total=0.0,
            count_by_engine={}, current_equity=self.state.equity,
            starting_equity=float(
                self.manifest.get("environment", "required_starting_equity")))

    @property
    def is_final_date(self) -> bool:
        return self.now_et.date().isoformat() == str(
            self.manifest.get("session", "final_trading_date"))

    @property
    def final_date_entry_frozen(self) -> bool:
        return (self.is_final_date and bool(
            self.manifest.get("session", "no_new_exposure_on_final_date")))


def run(ctx: EngineContext) -> list[Candidate]:
    """All candidates this cycle, ranked by score. Empty is a valid answer."""
    if ctx.final_date_entry_frozen:
        # The submission-day freeze stands, with exactly one pre-declared
        # exception: the 0-DTE NFP gap continuation (09:30-09:50 ET, hard
        # time-stop 10:40 ET). Nothing else may open on the final morning.
        return [c for c in _event(ctx) if c.label == "event-nfp-gap"]

    out: list[Candidate] = []
    out.extend(_trend(ctx))
    out.extend(_catalyst(ctx))
    out.extend(_event(ctx))
    out.extend(_vol(ctx))
    return sorted(out, key=lambda c: c.score, reverse=True)


# ── Trend Vector ─────────────────────────────────────────────────────────────

def _trend(ctx: EngineContext) -> list[Candidate]:
    cfg = ctx.manifest.get("strategies", "trend_directional")
    inc_cfg = ctx.manifest.get("strategies", "trend_income")
    if not cfg.get("enabled", True) or not inc_cfg.get("enabled", True):
        return []

    candidates = _score_signals(ctx)
    out: list[Candidate] = []
    filter_params = dict(
        rsi=float(cfg.get("max_rsi_entry", 65.0)),
        m5=float(cfg.get("max_momentum_5d_entry", 6.0)),
        m20=float(cfg.get("max_momentum_20d_entry", 25.0)),
    )

    def entry_ok(sig: Signal, direction: int) -> bool:
        # The pullback-entry filter: quality trend, but never after a blow-off.
        # Validated 2026-08-25: without it the score chases extended names and
        # the forward edge is negative on exactly the biggest runners.
        if direction > 0:
            return (sig.rsi14 <= filter_params["rsi"]
                    and abs(sig.momentum_5d) <= filter_params["m5"]
                    and sig.momentum_20d <= filter_params["m20"])
        return (sig.rsi14 >= 100 - filter_params["rsi"]
                and abs(sig.momentum_5d) <= filter_params["m5"]
                and sig.momentum_20d >= -filter_params["m20"])

    if ctx.regime.long_allowed:
        picks = [s for s in sorted(candidates, key=lambda s: s.score, reverse=True)
                 if s.score >= float(cfg["score_threshold"]) and entry_ok(s, 1)][: int(cfg["max_positions"])]
        for sig in picks:
            c = _one_trend(ctx, sig, direction=1, inc_cfg=inc_cfg)
            if c:
                out.append(c)
    elif ctx.regime.short_allowed:
        picks = [s for s in sorted(candidates, key=lambda s: s.score)
                 if s.score <= -float(cfg["score_threshold"]) and entry_ok(s, -1)][: int(cfg["max_positions"])]
        for sig in picks:
            c = _one_trend(ctx, sig, direction=-1, inc_cfg=inc_cfg)
            if c:
                out.append(c)
    elif _breakout(ctx, cfg):
        # Round-2 addition: a 20-day-high breakout overrides 'chop' for longs.
        # The market is at record highs and the biggest 5-day P&L scenario in
        # this window is a fresh-high continuation. One position, half
        # conviction, same pullback filter.
        picks = [s for s in sorted(candidates, key=lambda s: s.score, reverse=True)
                 if s.score >= float(cfg["score_threshold"]) and entry_ok(s, 1)][: 1]
        for sig in picks:
            c = _one_trend(ctx, sig, direction=1, inc_cfg=inc_cfg)
            if c:
                c.proposal.conviction = round(c.proposal.conviction * 0.6, 2)
                c.label = "trend-breakout"
                out.append(c)
    return out


def _breakout(ctx: EngineContext, cfg) -> bool:
    """SPY trading above its prior 20 sessions' high while the regime is chop."""
    if not bool(cfg.get("breakout_allow_long", False)):
        return False
    bars = ctx.state.bars.get("SPY", [])
    if len(bars) < 21:
        return False
    spot = _spot(ctx.state, "SPY")
    if spot is None:
        return False
    # exclude today's forming bar: the breakout is against PRIOR sessions
    prior_high = max(getattr(b, "high", 0.0) for b in bars[-21:-1])
    return prior_high > 0 and spot > prior_high


def _one_trend(ctx: EngineContext, sig: Signal, direction: int,
               inc_cfg) -> Candidate | None:
    cfg = ctx.manifest.get("strategies", "trend_directional")
    state = ctx.state
    s = state.latest.get(sig.symbol)
    if s is None:
        return None
    spot = _spot(state, sig.symbol)
    if spot is None:
        return None

    expiries = sorted({c.expiration for c in
                       state.contracts(sig.symbol)})
    expiry = pick_expiry(expiries, state.now_utc.date(),
                         int(cfg["min_dte"]), int(cfg["max_dte"]))
    if expiry is None:
        return None
    contracts = state.contracts(sig.symbol, expiry)

    iv = atm_iv(contracts, spot, state.now_utc.date())
    closes = [b.close for b in state.bars.get(sig.symbol, [])]
    rv = realized_vol(closes, 30) if len(closes) > 30 else 0.0
    richness = ivr(iv, rv) if iv else None

    # v2.4: credit is the PRIMARY structure — the 250-session model backtest
    # measured 87% wins / +$18.4k with $4.6k maxDD for credit spreads on this
    # exact signal, against 40% / +$15.7k / $14.1k for debit verticals. Debit
    # remains the fallback when the credit ladder cannot be built.
    proposal = build_credit_vertical(
        spot, state.now_utc.date(), expiry, sig.symbol, direction,
        contracts, float(inc_cfg["target_short_delta"]),
        float(inc_cfg["delta_tolerance"]), 5.0)
    engine = "trend_income"
    if proposal is None:
        proposal = build_debit_vertical(
            spot, state.now_utc.date(), expiry, sig.symbol, direction,
            contracts, float(cfg["target_long_delta"]),
            float(cfg["delta_tolerance"]), int(cfg["width_strikes"]))
        engine = "trend_directional"

    if proposal is None:
        return None
    proposal.engine = engine
    proposal.conviction = conviction(sig, ctx.regime.score)
    proposal.thesis = (f"Trend Vector: {sig.symbol} {sig.reason}; "
                       f"regime {ctx.regime.mode} ({ctx.regime.confidence:.2f}); "
                       f"IV richness {richness if richness is not None else 'n/a'}")
    sized = fixed_quantity(proposal, ctx.manifest, engine, ctx.portfolio)
    if sized is None:
        return None
    return Candidate(sized, sig.score, "trend")


# ── Catalyst Vector ──────────────────────────────────────────────────────────

def _catalyst(ctx: EngineContext) -> list[Candidate]:
    cfg = ctx.manifest.get("strategies", "catalyst")
    if not cfg.get("enabled", True):
        return []
    out: list[Candidate] = []
    today = ctx.now_et.date()
    entry_before = _hm(cfg.get("entry_before_et", "15:15"))

    # Pre-event straddle: LULU earnings 09-03 16:30 ET -> enter 09-02 only
    # (T-1). The event day itself is skipped (the straddle needs an expiry
    # AFTER the event), and the day after belongs to the exit, not a new entry.
    for cat in catalysts.major_catalysts():
        if cat.kind != "earnings" or not cat.underlying:
            continue
        if today != cat.date - timedelta(days=1):
            continue
        if (ctx.now_et.hour, ctx.now_et.minute) > entry_before:
            continue  # too late in the afternoon to pay a straddle
        cand = _earnings_straddle(ctx, cat.underlying, cat, cfg)
        if cand:
            out.append(cand)

    # PEAD: prior-week earnings gaps, entered once the gap held day one.
    for sym in catalysts.PRE_WINDOW_EARNINGS:
        sig = ctx.signals.get(sym)
        if sig is None or sig.gap_dir == 0:
            continue
        if abs(sig.gap_pct) < float(cfg["pead_gap_threshold"]) * 100.0:
            continue
        cand = _pead_vertical(ctx, sig, cfg)
        if cand:
            out.append(cand)
    return out


def _expiry_after_event(state, symbol: str, event_date, max_days: int):
    """First available expiry strictly AFTER the event date (pure, testable).

    US options expire at 16:00 ET on expiry day; an after-close report is
    released after any same-day option has already expired, so the straddle
    must live on the following expiry (for LULU on 09-03: the 09-04 weekly).
    """
    after = sorted({c.expiration for c in state.contracts(symbol)
                    if c.expiration > event_date
                    and (c.expiration - event_date).days <= max_days})
    return after[0] if after else None


def _earnings_straddle(ctx: EngineContext, symbol: str, cat, cfg) -> Candidate | None:
    state = ctx.state
    spot = _spot(state, symbol)
    if spot is None:
        return None
    # Round-2 fix: the expiry must be strictly AFTER the event. US options
    # expire at 16:00 ET on expiry day, and an after-close report (LULU
    # 09-03 16:30 ET) is released AFTER any 09-03 option has already expired.
    # The straddle therefore lives on the first expiry after the event date
    # (the Friday weekly), is entered on T-1, and is exited the next session.
    max_after = int(cfg.get("straddle_expiry_after_event_max_days", 4))
    expiry = _expiry_after_event(state, symbol, cat.date, max_after)
    if expiry is None:
        return None
    dte = (expiry - state.now_utc.date()).days
    if not (int(cfg["min_dte"]) <= dte <= int(cfg["max_dte"])):
        return None
    contracts = state.contracts(symbol, expiry)
    proposal = build_straddle(spot, state.now_utc.date(), expiry, symbol,
                              contracts, float(cfg["target_atm_delta"]), 0.06)
    if proposal is None:
        proposal = build_strangle(spot, state.now_utc.date(), expiry, symbol,
                                  contracts, float(cfg["target_atm_delta"]), 0.10)
    if proposal is None:
        return None
    proposal.engine = "catalyst"
    proposal.conviction = 0.75
    proposal.event_exit_date = (cat.date + timedelta(days=1)).isoformat()
    proposal.event_exit_time = str(cfg.get("post_event_exit_time_et", "09:35"))
    proposal.thesis = (f"Catalyst Vector: {symbol} {cat.name} on "
                       f"{cat.date} {cat.time_et} ET; ATM straddle expiring "
                       f"{expiry} (the expiry AFTER the event) to monetize the "
                       f"expected move, exited {proposal.event_exit_date} at "
                       f"{proposal.event_exit_time} ET. Confirmed event, "
                       f"defined risk.")
    sized = fixed_quantity(proposal, ctx.manifest, "catalyst", ctx.portfolio,
                           cap_key="pre_event_max_loss_per_trade_fraction")
    if sized is None:
        return None
    return Candidate(sized, 0.9, "catalyst-straddle")


def _pead_vertical(ctx: EngineContext, sig: Signal, cfg) -> Candidate | None:
    state = ctx.state
    s = state.latest.get(sig.symbol)
    if s is None:
        return None
    spot = _spot(state, sig.symbol)
    if spot is None:
        return None
    # drift leg: 0-3 DTE, direction of the gap
    expiries = sorted({c.expiration for c in state.contracts(sig.symbol)})
    expiry = pick_expiry(expiries, state.now_utc.date(),
                         int(cfg["min_dte"]), int(cfg["max_dte"]))
    if expiry is None:
        return None
    contracts = state.contracts(sig.symbol, expiry)
    dte = (expiry - state.now_utc.date()).days
    # widen delta target a bit for drift legs — we are buying continuation
    proposal = build_debit_vertical(
        spot, state.now_utc.date(), expiry, sig.symbol, sig.gap_dir,
        contracts, float(cfg["target_atm_delta"]) - 0.05, 0.10, 2)
    if proposal is None:
        return None
    proposal.engine = "catalyst"
    proposal.conviction = 0.7
    proposal.thesis = (f"Catalyst Vector (PEAD): {sig.symbol} gapped "
                       f"{sig.gap_pct:+.1f}% post-earnings and held it; "
                       f"buying the drift within {dte} DTE.")
    sized = fixed_quantity(proposal, ctx.manifest, "catalyst", ctx.portfolio,
                           cap_key="pead_max_loss_per_trade_fraction")
    if sized is None:
        return None
    return Candidate(sized, 0.8, "catalyst-pead")


# ── Event Vector ─────────────────────────────────────────────────────────────

def _event(ctx: EngineContext) -> list[Candidate]:
    cfg = ctx.manifest.get("strategies", "event_macro")
    if not cfg.get("enabled", True):
        return []
    out: list[Candidate] = []
    today = ctx.now_et.date()
    nfp = next((c for c in catalysts.CATALYSTS if c.id == "nfp"), None)
    if nfp is None:
        return []

    # Pre-event strangle: NFP is 09-04 08:30 ET; enter 09-03 with 1 DTE.
    if today == nfp.date - timedelta(days=1):
        if (ctx.now_et.hour, ctx.now_et.minute) >= (10, 5) and \
           (ctx.now_et.hour, ctx.now_et.minute) <= (15, 15):
            cand = _nfp_strangle(ctx, cfg)
            if cand:
                out.append(cand)

    # Gap-day continuation: 09-04, after the 09:30 open, before 09:50.
    if today == nfp.date and (9, 30) <= (ctx.now_et.hour, ctx.now_et.minute) <= (9, 50):
        cand = _nfp_gap_play(ctx, cfg)
        if cand:
            out.append(cand)
    return out


def _nfp_strangle(ctx: EngineContext, cfg) -> Candidate | None:
    state = ctx.state
    spot = _spot(state, "SPY")
    if spot is None:
        return None
    expiry = ctx.now_et.date() + timedelta(days=1)   # NFP day = expiry day
    contracts = state.contracts("SPY", expiry)
    proposal = build_strangle(spot, state.now_et.date(), expiry, "SPY",
                              contracts, float(cfg.get("target_delta", 0.42)),
                              0.10)
    if proposal is None:
        return None
    proposal.engine = "event_macro"
    proposal.conviction = 0.65
    # exit the morning after the report, at the first post-open cycle
    proposal.event_exit_date = expiry.isoformat()
    proposal.event_exit_time = str(cfg.get("post_event_exit_time_et", "09:35"))
    proposal.thesis = ("Event Vector: August Employment Situation tomorrow "
                       "08:30 ET; 1-DTE SPY strangle for the distribution, "
                       "structure-exited 09:35 ET after the report by order, "
                       "never by expiry.")
    sized = fixed_quantity(proposal, ctx.manifest, "event_macro", ctx.portfolio)
    if sized is None:
        return None
    return Candidate(sized, 0.7, "event-nfp-strangle")


def _nfp_gap_play(ctx: EngineContext, cfg) -> Candidate | None:
    """0-DTE SINGLE-LEG long in the direction of the NFP gap.

    Round-2 change: a 2-wide vertical caps the payoff at the width; a single
    long option keeps the upside uncapped while the risk stays defined at the
    debit paid. For a 0-DTE gap-continuation trade the asymmetry is the whole
    point — this is the one place in the policy where single-leg long is the
    right instrument. Hard time-stop at 10:40 ET (the final flatten).
    """
    state = ctx.state
    spot = _spot(state, "SPY")
    if spot is None:
        return None
    closes = [b.close for b in state.bars.get("SPY", [])]
    if len(closes) < 2:
        return None
    # during the session the latest daily bar is today's forming bar, so the
    # previous session close is closes[-2]; the gap is spot vs that close
    prev_close = closes[-2]
    gap_pct = spot / prev_close - 1.0
    threshold = float(cfg["gap_threshold"])
    if abs(gap_pct) < threshold:
        return None
    direction = 1 if gap_pct > 0 else -1
    expiry = ctx.now_et.date()   # 0 DTE today
    contracts = state.contracts("SPY", expiry)
    proposal = build_single_long(
        spot, ctx.now_et.date(), expiry, "SPY", direction,
        contracts, float(cfg.get("target_delta", 0.42)), 0.10)
    if proposal is None:
        return None
    proposal.engine = "event_macro"
    proposal.conviction = 0.6
    proposal.event_exit_date = ""
    proposal.event_exit_time = ""
    proposal.thesis = (f"Event Vector: NFP gap {gap_pct * 100:+.2f}% -> "
                       f"{'long' if direction > 0 else 'short'} 0-DTE SPY "
                       f"single-leg, time-stop 10:40 ET, risk = debit paid.")
    sized = fixed_quantity(proposal, ctx.manifest, "event_macro",
                            ctx.portfolio,
                            cap_key="addon_max_loss_per_trade_fraction")
    if sized is None:
        return None
    return Candidate(sized, 0.65, "event-nfp-gap")


# ── Vol Vector ───────────────────────────────────────────────────────────────

def _vol(ctx: EngineContext) -> list[Candidate]:
    cfg = ctx.manifest.get("strategies", "vol_income")
    if not cfg.get("enabled", True):
        return []
    if ctx.regime.mode != cfg.get("regime", "chop"):
        return []
    state = ctx.state
    spot = _spot(state, "SPY")
    if spot is None:
        return []
    closes = [b.close for b in state.bars.get("SPY", [])]
    rv = realized_vol(closes, 30) if len(closes) > 30 else 0.0
    expiries = sorted({c.expiration for c in state.contracts("SPY")})
    expiry = pick_expiry(expiries, state.now_utc.date(),
                         int(cfg["min_dte"]), int(cfg["max_dte"]))
    if expiry is None:
        return []
    contracts = state.contracts("SPY", expiry)
    iv = atm_iv(contracts, spot, state.now_utc.date())
    richness = ivr(iv, rv) if iv else None
    if richness is None or richness < float(cfg["min_ivr"]):
        return []
    proposal = build_iron_condor(spot, state.now_utc.date(), expiry, "SPY",
                                 contracts, float(cfg["target_short_delta"]),
                                 float(cfg["delta_tolerance"]), 5.0)
    if proposal is None:
        return []
    proposal.engine = "vol_income"
    proposal.conviction = 0.55
    proposal.thesis = (f"Vol Vector: SPY range-bound, premium rich "
                       f"(IVR {richness:.2f}); iron condor harvesting theta.")
    sized = fixed_quantity(proposal, ctx.manifest, "vol_income", ctx.portfolio)
    if sized is None:
        return []
    return [Candidate(sized, 0.6, "vol-condor")]


# ── shared helpers ───────────────────────────────────────────────────────────

def _score_signals(ctx: EngineContext) -> list[Signal]:
    return list(ctx.signals.values())


def _spot(state: MarketState, symbol: str) -> float | None:
    """Real-time underlying price: bid/ask midpoint, or last daily close.

    The IEX quote model carries bid_price/ask_price only - there is no
    close_price field. Taking the ask would bias every delta selection and
    Black-Scholes fair value toward the offer. The midpoint is the honest
    number, and the market_session gate already keeps us out of the auction
    where mid and edge disagree most.
    """
    q = state.latest.get(symbol)
    if q is not None:
        bid = getattr(q, "bid_price", None)
        ask = getattr(q, "ask_price", None)
        if bid and ask:
            val = (float(bid) + float(ask)) / 2.0
        elif bid:
            val = float(bid)
        elif ask:
            val = float(ask)
        else:
            val = 0.0
        if val > 0:
            return val
    bars = state.bars.get(symbol, [])
    if bars:
        val = float(getattr(bars[-1], "close", 0.0))
        return val if val > 0 else None
    return None


def _hm(value: str) -> tuple[int, int]:
    hh, mm = map(int, value.split(":"))
    return hh, mm
