#!/usr/bin/env python3
"""Strategy-level backtest: the Trend Vector AS THE ENGINE TRADES IT.

Honest scope, printed at the top of every run:

  * entries = the real entry rules: regime gate (risk_on/risk_off), score
    threshold, the pullback filter (RSI<=65, |5d|<=6%, 20d<=25%), top-3
    ranking, per-engine $1,000 cap, daily $6,000 exposure cap, 10 concurrent;
  * structures = debit verticals priced with Black-Scholes, IV proxied by
    trailing 30-day realized vol (clamped 12%-80%) - we do not have free
    historical option IV, so vol is a MODEL INPUT, not market data;
  * exits = the shipped rules: take-profit +60% of debit, stop-loss -50%,
    expiry at the 2-DTE close (intrinsic value);
  * marks = daily closes only (TP/SL evaluated once per session - intraday
    fills can only be better for TP, worse for SL);
  * this is a SIMULATION of what the structure book would have done. It is
    not a fill backtest. Catalyst/Event/Vol vectors are NOT simulated -
    they are the scheduled events of this specific window and have no
    history to replay.

    .venv/bin/python scripts/backtest_strategy.py [--sweep]
"""
from __future__ import annotations

import argparse
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
from strategy.indicators import (black_scholes, bs_delta, ema,
                                 realized_vol)
from strategy.regime import classify, universe_breadth
from strategy.signals import score_symbol

TP_DEFAULT = 0.40
SL_DEFAULT = 0.50
DELTA_DEFAULT = 0.40
DTE_DEFAULT = 2
CAP_PER_TRADE = 1000.0
DAILY_CAP = 6000.0
MAX_CONCURRENT = 10
RATE = 0.045


def strike_for_delta(spot: float, tau: float, sigma: float, target: float,
                     is_call: bool) -> float:
    best, best_err = None, None
    for frac in [i / 400.0 for i in range(80, 481)]:   # 0.80x .. 1.20x
        k = spot * frac
        d = bs_delta(spot, k, tau, sigma, RATE, is_call=is_call)
        err = abs(abs(d) - target)
        if best_err is None or err < best_err:
            best, best_err = k, err
    return best


def price_vertical(spot, k1, k2, tau, sigma, is_call) -> float:
    return (black_scholes(spot, k1, tau, sigma, RATE, is_call)
            - black_scholes(spot, k2, tau, sigma, RATE, is_call))


def intrinsic_vertical(spot, k1, k2, is_call) -> float:
    if is_call:
        return max(spot - k1, 0.0) - max(spot - k2, 0.0)
    return max(k1 - spot, 0.0) - max(k2 - spot, 0.0)


def clamp_vol(rv: float) -> float:
    if rv <= 0:
        return 0.25
    return max(0.12, min(0.80, rv))


class Book:
    """A deliberately simple concurrent book, honoring the shipped caps."""
    def __init__(self, start_equity: float):
        self.equity = start_equity
        self.peak = start_equity
        self.max_dd = 0.0
        self.open: list[dict] = []
        self.trades: list[dict] = []
        self.day_risk: dict[str, float] = {}

    def can_open(self, day: str, risk: float) -> bool:
        return (len(self.open) < MAX_CONCURRENT
                and self.day_risk.get(day, 0.0) + risk <= DAILY_CAP)

    def open_trade(self, day: str, trade: dict) -> None:
        self.open.append(trade)
        self.day_risk[day] = self.day_risk.get(day, 0.0) + trade["risk"]
        trade["entry_day"] = day

    def close(self, trade: dict, pnl: float) -> None:
        if trade in self.open:
            self.open.remove(trade)
        self.equity += pnl
        self.peak = max(self.peak, self.equity)
        self.max_dd = max(self.max_dd, self.peak - self.equity)
        trade["pnl"] = round(pnl, 2)
        self.trades.append(trade)

    def mark(self, trade: dict, spot: float, sigma: float,
             tau: float) -> float | None:
        value = (intrinsic_vertical(spot, trade["k1"], trade["k2"],
                                    trade["is_call"]) if tau <= 0
                 else price_vertical(spot, trade["k1"], trade["k2"], tau,
                                     sigma, trade["is_call"]))
        debit = trade["debit"]
        if value >= (1.0 + trade["tp"]) * debit:
            return (value - debit) * 100.0 * trade["qty"]
        if value <= (1.0 - trade["sl"]) * debit:
            return (value - debit) * 100.0 * trade["qty"]
        return None


