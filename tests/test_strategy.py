#!/usr/bin/env python3
"""Unit tests for the strategy layer. No network, no broker, no secrets.

Run with:  .venv/bin/python -m pytest tests/test_strategy.py -q
"""
from __future__ import annotations

import copy
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from policy.loader import LossBudget, Manifest, load as load_manifest
from strategy.data import ChainContract, contract_symbol, parse_contract
from strategy.engine import EngineContext, run as run_engines
from strategy.indicators import (atr, black_scholes, bs_delta, ema, ivr,
                                 realized_vol, rsi)
from strategy.proposal import OptionLeg, Proposal
from strategy.regime import classify
from strategy.signals import score_symbol
from strategy.sizing import PortfolioState, fixed_quantity
from strategy.structures import (build_debit_vertical, build_iron_condor,
                                 build_straddle)


@pytest.fixture(scope="module")
def manifest():
    return load_manifest(Path(ROOT / "policy" / "manifest.json"))


def make_contract(symbol: str, *, bid: float, ask: float, delta: float,
                  iv: float = 0.30) -> ChainContract:
    parsed = parse_contract(symbol)
    assert parsed
    under, expiry, ctype, strike = parsed
    return ChainContract(symbol=symbol, expiration=expiry,
                         contract_type=ctype, strike=strike,
                         bid=bid, ask=ask, delta=delta, iv=iv,
                         quote_ts=None)


# ── indicators ───────────────────────────────────────────────────────────────

def test_ema_simple():
    assert abs(ema([1, 2, 3, 4, 5], 3) - 4.0625) < 0.001


def test_rsi_bounds():
    up = [float(i) for i in range(1, 40)]
    down = [float(40 - i) for i in range(0, 39)]
    assert rsi(up) > 70
    assert rsi(down) < 30


def test_realized_vol_positive():
    closes = [100.0 + i * 0.3 + (0.5 if i % 2 else -0.5) for i in range(60)]
    assert realized_vol(closes, 30) > 0.0


def test_black_scholes_call_gt_put_same_strike():
    c = black_scholes(100, 100, 0.05, 0.25)
    p = black_scholes(100, 100, 0.05, 0.25, is_call=False)
    assert c > 0 and p > 0
    assert abs(c - p) < 5.0  # near parity around the money


def test_ivr_scale():
    assert ivr(0.30, 0.20) == pytest.approx(0.5, rel=0.01)
    assert ivr(0.10, 0.20) == pytest.approx(-0.5, rel=0.01)
    assert ivr(None, 0.2) is None


def test_atr_positive():
    high = [100 + i for i in range(20)]
    low = [99 + i for i in range(20)]
    close = [99.5 + i for i in range(20)]
    assert atr(high, low, close, 14) > 0


# ── contracts ────────────────────────────────────────────────────────────────

def test_parse_contract_roundtrip():
    sym = contract_symbol("SPY", date(2026, 9, 4), "call", 770.0)
    assert parse_contract(sym) == ("SPY", date(2026, 9, 4), "call", 770.0)


# ── regime ───────────────────────────────────────────────────────────────────

def test_regime_uptrend_is_risk_on():
    spy = [100 + i * 0.5 for i in range(80)]
    qqq = [100 + i * 0.6 for i in range(80)]
    r = classify(spy, qqq, [0.5])
    assert r.mode == "risk_on"
    assert r.long_allowed


def test_regime_downtrend_is_risk_off():
    spy = [200 - i * 0.8 for i in range(80)]
    qqq = [200 - i * 0.9 for i in range(80)]
    r = classify(spy, qqq, [-0.5])
    assert r.mode == "risk_off"
    assert r.short_allowed


def test_regime_range_is_chop():
    import math
    spy = [200 + 0.8 * math.sin(i / 2.5) for i in range(80)]
    qqq = [200 + 1.0 * math.sin(i / 2.6 + 1) for i in range(80)]
    r = classify(spy, qqq, [0.0])
    assert r.mode == "chop"


# ── signals ──────────────────────────────────────────────────────────────────

def test_signal_scores_leader_above_benchmark():
    spy = [100 * (1.0004 ** i) for i in range(70)]
    leader = [100 * (1.0012 ** i) for i in range(70)]
    sig = score_symbol(leader, spy, leader, [c - 1 for c in leader], "NVDA")
    assert sig is not None and sig.score > 0.3


def test_signal_gap_detection():
    spy = [100 * (1.001 ** i) for i in range(70)]
    closes = [100 * (1.002 ** i) for i in range(69)] + [130.0]  # +8% day
    sig = score_symbol(closes, spy, closes, closes, "NVDA")
    assert sig is not None and sig.gap_dir == 1 and sig.gap_pct > 6.0


# ── structures ───────────────────────────────────────────────────────────────

