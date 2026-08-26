#!/usr/bin/env python3
"""Per-name signal scoring: trend, momentum, relative strength, gap detection.

The signal layer is deliberately small and deterministic. It does not decide
*what* to trade; it ranks what the Trend Vector may consider, and it measures
the inputs the Catalyst Vector needs (pead gaps). Everything here is
unit-testable from a dict of closes with no network.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from strategy.indicators import ema, pct_change, rsi, norm_sigmoid


@dataclass
class Signal:
    symbol: str
    score: float            # signed, roughly [-1, +1]; >0 = long bias
    trend_pct: float        # vs EMA50, %
    momentum_5d: float      # %
    momentum_20d: float     # %
    rel_5d: float           # vs SPY 5d, %
    rsi14: float
    atr_pct: float
    gap_pct: float          # last close vs 20d mean close, %  (PEAD raw input)
    gap_dir: int            # +1 | -1 | 0 — PEAD direction
    reason: str = ""

    @property
    def optionable(self) -> bool:
        return True


def score_symbol(closes: list[float], spy_closes: list[float],
                 highs: list[float], lows: list[float],
                 symbol: str) -> Signal | None:
    """Score one name. Returns None when history is too short to trust."""
    if len(closes) < 60 or len(spy_closes) < 60:
        return None

    spy_5d = pct_change(spy_closes, 5)
    spy_20d = pct_change(spy_closes, 20)

    sp = {
        "trend": max(-0.08, min(0.08, closes[-1] / ema(closes, 50) - 1.0)) / 0.04,
        "momentum": max(-0.12, min(0.12, pct_change(closes, 5) / 100.0)) / 0.06,
        "rel": max(-0.15, min(0.15,
                              (pct_change(closes, 5) - spy_5d) / 100.0)) / 0.06,
        "rsi_trend": (rsi(closes, 14) - 50.0) / 40.0,
    }
    score = (0.40 * sp["trend"] + 0.25 * sp["momentum"]
             + 0.20 * sp["rel"] + 0.15 * sp["rsi_trend"])

    rsi14 = rsi(closes, 14)
    atr_val = 0.0
    if len(highs) >= 2 and len(lows) >= 2:
        trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                   abs(lows[i] - closes[i - 1])) for i in range(1, min(len(closes), len(highs)))]
        atr_val = sum(trs[-14:]) / max(len(trs[-14:]), 1)

    mean20 = sum(closes[-20:]) / 20.0 if len(closes) >= 20 else closes[-1]
    gap_pct = (closes[-1] / mean20 - 1.0) * 100.0
    gap_dir = 1 if gap_pct >= 6.0 else (-1 if gap_pct <= -6.0 else 0)

    reason = (f"t={sp['trend']:+.2f} m={sp['momentum']:+.2f} "
              f"rel={sp['rel']:+.2f} rsi={sp['rsi_trend']:+.2f}")

    return Signal(
        symbol=symbol,
        score=round(max(-1.5, min(1.5, score)), 3),
        trend_pct=round((closes[-1] / ema(closes, 50) - 1.0) * 100.0, 2),
        momentum_5d=round(pct_change(closes, 5), 2),
        momentum_20d=round(pct_change(closes, 20), 2),
        rel_5d=round(pct_change(closes, 5) - spy_5d, 2),
        rsi14=round(rsi14, 1),
        atr_pct=round(atr_val / closes[-1] * 100.0, 2),
        gap_pct=round(gap_pct, 2),
        gap_dir=gap_dir,
        reason=reason,
    )


def rank_long(top: list[Signal], n: int = 3) -> list[Signal]:
    """Top long candidates by score, requiring a non-trivial trend."""
    eligible = [s for s in top if s.score >= 0.45]
    return sorted(eligible, key=lambda s: s.score, reverse=True)[:n]


def rank_short(top: list[Signal], n: int = 3) -> list[Signal]:
    eligible = [s for s in top if s.score <= -0.45]
    return sorted(eligible, key=lambda s: s.score)[:n]


def conviction(sig: Signal, regime_score: float) -> float:
    """0..1 conviction blending name score with the market regime."""
    raw = sig.score * (0.6 + 0.4 * min(1.0, abs(regime_score) / 1.5))
    return round(norm_sigmoid(3.0 * raw), 3)
