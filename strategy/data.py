#!/usr/bin/env python3
"""Alpaca market-data plumbing for the strategy layer.

One class, one API surface. Everything the engines need — account, clock,
positions, stock bars/quotes, option chains with greeks — comes out of here
as plain builtins, so the engines are testable without a network and the
15-minute indicative feed appears in exactly one place (with a single knob:
MAX_OPTION_QUOTE_AGE_SECONDS in the manifest).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import (OptionChainRequest, OptionBarsRequest,
                                  StockBarsRequest, StockLatestQuoteRequest)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient

OCC_RE = re.compile(r"^([A-Z]{1,5})(\d{6})([CP])(\d{8})$")


def parse_contract(symbol: str) -> tuple[str, date, str, float] | None:
    """(underlying, expiry, type, strike) from an OCC symbol, or None."""
    m = OCC_RE.match(symbol)
    if not m:
        return None
    underlying, yymmdd, ctype, strike = m.groups()
    try:
        expiry = datetime.strptime(yymmdd, "%y%m%d").date()
    except ValueError:
        return None
    return underlying, expiry, ("call" if ctype == "C" else "put"), int(strike) / 1000.0


def partition_positions(positions: list) -> tuple[list, list]:
    """Separate option contracts from every other broker-held asset.

    Sentinel's engines deliberately consume only option positions. Callers
    retain non-option positions as an explicit fact so entry authority cannot
    ignore unmanaged exposure.
    """
    options, non_options = [], []
    for position in positions:
        asset_class = str(getattr(position, "asset_class", "")).rsplit(".", 1)[-1]
        (options if asset_class.lower() == "us_option" else non_options).append(position)
    return options, non_options


def contract_symbol(underlying: str, expiry: date, ctype: str,
                    strike: float) -> str:
    """Build an OCC symbol. Alpaca expects the 8-digit strike (x1000)."""
    body = f"{underlying.upper()}{expiry:%y%m%d}{'C' if ctype == 'call' else 'P'}"
    return f"{body}{int(round(strike * 1000)):08d}"


@dataclass
class ChainContract:
    symbol: str
    expiration: date
    contract_type: str
    strike: float
    bid: float | None
    ask: float | None
    delta: float | None
    iv: float | None
    quote_ts: datetime | None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return round((self.bid + self.ask) / 2.0, 4)


@dataclass
class MarketState:
    """A read-only snapshot passed to the engines. One per cycle."""
    account: object = None
    clock: object = None
    equity: float = 0.0
    positions: list = field(default_factory=list)
    non_option_positions: list = field(default_factory=list)
    bars: dict = field(default_factory=dict)          # symbol -> list[bar-like]
    latest: dict = field(default_factory=dict)        # symbol -> latest quote
    chains: dict = field(default_factory=dict)        # underlying -> list[ChainContract]
    chain_ages: dict = field(default_factory=dict)    # underlying -> seconds
    now_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def contracts(self, underlying: str, expiry: date | None = None,
                  ctype: str | None = None) -> list[ChainContract]:
        out = [c for c in self.chains.get(underlying, [])
               if (expiry is None or c.expiration == expiry)
               and (ctype is None or c.contract_type == ctype)]
        return out


class AlpacaData:
    """Thin wrapper. Two clients, no state of its own worth naming."""

    def __init__(self, key: str, secret: str):
        self.trading = TradingClient(key, secret, paper=True)
        self.stocks = StockHistoricalDataClient(key, secret)
        self.options = OptionHistoricalDataClient(key, secret)

    # ── account / clock ──────────────────────────────────────────────────────
    def account(self):
        return self.trading.get_account()

    def clock(self):
        return self.trading.get_clock()

    def positions(self):
        return self.trading.get_all_positions()

    def orders_open(self):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        return self.trading.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))

    # ── stocks ───────────────────────────────────────────────────────────────
    def daily_bars(self, symbols: list[str], days: int = 90) -> dict:
        """Bars for all symbols, chunked with retry against API throttling.

        A single 18-symbol request can come back partial under the free tier's
        rate limits; the agent needs every symbol it would consider, so missing
        symbols are fetched individually. A symbol that still fails is absent
        from the result — the engines treat it as 'no data' and never guess.
        """
        out: dict = {}
        batches = [symbols[i:i + 5] for i in range(0, len(symbols), 5)]
        for batch in batches:
            data = self._retry(lambda: self._bars_batch(batch, days), tries=3)
            out.update(data)
        for sym in symbols:
            if sym in out:
                continue
            data = self._retry(lambda: self._bars_batch([sym], days), tries=2)
            out.update(data)
        # staleness guard: the free feed occasionally serves an old snapshot.
        # If the freshest bar is more than 5 calendar days behind (a weekend is
        # 3 days), refetch once; if still stale, drop the symbol - the engines
        # treat absence as 'no data' and refuse rather than guess.
        def stale(bars: list) -> bool:
            if not bars:
                return True
            ts = getattr(bars[-1], "timestamp", None)
            if ts is None:
                return True
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).days > 5
        if out and any(stale(v) for v in out.values()):
            fresh = self._retry(lambda: self._bars_batch(list(symbols), days),
                                tries=2)
            if fresh:
                out.update(fresh)
        return {s: v for s, v in out.items() if not stale(v)}

    def _bars_batch(self, batch: list[str], days: int) -> dict:
        """Daily bars for a batch. Deliberately no `limit`.

        Alpaca fills a limited window from `start` FORWARD, so `limit=days`
        against a `2*days` start returned the OLDEST `days` bars: measured
        2026-08-25, that was 2026-02-27..2026-07-08, a freshest bar 48 days
        old. The staleness guard above then correctly dropped every symbol,
        and the engines — which refuse rather than guess on missing data —
        produced zero candidates. Regime read "insufficient history" forever.

        Nothing errored. The agent would simply have traded nothing all week,
        and the only visible symptom was a plausible-looking "candidates: 0".

        `limit` is also a total across the whole request rather than per
        symbol, so a five-symbol batch would have received about 18 bars each
        even if the window had been right.

        Ask for the entire window and take the tail here, where "the most
        recent `days` bars" is what the caller actually meant.
        """
        req = StockBarsRequest(
            symbol_or_symbols=batch, timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=datetime.now(timezone.utc) - timedelta(days=days * 2),
            feed="iex", adjustment="raw")
        resp = self.stocks.get_stock_bars(req)
        data = resp.data if isinstance(resp, dict) else getattr(resp, "data", {})
        return {s: list(v)[-days:] for s, v in data.items() if s in batch}

    @staticmethod
    def _retry(fn, tries: int = 3, pause: float = 4.0):
        import time
        last = None
        for attempt in range(tries):
            try:
                return fn()
            except Exception as exc:                            # noqa: BLE001
                last = exc
                time.sleep(pause * (attempt + 1))
        raise last

    def intraday_bars(self, symbol: str, limit: int = 16) -> list:
        req = StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=datetime.now(timezone.utc) - timedelta(hours=6), limit=limit,
            feed="iex", adjustment="raw")
        resp = self.stocks.get_stock_bars(req)
        return list(resp.data.get(symbol, []))

    def latest_quotes(self, symbols: list[str]) -> dict:
        """Latest IEX quotes, chunked. Missing symbols are skipped, not guessed."""
        out: dict = {}
        for i in range(0, len(symbols), 5):
            batch = symbols[i:i + 5]
            try:
                req = StockLatestQuoteRequest(symbol_or_symbols=batch, feed="iex")
                resp = self.stocks.get_stock_latest_quote(req)
                data = resp if isinstance(resp, dict) else getattr(resp, "data", {})
                out.update({k: v for k, v in data.items() if k in batch})
            except Exception:                                   # noqa: BLE001
                continue
        return out

    # ── options ──────────────────────────────────────────────────────────────
    def option_chain(self, underlying: str,
                     expiry: date | None = None) -> list[ChainContract]:
        """Fetch the chain for the target expiry; fall back to nearby days.

        v2.1: the search starts at TODAY, not tomorrow — 0-DTE structures
        (NFP gap play, zero-DTE time-stops) need today's expiry. Returns
        normalized ChainContract records; quote age travels with each one.
        """
        target = expiry or date.today()
        for offset in range(0, 8):
            day = target + timedelta(days=offset)
            contracts = self._chain_for_expiry(underlying, day)
            if contracts:
                return contracts
        return []

    def option_chains_multi(self, underlying: str,
                            expiries: list[date]) -> list[ChainContract]:
        """Chains for several expiries merged, in one list. The engines filter
        by expiration afterwards; a 0-DTE engine and a 2-DTE engine can both
        find their strikes in the same snapshot."""
        out: list[ChainContract] = []
        seen: set[str] = set()
        for day in expiries:
            for c in self._chain_for_expiry(underlying, day):
                if c.symbol not in seen:
                    seen.add(c.symbol)
                    out.append(c)
        return out

    def _chain_for_expiry(self, underlying: str,
                          day: date) -> list[ChainContract]:
        try:
            resp = self.options.get_option_chain(
                OptionChainRequest(underlying_symbol=underlying,
                                   expiration_date=day))
        except Exception:                                       # noqa: BLE001
            return []
        out: list[ChainContract] = []
        for sym, snap in resp.items():
            parsed = parse_contract(sym)
            if not parsed:
                continue
            _, exp, ctype, strike = parsed
            quote = getattr(snap, "latest_quote", None)
            greeks = getattr(snap, "greeks", None)
            bid = getattr(quote, "bid_price", None) if quote else None
            ask = getattr(quote, "ask_price", None) if quote else None
            ts = getattr(quote, "timestamp", None) if quote else None
            out.append(ChainContract(
                symbol=sym, expiration=exp, contract_type=ctype,
                strike=strike, bid=bid, ask=ask,
                delta=getattr(greeks, "delta", None) if greeks else None,
                iv=getattr(snap, "implied_volatility", None),
                quote_ts=ts,
            ))
        return out

    def chain_age_seconds(self, contracts: list[ChainContract]) -> float | None:
        """Age of the freshest quote in the chain, per the clock."""
        ts = [c.quote_ts for c in contracts if c.quote_ts is not None]
        if not ts:
            return None
        newest = max(ts)
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - newest).total_seconds())