def test_debit_vertical_priced_within_reason():
    exp = date(2026, 9, 4)
    contracts = [
        make_contract(contract_symbol("SPY", exp, "call", 765.0),
                      bid=2.0, ask=2.2, delta=0.55),
        make_contract(contract_symbol("SPY", exp, "call", 775.0),
                      bid=0.5, ask=0.6, delta=0.30),
        make_contract(contract_symbol("SPY", exp, "call", 785.0),
                      bid=0.1, ask=0.2, delta=0.12),
        make_contract(contract_symbol("SPY", exp, "put", 755.0),
                      bid=1.5, ask=1.7, delta=-0.45),
    ]
    p = build_debit_vertical(770.0, date(2026, 9, 3), exp, "SPY", 1,
                             contracts, 0.55, 0.15, 2)
    assert p is not None and p.max_loss_dollars > 0
    assert p.structure == "debit_vertical"
    assert len(p.legs) == 2


def test_straddle_has_two_legs():
    exp = date(2026, 9, 3)
    contracts = [
        make_contract(contract_symbol("LULU", exp, "call", 118.0),
                      bid=3.0, ask=3.4, delta=0.5),
        make_contract(contract_symbol("LULU", exp, "put", 118.0),
                      bid=3.1, ask=3.5, delta=-0.5),
    ]
    p = build_straddle(118.0, date(2026, 9, 2), exp, "LULU", contracts)
    assert p is not None and p.structure == "straddle"
    assert {leg.side for leg in p.legs} == {"buy"}


def test_iron_condor_is_four_legs():
    exp = date(2026, 9, 4)
    contracts = [
        make_contract(contract_symbol("SPY", exp, "put", 752.0),
                      bid=0.8, ask=0.9, delta=-0.16),
        make_contract(contract_symbol("SPY", exp, "put", 747.0),
                      bid=0.3, ask=0.4, delta=-0.10),
        make_contract(contract_symbol("SPY", exp, "call", 778.0),
                      bid=0.8, ask=0.9, delta=0.16),
        make_contract(contract_symbol("SPY", exp, "call", 783.0),
                      bid=0.3, ask=0.4, delta=0.10),
    ]
    p = build_iron_condor(770.0, date(2026, 9, 3), exp, "SPY", contracts)
    assert p is not None and len(p.legs) == 4
    assert p.max_loss_dollars > 0
    # credit structures carry a NEGATIVE limit (Alpaca mleg: credit < 0)
    assert p.limit_price < 0


# ── sizing ───────────────────────────────────────────────────────────────────

def test_sizing_honors_engine_cap(manifest):
    p = Proposal(engine="catalyst", underlying="LULU", direction="neutral",
                 structure="straddle", limit_price=3.5, max_loss_dollars=350.0,
                 legs=[OptionLeg("LULU260903C00118000", "buy", 1, 118.0,
                                 "call", date(2026, 9, 3))])
    state = PortfolioState(max_loss_by_underlying={}, max_loss_total=0.0,
                           count_by_engine={}, current_equity=100000,
                           starting_equity=100000)
    sized = fixed_quantity(p, manifest, "event_macro", state)
    # event_macro cap is 10% = $10,000; $350/contract -> 28 contracts
    assert sized is not None and sized.legs[0].quantity == 28
    assert sized.max_loss_dollars <= 10000.0


def test_sizing_refuses_when_one_contract_exceeds_cap(manifest):
    p = Proposal(engine="trend_directional", underlying="NVDA",
                 direction="long", structure="debit_vertical",
                 limit_price=5.0, max_loss_dollars=5000.0,
                 legs=[OptionLeg("x", "buy", 1, 1.0, "call", date(2026, 9, 4))])
    state = PortfolioState(max_loss_by_underlying={}, max_loss_total=0.0,
                           count_by_engine={}, current_equity=100000,
                           starting_equity=100000)
    assert fixed_quantity(p, manifest, "trend_directional", state) is None


def test_sizing_respects_at_risk_cap(manifest):
    p = Proposal(engine="catalyst", underlying="LULU", direction="neutral",
                 structure="straddle", limit_price=10.0, max_loss_dollars=1000.0,
                 legs=[OptionLeg("x", "buy", 1, 1.0, "call", date(2026, 9, 3))])
    state = PortfolioState(max_loss_by_underlying={},
                           max_loss_total=39600.0, count_by_engine={},
                           current_equity=100000, starting_equity=100000)
    sized = fixed_quantity(p, manifest, "catalyst", state)
    # at-risk cap 40% = $40,000; only $400 of headroom -> 0 contracts -> None
    assert sized is None


