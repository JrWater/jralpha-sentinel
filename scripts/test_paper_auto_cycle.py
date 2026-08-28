#!/usr/bin/env python3
"""Run one real autonomous cycle on the isolated legacy paper account.

This is deliberately separate from both production cron and
validate_delay_chain.py:

* production manifest, credentials, cron and state are never edited;
* the real run_cycle -> Gates -> Executor path submits the order;
* a test-only manifest binds authority to the connected legacy paper account;
* every durable artifact is redirected to an isolated run directory;
* at most one Proposal is selected and its declared max loss is capped;
* the account must start with zero positions and zero open orders.

The command is inert unless ``--confirm-paper-order`` is present.  Paper
orders are still broker mutations, so an explicit flag is required every run.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LEGACY_ENV = Path.home() / ".openclaw" / ".env"
TEST_ROOT = Path("/private/tmp/jralpha-sentinel-paper-e2e")
DEFAULT_MAX_RISK = 500.0


def _status(order) -> str:
    return str(getattr(order, "status", "")).split(".")[-1].lower()


def _open_orders(client) -> list:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    return list(client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)))


def _one_marketable_credit_candidate(candidates: list, max_risk: float):
    """Keep one engine-produced vertical and make its limit fill-oriented.

    This is deliberately test-harness-only.  The proposal still traverses
    run_cycle's proposer, pretrade gates, budget reservation and Executor;
    only the minimum acceptable credit is changed.  We refuse unless that
    credit is both inside the declared loss cap and currently marketable at
    the displayed leg bid/ask.
    """
    for candidate in candidates:
        proposal = candidate.proposal
        if proposal.structure != "credit_vertical" or len(proposal.legs) != 2:
            continue
        sells = [leg for leg in proposal.legs if leg.side == "sell"]
        buys = [leg for leg in proposal.legs if leg.side == "buy"]
        if len(sells) != 1 or len(buys) != 1:
            continue
        qty = int(proposal.legs[0].quantity)
        width = abs(sells[0].strike - buys[0].strike)
        minimum_credit = max(0.01, width - max_risk / (100.0 * qty))
        marketable_credit = sells[0].ref_bid - buys[0].ref_ask
        if marketable_credit + 1e-9 < minimum_credit:
            continue
        credit = round(minimum_credit, 2)
        proposal.limit_price = -credit
        proposal.max_loss_dollars = round((width - credit) * 100 * qty, 2)
        proposal.reason = f"{proposal.reason}; isolated fill test @ {credit:.2f}"
        return [candidate]
    raise RuntimeError(
        "no engine-produced credit vertical is both marketable and inside "
        f"the ${max_risk:,.0f} test cap")


def build_test_manifest(raw: dict, *, account_id: str, now_utc: datetime,
                        max_risk_dollars: float):
    """Return an in-memory policy capped to one minimal paper Proposal."""
    from policy.loader import Manifest

    cooked = deepcopy(raw)
    starting = float(cooked["environment"]["required_starting_equity"])
    cap_fraction = max_risk_dollars / starting
    cooked["environment"]["competition_account_id"] = account_id
    cooked["session"]["competition_starts_utc"] = (
        now_utc - timedelta(hours=1)).isoformat()
    cooked["agent"]["max_proposals_per_cycle"] = 1
    cooked["risk_caps"]["max_loss_per_position_fraction"] = cap_fraction
    cooked["risk_caps"]["at_risk_cap_fraction"] = cap_fraction
    cooked["risk_caps"]["daily_new_exposure_cap_fraction"] = cap_fraction

    for cfg in cooked.get("strategies", {}).values():
        if not isinstance(cfg, dict):
            continue
        for key in list(cfg):
            if "max_loss" in key and key.endswith("_fraction"):
                cfg[key] = cap_fraction
    return Manifest(cooked)


def _wait_order(client, order_id, timeout: int):
    deadline = time.monotonic() + timeout
    last = client.get_order_by_id(order_id)
    while time.monotonic() < deadline:
        last = client.get_order_by_id(order_id)
        if _status(last) in {
            "filled", "canceled", "expired", "rejected", "suspended"
        }:
            return last
        time.sleep(2)
    return last


def _test_paths(run_id: str) -> dict[str, Path]:
    base = TEST_ROOT / run_id
    return {
        "base": base,
        "day": base / "day_state.json",
        "permit": base / "entry_permit.json",
        "ledger": base / "ledger.json",
        "decisions": base / "decisions.jsonl",
        "meta": base / "positions_meta.json",
        "snapshot": base / "snapshot.json",
        # The journal and the cycle lock are isolated for the same reason the
        # ledger is: a rehearsal must never hold production's lock, and must
        # never leave a DISPATCHING record that would block the live cycle.
        "wal": base / "submission_wal.jsonl",
        "lock": base / "cycle.lock",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-paper-order", action="store_true")
    ap.add_argument("--max-risk-dollars", type=float,
                    default=DEFAULT_MAX_RISK)
    ap.add_argument("--fill-wait-seconds", type=int, default=45)
    ap.add_argument(
        "--require-fill", action="store_true",
        help="test entry fill plus system-managed exit using a capped, "
             "marketable credit-vertical limit")
    args = ap.parse_args()
    if not args.confirm_paper_order:
        print("REFUSED: pass --confirm-paper-order for a real paper order")
        return 2
    if not 0 < args.max_risk_dollars <= DEFAULT_MAX_RISK:
        print(f"REFUSED: max risk must be in (0, {DEFAULT_MAX_RISK:.0f}]")
        return 2

    from alpaca.trading.client import TradingClient
    from policy.loader import MANIFEST_PATH
    from scripts import run_cycle as rc
    from scripts.verify_account import creds, load_env
    from agent import ledger as ledger_mod
    from agent import snapshot as snapshot_mod
    from gates import safety_gate

    key, secret = creds(load_env(LEGACY_ENV))
    if not key or not secret:
        print(f"REFUSED: legacy credentials unavailable at {LEGACY_ENV}")
        return 2
    client = TradingClient(key, secret, paper=True)
    account = client.get_account()
    raw = json.loads(MANIFEST_PATH.read_text())
    production_id = raw["environment"].get("competition_account_id")
    if account.account_number == production_id:
        print("REFUSED: connected account is the production competition account")
        return 3
    if getattr(client, "_sandbox", None) is not True:
        print("REFUSED: client is not confirmed paper")
        return 3
    positions = list(client.get_all_positions())
    orders = _open_orders(client)
    if positions or orders:
        print(f"REFUSED: test account is not clean: positions={len(positions)} "
              f"open_orders={len(orders)}")
        return 3

    now_utc = datetime.now(timezone.utc)
    test_manifest = build_test_manifest(
        raw, account_id=account.account_number, now_utc=now_utc,
        max_risk_dollars=args.max_risk_dollars)
    run_id = now_utc.strftime("%Y%m%dT%H%M%SZ")
    paths = _test_paths(run_id)
    paths["base"].mkdir(parents=True, exist_ok=False)

    def mirror(rows):
        return ledger_mod.mirror_from_broker(rows, paths["ledger"])

    def ledger_positions():
        return ledger_mod.ledger_positions(paths["ledger"])

    def append_decision(row):
        return ledger_mod.append_decision(row, paths["decisions"])

    def write_permit(results, gates, *, manifest_sha):
        return safety_gate.write_permit(
            results, gates, manifest_sha=manifest_sha,
            path=paths["permit"])

    def decisions_writable(_root=None):
        paths["decisions"].parent.mkdir(parents=True, exist_ok=True)
        paths["decisions"].touch(exist_ok=True)
        return True

    snapshot_mod.SNAPSHOT = paths["snapshot"]
    rc.STRUCTURES = ledger_mod.StructureLedger(paths["meta"])
    rc.DAY_PATH = paths["day"]
    rc.SUBMISSION_WAL_PATH = paths["wal"]
    rc.CYCLE_LOCK_PATH = paths["lock"]
    rc.PUBLIC_ACCOUNT_SCOPE = "legacy_test"

    print(f"test account: {account.account_number}")
    print(f"isolated state: {paths['base']}")
    print(f"max declared risk: ${args.max_risk_dollars:,.2f}")

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            rc, "load_manifest", lambda *a, **k: test_manifest))
        stack.enter_context(mock.patch.object(rc, "mirror_from_broker", mirror))
        stack.enter_context(mock.patch.object(rc, "ledger_positions",
                                              ledger_positions))
        stack.enter_context(mock.patch.object(rc, "append_decision",
                                              append_decision))
        stack.enter_context(mock.patch.object(rc, "write_permit", write_permit))
        stack.enter_context(mock.patch(
            "gates.evaluation._decisions_writable", decisions_writable))
        stack.enter_context(mock.patch("agent.executor.append_decision",
                                      append_decision))
        if args.require_fill:
            real_run_engines = rc.run_engines

            def fill_oriented_engines(ctx):
                return _one_marketable_credit_candidate(
                    real_run_engines(ctx), args.max_risk_dollars)

            stack.enter_context(mock.patch.object(
                rc, "run_engines", fill_oriented_engines))

        sys.argv = ["run_cycle.py", "--env", str(LEGACY_ENV), "--no-llm"]
        code = rc.main()
        if code != 0:
            print(f"AUTO_CYCLE_FAILED exit={code}")
            return 4

        records = [json.loads(line) for line in
                   paths["decisions"].read_text().splitlines() if line.strip()]
        submitted = [r for r in records if r.get("kind") == "order_submitted"]
        if len(submitted) != 1:
            print(f"AUTO_CYCLE_FAILED submitted_records={len(submitted)}")
            return 4
        record = submitted[0]
        if float(record.get("max_loss_dollars", 0)) > args.max_risk_dollars:
            print("AUTO_CYCLE_FAILED risk cap exceeded")
            return 5

        order_id = record["order_id"]
        order = _wait_order(client, order_id, args.fill_wait_seconds)
        status = _status(order)
        print(f"broker order: {order_id} status={status}")

        if status in {"rejected", "suspended", "expired"}:
            clean = not client.get_all_positions() and not _open_orders(client)
            print(f"AUTO_SUBMISSION_REJECTED cleanup={'PASS' if clean else 'FAIL'}")
            return 5 if clean else 6

        if status not in {"filled", "partially_filled"}:
            if status != "canceled":
                client.cancel_order_by_id(order_id)
                order = _wait_order(client, order_id, 20)
                status = _status(order)
                print(f"unfilled test order canceled: status={status}")
            clean = not client.get_all_positions() and not _open_orders(client)
            print(f"AUTO_SUBMISSION_ACCEPTED cleanup={'PASS' if clean else 'FAIL'}")
            return 0 if clean else 6

        # A fill proves more than acceptance.  Use the system's own structure
        # exit implementation, with a test-only final-date policy that makes
        # the already-recorded Position group immediately eligible to flatten.
        cleanup_raw = deepcopy(test_manifest._raw)
        now_et = datetime.now(timezone.utc).astimezone(
            ZoneInfo(str(cleanup_raw["session"]["timezone"])))
        cleanup_raw["session"]["final_trading_date"] = now_et.date().isoformat()
        cleanup_raw["session"]["flatten_all_at"] = "00:00"
        from policy.loader import Manifest
        cleanup_manifest = Manifest(cleanup_raw)
        data = rc.AlpacaData(key, secret)
        state = rc.build_state(data, cleanup_manifest,
                               [str(record["underlying"])])
        closed = rc.manage_exits(
            state, cleanup_manifest, rc.Executor(data.trading, cleanup_manifest))
        print(f"system exit submissions: {closed}")

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if not client.get_all_positions() and not _open_orders(client):
                print("AUTO_FILL_AND_SYSTEM_EXIT cleanup=PASS")
                return 0
            time.sleep(3)
        print("AUTO_FILL_AND_SYSTEM_EXIT cleanup=FAIL; manual attention required")
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