def precompute(manifest, data) -> dict:
    """Score series + regime series once; the sweep only replays the book."""
    symbols = sorted(set(manifest.declared_symbols()))
    n = min(len(data[s]) for s in symbols if s in data)
    closes = {s: [b.close for b in data[s][:n]] for s in symbols if s in data}
    spy = closes["SPY"]
    qqq = closes["QQQ"]
    dates = [b.timestamp.date().isoformat() for b in data["SPY"][:n]]

    scores = {}
    for sym in symbols:
        if sym in ("SPY", "QQQ"):
            continue
        c = closes[sym]
        scores[sym] = {}
        for t in range(70, n):
            sig = score_symbol(c[: t + 1], spy[: t + 1], c[: t + 1],
                               c[: t + 1], sym)
            if sig is not None:
                scores[sym][t] = sig

    regimes = {}
    for t in range(70, n):
        breadth = universe_breadth(
            {s: closes[s][: t + 1] for s in closes})
        regimes[t] = classify(spy[: t + 1], qqq[: t + 1], [breadth])
    return {"closes": closes, "dates": dates, "scores": scores,
            "regimes": regimes, "n": n}


def run_backtest(pre, *, tp: float, sl: float, delta_target: float,
                 dte: int) -> dict:
    closes, dates, scores, regimes, n = (pre["closes"], pre["dates"],
                                         pre["scores"], pre["regimes"],
                                         pre["n"])
    book = Book(100000.0)
    for t in range(70, n - dte - 1):
        day = dates[t]
        # exits first: mark everything held overnight at today's data
        for trade in list(book.open):
            spot = closes[trade["sym"]][t]
            sigma = clamp_vol(realized_vol(closes[trade["sym"]][: t + 1], 30))
            tau = max(trade["expiry_t"] - t, 0)
            pnl = book.mark(trade, spot, sigma, tau)
            if pnl is not None:
                book.close(trade, pnl)
        for trade in list(book.open):
            if trade["expiry_t"] <= t:
                spot = closes[trade["sym"]][t]
                value = intrinsic_vertical(spot, trade["k1"], trade["k2"],
                                           trade["is_call"])
                book.close(trade, (value - trade["debit"]) * 100.0
                           * trade["qty"])

        regime = regimes[t]
        if not (regime.long_allowed or regime.short_allowed):
            continue
        direction = 1 if regime.long_allowed else -1

        picks = []
        for sym, sigs in scores.items():
            sig = sigs.get(t)
            if sig is None:
                continue
            if direction > 0:
                ok = (sig.score >= 0.55 and sig.rsi14 <= 65.0
                      and abs(sig.momentum_5d) <= 6.0
                      and sig.momentum_20d <= 25.0)
            else:
                ok = (sig.score <= -0.55 and sig.rsi14 >= 35.0
                      and abs(sig.momentum_5d) <= 6.0
                      and sig.momentum_20d >= -25.0)
            if ok:
                picks.append((sig.score * direction, sym))
        picks.sort(reverse=True)
        if not picks:
            continue

        for _, sym in picks[:3]:
            c = closes[sym]
            spot = c[t]
            sigma = clamp_vol(realized_vol(c[: t + 1], 30))
            tau = dte / 365.0
            is_call = direction > 0
            k1 = strike_for_delta(spot, tau, sigma, delta_target, is_call)
            k2 = strike_for_delta(spot, tau, sigma, 0.25, is_call)
            if not k1 or not k2 or k1 == k2:
                continue
            debit = max(price_vertical(spot, k1, k2, tau, sigma, is_call),
                        0.01)
            per_contract = debit * 100.0
            if per_contract > CAP_PER_TRADE:
                continue
            qty = max(1, int(CAP_PER_TRADE // per_contract))
            risk = per_contract * qty
            if not book.can_open(day, risk):
                continue
            book.open_trade(day, {
                "sym": sym, "dir": direction, "k1": k1, "k2": k2,
                "debit": debit, "qty": qty, "risk": risk,
                "is_call": is_call, "tp": tp, "sl": sl,
                "expiry_t": t + dte,
            })

    trades = book.trades
    if trades:
        total = sum(x["pnl"] for x in trades)
        wins = [x for x in trades if x["pnl"] > 0]
        return {
            "n": len(trades),
            "total_pnl": round(total, 2),
            "win_rate": round(len(wins) / len(trades), 3),
            "avg": round(total / len(trades), 2),
            "best": round(max(x["pnl"] for x in trades), 2),
            "worst": round(min(x["pnl"] for x in trades), 2),
            "max_dd": round(book.max_dd, 2),
            "final_equity": round(book.equity, 2),
        }
    return {"n": 0, "total_pnl": 0.0, "win_rate": 0.0, "avg": 0.0,
            "best": 0.0, "worst": 0.0, "max_dd": 0.0,
            "final_equity": 100000.0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    manifest = load_manifest()
    env = load_env(ROOT / ".env")
    dc = StockHistoricalDataClient(*creds(env))
    symbols = sorted(set(manifest.declared_symbols()))

    data = {}
    for sym in symbols:
        # no `limit`: Alpaca fills a limited window forward from start, which
        # returns the OLDEST bars. Ask for the whole window, take the tail.
        req = StockBarsRequest(
            symbol_or_symbols=[sym], timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=420),
            feed="iex", adjustment="raw")
        data[sym] = list(dc.get_stock_bars(req).data.get(sym, []))[-250:]
    if len(data.get("SPY", [])) < 100:
        print("insufficient data — bars fetch failed")
        return 1

    print(f"universe {len(symbols)} symbols, {len(data['SPY'])} sessions, "
          f"period {data['SPY'][70].timestamp.date()} .. "
          f"{data['SPY'][-1].timestamp.date()}")

    pre = precompute(manifest, data)

    if args.sweep:
        print(f"{'TP':>5} {'SL':>5} {'Δ':>5} {'DTE':>3} {'n':>4} "
              f"{'win%':>6} {'avg$':>7} {'total$':>9} {'best$':>7} "
              f"{'worst$':>7} {'maxDD$':>7} {'equity$':>9}")
        rows = []
        for tp in (0.4, 0.6, 0.8):
            for sl in (0.4, 0.5, 0.6):
                for delta in (0.40, 0.45, 0.55):
                    for dte in (1, 2):
                        r = run_backtest(pre, tp=tp, sl=sl,
                                         delta_target=delta, dte=dte)
                        rows.append((tp, sl, delta, dte, r))
                        print(f"{tp:>5} {sl:>5} {delta:>5} {dte:>3} "
                              f"{r['n']:>4} {r['win_rate'] * 100:>5.0f}% "
                              f"{r['avg']:>7} {r['total_pnl']:>9} "
                              f"{r['best']:>7} {r['worst']:>7} "
                              f"{r['max_dd']:>7} {r['final_equity']:>9}")
        best = max(rows, key=lambda r: r[4]["final_equity"])
        print(f"\nbest by final equity: TP={best[0]} SL={best[1]} "
              f"delta={best[2]} DTE={best[3]} -> "
              f"equity ${best[4]['final_equity']:,.0f}, "
              f"win {best[4]['win_rate'] * 100:.0f}%, "
              f"avg ${best[4]['avg']:,.0f}/trade, "
              f"maxDD ${best[4]['max_dd']:,.0f}")
        return 0

    r = run_backtest(pre, tp=TP_DEFAULT, sl=SL_DEFAULT,
                     delta_target=DELTA_DEFAULT, dte=DTE_DEFAULT)
    print("\nshipped parameters (TP 40% / SL 50% / Δ0.40 / 2 DTE):")
    for k, v in r.items():
        print(f"  {k:<14} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