def test_open_risk_accumulates_across_siblings_in_one_cycle(manifest):
    """The at-risk cap is a cap on the BOOK, so two candidates opened in the
    same cycle cannot each spend the same headroom.

    The defect this pins: PortfolioState.max_loss_total was snapshotted from
    already-open positions at the top of the cycle and never moved again, so
    every sibling was measured against the same stale figure. With $35,000
    open under a $40,000 cap, six $5,000 candidates each saw $5,000 of room
    and all six could open — $65,000 against a cap the write-up, the slides
    and the narration all call hard.
    """
    from strategy.sizing import record_open_risk
    cap = (float(manifest.get("risk_caps", "at_risk_cap_fraction"))
           * float(manifest.get("environment", "required_starting_equity")))
    state = PortfolioState(max_loss_by_underlying={}, max_loss_total=35000.0,
                           count_by_engine={}, current_equity=100000,
                           starting_equity=100000)

    assert record_open_risk(state, 5000.0, cap) is True
    assert state.max_loss_total == 40000.0

    # headroom is spent; the sibling must be refused, not waved through
    assert record_open_risk(state, 5000.0, cap) is False
    # and a refusal must not consume budget it did not get
    assert state.max_loss_total == 40000.0


# ── engine ───────────────────────────────────────────────────────────────────

def test_engine_suppressed_on_final_date(manifest):
    from strategy.data import MarketState
    state = MarketState(equity=100000.0)
    ctx = EngineContext(
        state=state, manifest=manifest,
        regime=classify([100 + i * 0.5 for i in range(80)],
                        [100 + i * 0.6 for i in range(80)], [0.5]),
        now_et=datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc))
    assert ctx.is_final_date
    assert run_engines(ctx) == []


def test_engine_final_day_keeps_only_manifest_allowed_event_candidates(
        manifest, monkeypatch):
    from types import SimpleNamespace
    import strategy.engine as engine_module
    from strategy.data import MarketState

    events = [SimpleNamespace(label="event-nfp-gap", score=0.65),
              SimpleNamespace(label="event-unlisted", score=0.75)]
    monkeypatch.setattr(engine_module, "_event", lambda _ctx: events)
    ctx = EngineContext(
        state=MarketState(equity=100000.0), manifest=manifest,
        regime=classify([100 + i * 0.5 for i in range(80)],
                        [100 + i * 0.6 for i in range(80)], [0.5]),
        now_et=datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc))

    assert [candidate.label for candidate in run_engines(ctx)] == ["event-nfp-gap"]



def test_engine_never_emits_without_evidence(manifest):
    """An empty market state must produce zero candidates — never a guess."""
    from strategy.data import MarketState
    state = MarketState(equity=100000.0)
    ctx = EngineContext(
        state=state, manifest=manifest,
        regime=classify([100 + i * 0.5 for i in range(80)],
                        [100 + i * 0.6 for i in range(80)], [0.5]),
        now_et=datetime(2026, 9, 1, 11, 30))
    assert run_engines(ctx) == []


# ── v2.1: day-state gates ────────────────────────────────────────────────────

def test_daystate_risk_cap():
    from strategy.daystate import DayState, record_risk
    ds = DayState(date="2026-09-01", start_equity=100000.0)
    assert record_risk(ds, 4000.0, cap=6000.0)
    assert ds.new_risk_dollars == 4000.0
    assert not record_risk(ds, 2500.0, cap=6000.0)
    assert ds.new_risk_dollars == 4000.0  # refused, not added


def test_daystate_kill_switch_latches():
    from strategy.daystate import DayState, check_kill
    ds = DayState(date="2026-09-01", start_equity=100000.0)
    assert not check_kill(ds, 98000.0, 0.03)
    assert check_kill(ds, 96999.0, 0.03)
    assert check_kill(ds, 105000.0, 0.03)  # recovery does not unlatch


def test_daystate_scale_after_killed_day():
    from strategy.daystate import load_or_reset
    ds = load_or_reset({"date": "2026-09-01", "start_equity": 100000.0,
                        "killed": True},
                       today="2026-09-02", equity_now=99000.0)
    assert ds.scale == 0.5
    ds2 = load_or_reset({"date": "2026-09-01", "start_equity": 100000.0,
                         "killed": False},
                        today="2026-09-02", equity_now=99000.0)
    assert ds2.scale == 1.0


def test_daystate_fired_once():
    from strategy.daystate import DayState, fire_key, fired, mark_fired
    ds = DayState(date="2026-09-03", start_equity=100000.0)
    key = fire_key("catalyst", "LULU", "2026-09-03")
    assert not fired(ds, key)
    mark_fired(ds, key)
    assert fired(ds, key)


# ── v2.1: structure-level exits ──────────────────────────────────────────────

def _gv():
    from strategy.exits import GroupView
    return GroupView(
        group_id="catalyst:LULU:2026-09-04:150001", engine="catalyst",
        underlying="LULU", expiry="2026-09-04", kind="debit",
        entry_net=3.40, ref_amount=3.40,
        take_profit_fraction=0.80, stop_loss_fraction=0.45,
        event_exit_date="2026-09-04", event_exit_time="09:35",
        legs=[("LULU260904C00118000", "buy", 1),
              ("LULU260904P00118000", "buy", 1)])


