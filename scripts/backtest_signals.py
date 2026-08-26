#!/usr/bin/env python3
"""Signal-quality backtest on the last ~120 sessions of daily bars.

Honest scope: this validates the SIGNAL, not the option fill. It answers
"when the Trend Vector scored a name >= threshold, what did the underlying
do over the next 1-3 sessions, versus SPY?" — which is what determines
whether the debit/credit verticals have a directional edge at all. Option
P&L depends on IV and fills as well; this is a prior, not a promise.

    .venv/bin/python scripts/backtest_signals.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from policy.loader import load as load_manifest
from scripts.verify_account import creds, load_env
from strategy.regime import classify, universe_breadth
from strategy.signals import score_symbol

HORIZONS = (1, 2, 3)
THRESHOLD = 0.55
# the shipped entry rules (manifest trend_directional), mirrored here so the
# signal tool measures what the engine actually trades
MAX_RSI = 65.0
MAX_M5 = 6.0
MAX_M20 = 25.0


def main() -> int:
    manifest = load_manifest()
    env = load_env(ROOT / ".env")
    key, secret = creds(env)
    dc = StockHistoricalDataClient(key, secret)
    symbols = sorted(set(manifest.declared_symbols()))

    data = {}
    for sym in symbols:
        req = StockBarsRequest(
            symbol_or_symbols=[sym], timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=300),
            limit=170, feed="iex", adjustment="raw")
        bars = dc.get_stock_bars(req).data.get(sym, [])
        data[sym] = [b.close for b in bars]

    spy = data.get("SPY", [])
    qqq = data.get("QQQ", [])
    print(f"{'symbol':<6} {'n_sig':>6} {'h1%':>7} {'h2%':>7} {'h3%':>7} "
          f"{'spy1%':>6} {'spy2%':>6} {'spy3%':>6} {'edge1':>7} {'edge2':>7} {'edge3':>7}")
    rows = []
    for sym, closes in data.items():
        if sym in ("SPY", "QQQ") or len(closes) < 90:
            continue
        hits = {h: [] for h in HORIZONS}
        for i in range(60, len(closes) - 3):
            # regime gate: longs only in risk_on, shorts only in risk_off
            breadth = universe_breadth(
                {s: data[s][: i + 1] for s in data if len(data[s]) > i})
            regime = classify(spy[: i + 1], qqq[: i + 1], [breadth])
            if not (regime.long_allowed or regime.short_allowed):
                continue
            direction = 1 if regime.long_allowed else -1
            sig = score_symbol(closes[: i + 1], spy[: i + 1],
                               closes[: i + 1], closes[: i + 1], sym)
            if sig is None or sig.score * direction < THRESHOLD:
                continue
            # pullback-entry filter (shipped)
            if direction > 0:
                ok = (sig.rsi14 <= MAX_RSI and abs(sig.momentum_5d) <= MAX_M5
                      and sig.momentum_20d <= MAX_M20)
            else:
                ok = (sig.rsi14 >= 100 - MAX_RSI
                      and abs(sig.momentum_5d) <= MAX_M5
                      and sig.momentum_20d >= -MAX_M20)
            if not ok:
                continue
            for h in HORIZONS:
                if i + h < len(closes):
                    hits[h].append(closes[i + h] / closes[i] - 1.0)
        if not any(hits.values()):
            continue
        def mean(v):
            return sum(v) / len(v) * 100.0 if v else 0.0
        e1 = mean(hits[1]) - mean(spy_fwd(spy, 1))
        e2 = mean(hits[2]) - mean(spy_fwd(spy, 2))
        e3 = mean(hits[3]) - mean(spy_fwd(spy, 3))
        rows.append((sym, len(hits[1]), mean(hits[1]), mean(hits[2]),
                     mean(hits[3]), mean(spy_fwd(spy, 1)), mean(spy_fwd(spy, 2)),
                     mean(spy_fwd(spy, 3)), e1, e2, e3))

    for r in sorted(rows, key=lambda r: r[9], reverse=True):
        print(f"{r[0]:<6} {r[1]:>6} {r[2]:>7.2f} {r[3]:>7.2f} {r[4]:>7.2f} "
              f"{r[5]:>6.2f} {r[6]:>6.2f} {r[7]:>6.2f} {r[8]:>7.2f} "
              f"{r[9]:>7.2f} {r[10]:>7.2f}")

    print(f"\nTHRESHOLD={THRESHOLD}, RSI<={MAX_RSI}, |5d|<={MAX_M5}%, "
          f"20d<={MAX_M20}% — plus the regime gate (risk_on longs / risk_off "
          f"shorts). Edge = mean signal-forward return minus SPY forward "
          f"return; this is the shipped entry rule set, not the raw score.")
    return 0


def spy_fwd(spy: list[float], h: int) -> list[float]:
    return [spy[i + h] / spy[i] - 1.0
            for i in range(60, len(spy) - h)]


if __name__ == "__main__":
    raise SystemExit(main())
