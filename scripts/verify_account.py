#!/usr/bin/env python3
"""Verify the dedicated competition paper account before anything else runs.

The hackathon rules are specific and unforgiving:

  * the submission must run on a **brand-new** Alpaca paper account; a reused
    account is "not eligible for judging"
  * that account's starting balance must be **$100,000**
  * every strategy must involve options, which needs level 3 for spreads
    (auto-approved on paper, but asserted here rather than assumed)

Getting any of these wrong is not a bug you find on day three — it is a week of
work that scores zero. So this runs first, and it is loud.

Usage:
    .venv/bin/python scripts/verify_account.py
    .venv/bin/python scripts/verify_account.py --compare-legacy
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

GREEN, RED, YELLOW, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")


def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def creds(env: dict) -> tuple:
    """Accept either spelling of the secret.

    The official SDK and MCP server want ALPACA_SECRET_KEY; a pre-existing
    setup may spell it ALPACA_API_SECRET. Reading both here means a rename
    never becomes a 3am authentication mystery.
    """
    key = env.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY")
    secret = (env.get("ALPACA_SECRET_KEY") or env.get("ALPACA_API_SECRET")
              or os.environ.get("ALPACA_SECRET_KEY")
              or os.environ.get("ALPACA_API_SECRET"))
    return key, secret


def row(ok: bool, label: str, detail: str) -> bool:
    mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{mark}] {label:<28} {detail}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare-legacy", action="store_true",
                        help="also query the pre-existing account in "
                             "~/.openclaw/.env and assert the competition "
                             "account is a different one")
    args = parser.parse_args()

    from alpaca.trading.client import TradingClient
    from policy.loader import load as load_manifest

    manifest = load_manifest()
    env = load_env(ROOT / ".env")
    key, secret = creds(env)
    if not key or not secret:
        print(f"{RED}No credentials.{RESET} Put ALPACA_API_KEY and "
              f"ALPACA_SECRET_KEY in {ROOT}/.env")
        return 2

    client = TradingClient(key, secret, paper=True)
    account = client.get_account()

    print(f"\n{DIM}manifest {manifest.identity}{RESET}")
    print(f"\nCompetition account check\n")

    required_equity = float(
        manifest.get("environment", "required_starting_equity"))
    required_level = int(manifest.get("environment", "required_options_level"))
    declared_id = manifest.get("environment", "competition_account_id",
                               default=None)

    equity = float(account.equity)
    cash = float(account.cash)
    level = int(getattr(account, "options_trading_level", 0) or 0)

    results = [
        row(str(account.status).endswith("ACTIVE"), "account status",
            str(account.status)),
        row(abs(equity - required_equity) < 0.01, "starting equity",
            f"${equity:,.2f} (required exactly ${required_equity:,.2f})"),
        row(abs(cash - required_equity) < 0.01, "cash is untouched",
            f"${cash:,.2f} — must be a fresh account with no trades yet"),
        row(level >= required_level, "options trading level",
            f"level {level} (need >= {required_level} for spreads)"),
        row(not account.trading_blocked, "trading not blocked",
            f"trading_blocked={account.trading_blocked}"),
    ]

    if declared_id:
        results.append(row(
            account.account_number == declared_id, "matches manifest",
            f"{account.account_number} vs declared {declared_id}"))
    else:
        print(f"  [{YELLOW}TODO{RESET}] {'manifest binding':<28} "
              f"set competition_account_id to {account.account_number}")

    if args.compare_legacy:
        legacy_key, legacy_secret = creds(
            load_env(Path.home() / ".openclaw" / ".env"))
        if legacy_key and legacy_secret:
            try:
                legacy = TradingClient(legacy_key, legacy_secret,
                                       paper=True).get_account()
                results.append(row(
                    legacy.account_number != account.account_number,
                    "distinct from existing",
                    f"existing account is {legacy.account_number} — the rules "
                    f"reject a reused account"))
            except Exception as exc:                       # noqa: BLE001
                print(f"  [{YELLOW}SKIP{RESET}] {'distinct from existing':<28} "
                      f"could not query legacy account: {exc}")

    print()
    if all(results):
        print(f"{GREEN}READY{RESET} — account {account.account_number} "
              f"satisfies every competition requirement.")
        if not declared_id:
            print(f"{YELLOW}Next:{RESET} write "
                  f'"competition_account_id": "{account.account_number}" '
                  f"into policy/manifest.json")
        return 0

    print(f"{RED}NOT READY{RESET} — fix the failures above in the Alpaca "
          f"dashboard before building anything on this account.")
    print(f"{DIM}A wrong account here is not a bug you find on day three; it "
          f"is a week of work that scores zero.{RESET}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