def test_exit_decision_take_profit_and_stop_loss():
    from datetime import datetime, timezone
    from strategy.exits import decide_exit
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    assert decide_exit(_gv(), 3.0, now_et=now, final_date="2026-09-04",
                       flatten_at="10:45") == "take-profit $3"
    assert decide_exit(_gv(), -2.0, now_et=now, final_date="2026-09-04",
                       flatten_at="10:45") == "stop-loss $-2"
    assert decide_exit(_gv(), 0.5, now_et=now, final_date="2026-09-04",
                       flatten_at="10:45") is None


def test_exit_decision_post_event_time_stop():
    from datetime import datetime, timezone
    from strategy.exits import decide_exit
    before = datetime(2026, 9, 4, 9, 20, tzinfo=timezone.utc)
    after = datetime(2026, 9, 4, 13, 40, tzinfo=timezone.utc)
    assert decide_exit(_gv(), 0.0, now_et=before, final_date="2026-09-04",
                       flatten_at="10:45") is None
    assert decide_exit(_gv(), 0.0, now_et=after, final_date="2026-09-04",
                       flatten_at="10:45") == "post-event time-stop"


def test_exit_decision_final_flatten():
    from datetime import datetime, timezone
    from strategy.exits import GroupView, decide_exit
    now = datetime(2026, 9, 4, 14, 50, tzinfo=timezone.utc)  # 10:50 ET
    gv = GroupView(group_id="t", engine="trend_directional", underlying="SPY",
                   expiry="2026-09-04", kind="debit", entry_net=1.0,
                   ref_amount=1.0, take_profit_fraction=0.6,
                   stop_loss_fraction=0.5,
                   legs=[("SPY260904C00770000", "buy", 1)])
    assert decide_exit(gv, 0.1, now_et=now, final_date="2026-09-04",
                       flatten_at="10:45") == "final-day flatten"


def test_close_proposal_flips_sides_at_touch():
    from strategy.exits import build_close_proposal
    gv = _gv()
    touch = {"LULU260904C00118000": 5.0, "LULU260904P00118000": 0.4}
    close = build_close_proposal(gv, touch)
    assert len(close.legs) == 2
    assert {leg.side for leg in close.legs} == {"sell"}
    # receives 5.4 of credit -> Alpaca mleg convention: NEGATIVE price
    assert close.limit_price == -5.4


def test_single_leg_close_uses_a_positive_simple_limit():
    """A long option closes by selling at a positive bid, never a credit MLEG."""
    from strategy.exits import GroupView, build_close_proposal
    gv = GroupView(
        group_id="trend_single:NVDA:2026-08-28:153002",
        engine="trend_single", underlying="NVDA", expiry="2026-08-28",
        kind="debit", entry_net=0.95, ref_amount=0.95,
        take_profit_fraction=1.0, stop_loss_fraction=0.5,
        legs=[("NVDA260828C00225000", "buy", 31)],
    )

    close = build_close_proposal(gv, {"NVDA260828C00225000": 0.14})

    assert close.order_class == "simple"
    assert close.legs[0].side == "sell"
    assert close.limit_price == 0.14


def test_credit_structure_pnl_sign():
    from strategy.exits import pnl_of
    # entry: sold for 1.10 credit (entry_net negative); closing costs 0.40
    assert abs(pnl_of(-1.10, -0.40) - 0.70) < 1e-9
    # entry: paid 3.40 debit; now worth 4.00
    assert abs(pnl_of(3.40, 4.00) - 0.60) < 1e-9


# ── v2.1: single-leg gap play + sizing scale ─────────────────────────────────

def test_credit_vertical_price_is_negative():
    from datetime import date as d
    from strategy.structures import build_credit_vertical
    from strategy.data import contract_symbol
    exp = d(2026, 9, 4)
    contracts = [
        make_contract(contract_symbol("SPY", exp, "put", 755.0),
                      bid=2.0, ask=2.2, delta=-0.20),
        make_contract(contract_symbol("SPY", exp, "put", 750.0),
                      bid=1.0, ask=1.2, delta=-0.12),
    ]
    p = build_credit_vertical(766.0, d(2026, 9, 3), exp, "SPY", 1,
                              contracts, 0.20, 0.08, 5.0)
    assert p is not None and p.structure == "credit_vertical"
    assert p.limit_price < 0  # credit received => negative per mleg convention


def test_single_long_is_defined_risk():
    from datetime import date
    from strategy.structures import build_single_long
    from strategy.data import contract_symbol
    exp = date(2026, 9, 4)
    contracts = [
        make_contract(contract_symbol("SPY", exp, "call", 770.0),
                      bid=3.0, ask=3.2, delta=0.42),
        make_contract(contract_symbol("SPY", exp, "put", 755.0),
                      bid=2.0, ask=2.2, delta=-0.42),
    ]
    p = build_single_long(766.0, date(2026, 9, 4), exp, "SPY", 1,
                          contracts, 0.42, 0.10)
    assert p is not None and p.structure == "single_long"
    assert len(p.legs) == 1
    assert p.max_loss_dollars > 0
    assert p.max_gain_dollars is None  # uncapped upside


