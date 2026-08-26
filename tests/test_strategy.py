#!/usr/bin/env python3
"""Unit tests for the strategy layer. No network, no broker, no secrets.

Run with:  .venv/bin/python -m pytest tests/test_strategy.py -q
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from policy.loader import load as load_manifest
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
    # event_macro cap is 1.5% = $1,500; $350/contract -> 4 contracts
    assert sized is not None and sized.legs[0].quantity == 4
    assert sized.max_loss_dollars <= 1500.0


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
                           max_loss_total=12500.0, count_by_engine={},
                           current_equity=100000, starting_equity=100000)
    sized = fixed_quantity(p, manifest, "catalyst", state)
    # at-risk cap 13% = $13,000; only $500 of headroom -> 0 contracts -> None
    assert sized is None


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
