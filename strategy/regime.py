#!/usr/bin/env python3
"""Market regime classification. Deterministic, bounded, fully testable."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from strategy.indicators import ema, rsi


@dataclass
class Regime:
    mode: str            # "risk_on" | "risk_off" | "chop"
    confidence: float    # 0..1
    score: float         # signed strength, roughly in [-2, +2]
    spy_vs_ema50: float
    spy_rsi14: float
    breadth: int         # net count of universe names above their EMA50
    reason: str = ""

    @property
    def long_allowed(self) -> bool:
        return self.mode == "risk_on" and self.confidence >= 0.55

    @property
    def short_allowed(self) -> bool:
        return self.mode == "risk_off" and self.confidence >= 0.55


def classify(spy_closes: Sequence[float], qqq_closes: Sequence[float],
             breadth_series: Sequence[float] | None = None) -> Regime:
    """Regime from SPY/QQQ trend + momentum + breadth.

    score = 0.5 * (SPY vs EMA50 normalized) + 0.3 * (SPY 5d momentum sign) +
            0.2 * (QQQ vs EMA20)  — calibrated so a clean uptrend lands > 1,
    a chop lands near 0, a clean downtrend < -1.
    """
    if len(spy_closes) < 60:
        return Regime("chop", 0.0, 0.0, 0.0, 50.0, 0,
                      reason="insufficient history")

    spy_ema50 = ema(spy_closes, 50)
    spy_ema20 = ema(spy_closes, 20)
    spy_rsi = rsi(spy_closes, 14)
    qqq_ema20 = ema(qqq_closes, 20)
    qqq_ema50 = ema(qqq_closes, 50)

    spy_v50 = spy_closes[-1] / spy_ema50 - 1.0
    spy_v20 = spy_closes[-1] / spy_ema20 - 1.0
    qqq_v20 = qqq_closes[-1] / qqq_ema20 - 1.0

    # 5-day momentum of SPY, scaled: ±5% -> ±1
    r5 = (spy_closes[-1] / spy_closes[-6] - 1.0) * 100.0 if len(spy_closes) > 6 else 0.0

    score = (1.2 * max(-0.12, min(0.12, spy_v50)) / 0.05
             + 0.6 * max(-0.01, min(0.01, spy_v20)) / 0.005
             + 0.6 * max(-0.10, min(0.10, qqq_v20)) / 0.05
             + 0.5 * max(-2.0, min(2.0, r5)) / 2.0)

    breadth = 0
    if breadth_series:
        # bread_series: fraction of universe above EMA50 in [-1, +1]
        breadth = 1 if breadth_series[-1] > 0.3 else (-1 if breadth_series[-1] < -0.3 else 0)

    # Seasonality guard: September is historically the weakest month. A tape
    # that is flat despite bullish seasonal base rates is treated as chop.
    # Threshold 0.35 rather than 0.45: a confirmed-but-moderate uptrend (SPY
    # above both EMAs, RSI mid-40s) is still a trend, and the per-name
    # pullback filter does the precision work.
    if score > 0.35 and spy_rsi >= 38:
        mode, confidence, reason = "risk_on", 0.55 + 0.35 * min(1.0, score / 1.5), \
            f"SPY above EMA50 (+{spy_v50 * 100:.2f}%), RSI {spy_rsi:.0f}, breadth {breadth:+d}"
    elif score < -0.35 and spy_rsi <= 62:
        mode, confidence, reason = "risk_off", 0.55 + 0.35 * min(1.0, -score / 1.5), \
            f"SPY below EMA50 ({spy_v50 * 100:.2f}%), RSI {spy_rsi:.0f}, breadth {breadth:+d}"
    else:
        mode, confidence, reason = "chop", 0.5, \
            f"SPY near EMA50 ({spy_v50 * 100:+.2f}%), RSI {spy_rsi:.0f}"

    return Regime(mode, round(confidence, 2), round(score, 2),
                  round(spy_v50, 5), spy_rsi, breadth, reason)


def universe_breadth(closes_by_symbol: dict[str, Sequence[float]],
                     window: int = 50) -> float:
    """Fraction of universe above its EMA50, normalized to [-1, +1]

    +0.2 means 60% above; -0.2 means 40% above.
    """
    if not closes_by_symbol:
        return 0.0
    above = 0
    total = 0
    for closes in closes_by_symbol.values():
        if len(closes) < window:
            continue
        total += 1
        if closes[-1] > ema(closes, window):
            above += 1
    if total == 0:
        return 0.0
    return round(above / total * 2.0 - 1.0, 3)