def test_sizing_scale_halves_cap(manifest):
    p = Proposal(engine="event_macro", underlying="SPY", direction="long",
                 structure="single_long", limit_price=4.0,
                 max_loss_dollars=400.0,
                 legs=[OptionLeg("x", "buy", 1, 770.0, "call",
                                 date(2026, 9, 4))])
    state = PortfolioState(max_loss_by_underlying={}, max_loss_total=0.0,
                           count_by_engine={}, current_equity=100000,
                           starting_equity=100000, scale=0.5)
    sized = fixed_quantity(p, manifest, "event_macro", state,
                           budget=LossBudget.GAP_ADDON)
    # add-on cap 8% = $8,000; halved = $4,000 -> 10 contracts
    assert sized is not None and sized.legs[0].quantity == 10


def test_straddle_expiry_must_follow_the_event():
    """An after-close report needs an expiry AFTER the event date."""
    from datetime import date as d
    from strategy.data import MarketState
    from strategy.engine import _expiry_after_event
    state = MarketState(equity=100000.0)
    state.chains["LULU"] = [
        ChainContract(symbol="LULU260903C00118000", expiration=d(2026, 9, 3),
                      contract_type="call", strike=118.0, bid=1.0, ask=1.2,
                      delta=0.5, iv=0.5, quote_ts=None),
        ChainContract(symbol="LULU260904C00118000", expiration=d(2026, 9, 4),
                      contract_type="call", strike=118.0, bid=1.2, ask=1.4,
                      delta=0.5, iv=0.5, quote_ts=None),
    ]
    assert _expiry_after_event(state, "LULU", d(2026, 9, 3), 4) == d(2026, 9, 4)


# ── the bar window must end at TODAY, not `days` ago ─────────────────────────

def test_bars_request_carries_no_limit():
    """Regression: `limit` silently returned the OLDEST bars in the window.

    Alpaca fills a limited window from `start` FORWARD. With
    start = now - 2*days and limit=days, the response was the oldest half:
    measured 2026-08-25, 2026-02-27..2026-07-08, a freshest bar 48 days old.
    The staleness guard in daily_bars then dropped every symbol, the engines
    refused for lack of data, and the regime read "insufficient history"
    forever. Nothing raised; the agent would have traded nothing all week.

    `limit` is also a total across the request rather than per symbol, so a
    five-symbol batch would have received ~18 bars each regardless.
    """
    import inspect

    from strategy import data as data_mod
    src = inspect.getsource(data_mod.AlpacaData._bars_batch)
    body = src.split('"""')[-1]          # ignore the explanatory docstring
    assert "limit=" not in body, (
        "_bars_batch must not pass `limit`: it truncates to the OLDEST bars "
        "in the window and silently starves every engine")


def test_bars_batch_returns_the_most_recent_days(monkeypatch):
    """The tail is taken client-side, so callers get the FRESHEST `days`."""
    from types import SimpleNamespace

    from strategy.data import AlpacaData

    bars = [SimpleNamespace(timestamp=f"day{i}") for i in range(200)]
    md = AlpacaData.__new__(AlpacaData)
    md.stocks = SimpleNamespace(
        get_stock_bars=lambda req: SimpleNamespace(data={"SPY": bars}))

    out = md._bars_batch(["SPY"], days=90)
    assert len(out["SPY"]) == 90
    assert out["SPY"][-1].timestamp == "day199"     # newest, not day89


# ── RSI must read the END of the series ─────────────────────────────────────

def test_rsi_reads_the_end_not_the_beginning():
    """Regression: rsi() looped range(1, period+1) — the FIRST 14 bars.

    A ramp-up test cannot catch this, because on a monotonic series the first
    fortnight looks like the last one. This series falls hard for 40 bars and
    then rises hard for 40, so the two ends disagree: the broken version
    reported deep oversold for a series ending in a rally.
    """
    from strategy.indicators import rsi

    falling_then_rising = ([100.0 - i for i in range(40)]
                           + [60.0 + i for i in range(40)])
    assert rsi(falling_then_rising, 14) > 70, "must reflect the closing rally"

    rising_then_falling = ([60.0 + i for i in range(40)]
                           + [100.0 - i for i in range(40)])
    assert rsi(rising_then_falling, 14) < 30, "must reflect the closing selloff"


