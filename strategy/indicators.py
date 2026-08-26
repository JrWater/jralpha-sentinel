#!/usr/bin/env python3
"""Pure indicator math. No I/O, no network, no state — unit-testable directly."""
from __future__ import annotations

import math
from typing import Sequence


def ema(values: Sequence[float], period: int) -> float:
    """Exponential moving average of the series, ending at the last value."""
    if not values:
        raise ValueError("empty series")
    k = 2.0 / (period + 1.0)
    e = float(values[0])
    for v in values[1:]:
        e = float(v) * k + e * (1.0 - k)
    return e


def ema_series(values: Sequence[float], period: int) -> list[float]:
    """Full EMA series, same length as input."""
    out: list[float] = []
    k = 2.0 / (period + 1.0)
    e: float | None = None
    for v in values:
        e = float(v) if e is None else float(v) * k + e * (1.0 - k)
        out.append(e)
    return out


def rsi(closes: Sequence[float], period: int = 14) -> float:
    """Wilder RSI at the end of the series."""
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = float(closes[i]) - float(closes[i - 1])
        if d >= 0:
            gains += d
        else:
            losses -= d
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def rsi_series(closes: Sequence[float], period: int = 14) -> list[float]:
    """Wilder RSI for every index (seed with SMA of the first period)."""
    out: list[float] = []
    avg_gain = avg_loss = 0.0
    for i, c in enumerate(closes):
        if i == 0:
            out.append(50.0)
            continue
        d = c - closes[i - 1]
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        if i <= period:
            avg_gain += gain / period
            avg_loss += loss / period
            out.append(50.0 if avg_loss == 0 else
                       100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
            continue
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out.append(50.0 if avg_loss == 0 else
                   100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return out


def atr(highs: Sequence[float], lows: Sequence[float],
        closes: Sequence[float], period: int = 14) -> float:
    """Average True Range at the end of the series."""
    n = len(closes)
    if n < 2:
        return 0.0
    trs = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    if len(trs) < period:
        return sum(trs) / max(len(trs), 1)
    return sum(trs[-period:]) / period


def realized_vol(closes: Sequence[float], window: int = 30,
                 annualization: float = 252.0) -> float:
    """Annualized realized volatility from daily log returns."""
    if len(closes) < window + 1:
        return 0.0
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - window, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(annualization)


def ivr(atm_iv: float, realized_vol: float) -> float | None:
    """Implied-vol richness proxy: how far premium is above recent realized vol.

    0.0 means IV == realized vol; 0.5 means IV is 50% richer. Negative means
    cheap premium. Used to pick credit structures (rich) vs debit (cheap).
    """
    if not atm_iv or not realized_vol or realized_vol <= 0:
        return None
    return atm_iv / realized_vol - 1.0


def pct_change(closes: Sequence[float], n: int) -> float:
    if len(closes) <= n or closes[-1 - n] == 0:
        return 0.0
    return (closes[-1] / closes[-1 - n] - 1.0) * 100.0


def norm_sigmoid(x: float) -> float:
    """Map a z-like score to 0..1."""
    return 1.0 / (1.0 + math.exp(-max(-12.0, min(12.0, x))))


def black_scholes(s: float, k: float, t: float, sigma: float,
                  r: float = 0.045, is_call: bool = True) -> float:
    """European BS price. Options are American in practice; for 0-3 DTE on
    liquid underlyings the early-exercise premium is negligible and this is a
    far better frame than trusting a 15-minute-old quote directly."""
    if t <= 0 or sigma <= 0 or s <= 0 or k <= 0:
        return 0.0
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if is_call:
        return s * _n_cdf(d1) - k * math.exp(-r * t) * _n_cdf(d2)
    return k * math.exp(-r * t) * _n_cdf(-d2) - s * _n_cdf(-d1)


def bs_delta(s: float, k: float, t: float, sigma: float,
             r: float = 0.045, is_call: bool = True) -> float:
    if t <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return _n_cdf(d1) if is_call else _n_cdf(d1) - 1.0


def _n_cdf(x: float) -> float:
    return 0.5 * (1.0 + _erf(x / math.sqrt(2.0)))


def _erf(x: float) -> float:
    # Abramowitz-Stegun 7.1.26 — plenty for pricing intraday options.
    sign = -1.0 if x < 0 else 1.0
    x = abs(x)
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return sign * y
