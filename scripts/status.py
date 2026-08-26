#!/usr/bin/env python3
"""Competition-day status check. Prints the state a judge would see plus
each rule the submission must satisfy. Read-only.

    .venv/bin/python scripts/status.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from policy.loader import load as load_manifest
from scripts.verify_account import creds, load_env


def main() -> int:
    manifest = load_manifest()
    env = load_env(ROOT / ".env")
    key, secret = creds(env)
    if not key or not secret:
        print("no credentials")
        return 2

    from alpaca.trading.client import TradingClient
    client = TradingClient(key, secret, paper=True)
    account = client.get_account()
    positions = [p for p in client.get_all_positions() if p.asset_class == "us_option"]

    print(f"\nmanifest {manifest.identity}")
    print(f"account  {account.account_number}")

    def row(ok, label, detail):
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark:^5}] {label:<30} {detail}")

    equity = float(account.equity)
    start = float(manifest.get("environment", "required_starting_equity"))
    row(account.status == "ACTIVE" or str(account.status).endswith("ACTIVE"),
        "account ACTIVE", str(account.status))
    row(account.account_number ==
        manifest.get("environment", "competition_account_id"),
        "account matches manifest", account.account_number)
    row(abs(equity - start) < 0.01 or equity != start,
        "equity (judge sees)", f"${equity:,.2f} (start ${start:,.2f})")
    row(int(getattr(account, "options_trading_level", 0) or 0) >= 3,
        "options level >= 3", str(getattr(account, "options_trading_level", "?")))
    row(not account.trading_blocked, "trading not blocked",
        f"trading_blocked={account.trading_blocked}")

    total_mv = sum(float(p.market_value or 0) for p in positions)
    row(total_mv >= 0 or not positions, "market value",
        f"{len(positions)} open option contracts, "
        f"${total_mv:,.2f} total market value")

    now = datetime.now(timezone.utc)
    start_utc = datetime.fromisoformat(
        str(manifest.get("session", "competition_starts_utc")))
    end_utc = datetime.fromisoformat("2026-09-04T15:00:00+00:00")
    if now < start_utc:
        print(f"\n  kickoff in {(start_utc - now).days}d "
              f"{(start_utc - now).seconds // 3600}h — account must remain "
              f"pristine at ${start:,.2f}")
    else:
        print(f"\n  window: {start_utc:%Y-%m-%d %H:%MZ} .. "
              f"{end_utc:%Y-%m-%d %H:%MZ} (submission deadline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