def test_rsi_is_stable_as_history_grows():
    """The value must not depend on how much history the caller fetched.

    Measured on SPY 2026-08-25, the broken version read 87 on a 100-bar
    window, 33 on 120 and 56 on 150 — and the regime gate (spy_rsi >= 38)
    flipped risk_on to chop and back purely on that.
    """
    from strategy.indicators import rsi

    import random
    rng = random.Random(7)
    series = [100.0]
    for _ in range(400):
        series.append(series[-1] * (1.0 + rng.uniform(-0.02, 0.021)))

    values = [rsi(series[-n:], 14) for n in (100, 150, 200, 300, 400)]
    assert max(values) - min(values) < 0.5, (
        f"RSI drifted with window length: {[round(v, 1) for v in values]}")


def test_rsi_agrees_with_the_series_implementation():
    """rsi() and rsi_series()[-1] are the same number, by construction."""
    from strategy.indicators import rsi, rsi_series

    import random
    rng = random.Random(11)
    series = [50.0]
    for _ in range(200):
        series.append(series[-1] * (1.0 + rng.uniform(-0.03, 0.031)))
    assert abs(rsi(series, 14) - rsi_series(series, 14)[-1]) < 1e-9


def test_an_unbroken_rally_is_overbought_not_neutral():
    """Regression: avg_loss == 0 returned 50.0, dead neutral.

    A name up fourteen sessions in a row is the most overbought a series can
    be. Reporting it as 50 let it pass the `RSI <= 65` pullback filter — the
    one filter whose whole job is to reject names that already ran.
    """
    from strategy.indicators import rsi

    straight_up = [100.0 + i for i in range(40)]
    assert rsi(straight_up, 14) == 100.0

    straight_down = [200.0 - i for i in range(40)]
    assert rsi(straight_down, 14) == 0.0

    flat = [100.0] * 40
    assert rsi(flat, 14) == 50.0


# ── v3.0: ALL-IN conviction single-leg layer ────────────────────────────────

def test_trend_single_fires_on_high_conviction(manifest):
    """The conviction single-leg layer builds a defined-risk, uncapped-upside
    long. The entry filter itself is covered elsewhere; this test pins the
    structure, engine label and cap of the layer."""
    import math
    from datetime import date as d, datetime as dt, timezone, timedelta
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    from strategy.data import MarketState, ChainContract, contract_symbol
    from strategy.engine import EngineContext, _one_single
    from strategy.regime import classify
    from strategy.signals import Signal

    def fake_quote(bid, ask):
        return SimpleNamespace(bid_price=bid, ask_price=ask,
                               timestamp=dt.now(timezone.utc))
    def fake_bar(off, close):
        ts = dt(2026, 8, 25, 12, 0, tzinfo=timezone.utc) + timedelta(days=off)
        return SimpleNamespace(timestamp=ts, close=close,
                               high=close * 1.005, low=close * 0.995)

    nv = [130.0 + i * 0.6 + 2.5 * math.sin(i / 2.5) for i in range(80)]
    spy = [100.0 + i * 0.45 for i in range(80)]
    state = MarketState(equity=100000.0,
                        now_utc=dt(2026, 8, 31, 15, 30, tzinfo=timezone.utc))
    state.bars = {"SPY": [fake_bar(i - 80, c) for i, c in enumerate(spy)],
                  "QQQ": [fake_bar(i - 80, c * 1.1) for i, c in enumerate(spy)],
                  "NVDA": [fake_bar(i - 80, c) for i, c in enumerate(nv)]}
    last = nv[-1]
    state.latest = {"SPY": fake_quote(spy[-1] - 0.05, spy[-1] + 0.05),
                    "QQQ": fake_quote(spy[-1] * 1.1, spy[-1] * 1.1 + 0.1),
                    "NVDA": fake_quote(last - 0.05, last + 0.05)}
    atm = round(last, 1)
    cs = []
    for exp in [d(2026, 9, 1), d(2026, 9, 2)]:
        for ctype, sd in [("call", 1.0), ("put", -1.0)]:
            for step in range(-6, 7):
                k = atm + step
                cs.append(ChainContract(
                    symbol=contract_symbol("NVDA", exp, ctype, k),
                    expiration=exp, contract_type=ctype, strike=k,
                    bid=0.5, ask=0.7,
                    delta=max(-0.9, min(0.9, 0.5 * sd - 0.04 * step * sd)),
                    iv=0.25, quote_ts=None))
    state.chains["NVDA"] = cs
    regime = classify(spy, [c * 1.1 for c in spy], [0.5])
    ctx = EngineContext(state=state, manifest=manifest, regime=regime,
                        now_et=dt(2026, 8, 31, 11, 30,
                                  tzinfo=ZoneInfo("America/New_York")),
                        signals={})
    sig = Signal(symbol="NVDA", score=1.0, trend_pct=9.0, momentum_5d=3.0,
                 momentum_20d=15.0, rel_5d=2.0, rsi14=55.0, atr_pct=2.0,
                 gap_pct=0.0, gap_dir=0, reason="synthetic")
    single_cfg = manifest.get("strategies", "trend_single")
    cand = _one_single(ctx, sig, direction=1, single_cfg=single_cfg)
    assert cand is not None
    p = cand.proposal
    assert p.structure == "single_long" and len(p.legs) == 1
    assert p.engine == "trend_single"
    assert p.max_loss_dollars <= 3000.0


# ── v3.1.1: the conviction single-leg layer must follow the regime ──────────

def test_production_manifest_uses_v320_event_only_final_window(manifest):
    """The approved final-window policy removes trend without de-risking events."""
    assert manifest.get("version") == "3.2.0"
    assert manifest.get("strategies", "trend_directional", "enabled") is False
    assert manifest.get("strategies", "trend_income", "enabled") is False
    assert manifest.get("strategies", "trend_single", "enabled") is False
    assert manifest.get(
        "strategies", "catalyst", "pre_event_max_loss_per_trade_fraction") == 0.12
    assert manifest.get(
        "strategies", "event_macro", "max_loss_per_trade_fraction") == 0.10
    assert manifest.get(
        "strategies", "event_macro", "addon_max_loss_per_trade_fraction") == 0.08
    assert manifest.get(
        "strategies", "event_macro", "gap_max_entries_total") == 2


def _manifest_with_single_layer(manifest, *, enabled: bool) -> Manifest:
    """Exercise the strategy rule independently of the production kill switch.

    v3.2.0 disabled the whole trend vector in production; this helper also
    re-enables the vector's two engines so the single-leg MECHANISM remains
    testable while the production switch stays off."""
    raw = copy.deepcopy(manifest._raw)
    raw["strategies"]["trend_single"]["enabled"] = enabled
    raw["strategies"]["trend_directional"]["enabled"] = True
    raw["strategies"]["trend_income"]["enabled"] = True
    return Manifest(raw)

def _single_layer_scenario(manifest, *, spy_closes, breadth):
    """_trend() in a given regime, with one strong positive-score name.

    Deliberately drives _trend() end-to-end rather than calling _one_single
    directly: the v3.0 layer's bug was in the *selection* around the builder,
    not the builder, so a test that calls the builder cannot see it.
    """
    import math
    from datetime import date as d, datetime as dt, timedelta, timezone
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    from strategy.data import ChainContract, MarketState, contract_symbol
    from strategy.engine import EngineContext, _trend
    from strategy.regime import classify
    from strategy.signals import Signal

    def fake_quote(bid, ask):
        return SimpleNamespace(bid_price=bid, ask_price=ask,
                               timestamp=dt.now(timezone.utc))

    def fake_bar(off, close):
        ts = dt(2026, 8, 25, 12, 0, tzinfo=timezone.utc) + timedelta(days=off)
        return SimpleNamespace(timestamp=ts, close=close,
                               high=close * 1.005, low=close * 0.995)

    nv = [130.0 + i * 0.15 + 2.0 * math.sin(i / 2.5) for i in range(80)]
    state = MarketState(equity=100000.0,
                        now_utc=dt(2026, 8, 31, 15, 30, tzinfo=timezone.utc))
    state.bars = {
        "SPY": [fake_bar(i - 80, c) for i, c in enumerate(spy_closes)],
        "QQQ": [fake_bar(i - 80, c * 1.1) for i, c in enumerate(spy_closes)],
        "NVDA": [fake_bar(i - 80, c) for i, c in enumerate(nv)],
    }
    last = nv[-1]
    state.latest = {
        "SPY": fake_quote(spy_closes[-1] - 0.05, spy_closes[-1] + 0.05),
        "QQQ": fake_quote(spy_closes[-1] * 1.1, spy_closes[-1] * 1.1 + 0.1),
        "NVDA": fake_quote(last - 0.05, last + 0.05),
    }
    atm = round(last, 1)
    cs = []
    for exp in [d(2026, 9, 1), d(2026, 9, 2)]:
        for ctype, sd in [("call", 1.0), ("put", -1.0)]:
            for step in range(-6, 7):
                k = atm + step
                cs.append(ChainContract(
                    symbol=contract_symbol("NVDA", exp, ctype, k),
                    expiration=exp, contract_type=ctype, strike=k,
                    bid=0.5, ask=0.7,
                    delta=max(-0.9, min(0.9, 0.5 * sd - 0.04 * step * sd)),
                    iv=0.25, quote_ts=None))
    state.chains["NVDA"] = cs

    regime = classify(spy_closes, [c * 1.1 for c in spy_closes], breadth)
    sig = Signal(symbol="NVDA", score=1.0, trend_pct=2.0, momentum_5d=1.0,
                 momentum_20d=5.0, rel_5d=2.0, rsi14=55.0, atr_pct=2.0,
                 gap_pct=0.0, gap_dir=0, reason="synthetic")
    ctx = EngineContext(state=state, manifest=manifest, regime=regime,
                        now_et=dt(2026, 8, 31, 11, 30,
                                  tzinfo=ZoneInfo("America/New_York")),
                        signals={"NVDA": sig})
    return regime, _trend(ctx)


def test_single_leg_layer_does_not_buy_calls_in_risk_off(manifest):
    """v3.0 shipped this layer outside the regime dispatch with direction
    hardcoded long, so a falling tape produced a bullish single-leg — and
    since conviction() scales by abs(regime_score), the harder the selloff
    the more likely it was to fire. Reproduced at regime score -5.15 as a
    $2,988 long call that was the cycle's ONLY trend candidate."""
    regime, cands = _single_layer_scenario(
        _manifest_with_single_layer(manifest, enabled=True),
        spy_closes=[140.0 - i * 0.55 for i in range(80)],
        breadth=[-0.5])
    assert regime.mode == "risk_off" and not regime.long_allowed
    singles = [c for c in cands if c.proposal.engine == "trend_single"]
    assert singles == [], (
        f"a long single-leg fired in {regime.mode} "
        f"(score {regime.score}): {[c.proposal.thesis for c in singles]}")


def test_single_leg_layer_still_fires_in_risk_on(manifest):
    """The guard above must not have simply switched the feature off — the
    convexity layer is the point of the v3.0 profile. Same signal, same
    conviction, bullish regime: it must still fire."""
    regime, cands = _single_layer_scenario(
        _manifest_with_single_layer(manifest, enabled=True),
        spy_closes=[100.0 + i * 0.45 for i in range(80)],
        breadth=[0.5])
    assert regime.long_allowed
    singles = [c for c in cands if c.proposal.engine == "trend_single"]
    assert len(singles) == 1
    p = singles[0].proposal
    assert p.structure == "single_long" and len(p.legs) == 1
    assert p.legs[0].side == "buy" and p.legs[0].contract_type == "call"
    assert 0 < p.max_loss_dollars <= 3000.0


def test_single_leg_layer_is_disabled_in_the_production_manifest(manifest):
    """The temporary safety switch removes single-leg candidates entirely."""
    _regime, cands = _single_layer_scenario(
        manifest,
        spy_closes=[100.0 + i * 0.45 for i in range(80)],
        breadth=[0.5])
    assert [c for c in cands if c.proposal.engine == "trend_single"] == []


def test_daystate_release_hands_back_an_unsent_reservation():
    """record_risk reserves on the assumption the submit that follows works.

    When submit raises, nothing was sent and nothing is at risk; holding the
    budget would suppress later entries all session over a trade that never
    opened. Observed 2026-08-27 in the gate-refusal form: two refused NVDA
    candidates left new_risk_dollars at 4715 against zero positions.
    """
    from strategy.daystate import DayState, record_risk, release_risk
    ds = DayState(date="2026-09-01", start_equity=100000.0)
    assert record_risk(ds, 4000.0, cap=6000.0)
    release_risk(ds, 4000.0)
    assert ds.new_risk_dollars == 0.0
    assert record_risk(ds, 5000.0, cap=6000.0)   # budget is usable again


def test_daystate_release_never_goes_negative():
    from strategy.daystate import DayState, release_risk
    ds = DayState(date="2026-09-01", start_equity=100000.0)
    release_risk(ds, 1000.0)
    assert ds.new_risk_dollars == 0.0


def test_entry_budget_is_not_half_reserved_when_daily_cap_refuses():
    """A second-budget refusal must undo the first reservation immediately."""
    from scripts.run_cycle import reserve_entry_risk
    from strategy.daystate import DayState
    from strategy.sizing import PortfolioState

    portfolio = PortfolioState(
        max_loss_by_underlying={}, max_loss_total=35000.0,
        count_by_engine={}, current_equity=100000.0,
        starting_equity=100000.0)
    day = DayState(date="2026-09-01", start_equity=100000.0,
                   new_risk_dollars=29000.0)

    refused = reserve_entry_risk(
        portfolio, day, 2000.0,
        at_risk_cap=40000.0, exposure_cap=30000.0)

    assert refused == "daily"
    assert portfolio.max_loss_total == 35000.0
    assert day.new_risk_dollars == 29000.0


def test_entry_budget_portfolio_refusal_never_touches_day_budget():
    from scripts.run_cycle import reserve_entry_risk
    from strategy.daystate import DayState
    from strategy.sizing import PortfolioState

    portfolio = PortfolioState(
        max_loss_by_underlying={}, max_loss_total=39500.0,
        count_by_engine={}, current_equity=100000.0,
        starting_equity=100000.0)
    day = DayState(date="2026-09-01", start_equity=100000.0,
                   new_risk_dollars=5000.0)

    refused = reserve_entry_risk(
        portfolio, day, 1000.0,
        at_risk_cap=40000.0, exposure_cap=30000.0)

    assert refused == "portfolio"
    assert portfolio.max_loss_total == 39500.0
    assert day.new_risk_dollars == 5000.0
