#!/usr/bin/env python3
"""Correct pre-fill structure records from broker evidence, never guesses.

This migration exists for the first competition day, when a structure was
recorded as soon as Alpaca accepted an entry request.  It is intentionally
separate from the forward lifecycle: this code maps legacy groups to their
entry orders only when the decision record identifies exactly one order.

Without ``--apply`` the command is read-only and prints an audit report.  An
apply writes only the requested metadata file and a JSON report beside it.
It never submits, cancels, or modifies a broker order.

The report may include ``mapping_candidates``.  They are review aids, not an
authority to edit state: an operator must copy only reviewed entries into an
explicit mapping file, and each entry is then cross-checked with the decision
record and the broker's client ID plus wire legs before ``--apply`` is allowed.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.ledger import STRUCTURE_META, atomic_write
from scripts.verify_account import creds, load_env


def _status(order: Any) -> str:
    if isinstance(order, dict):
        value = order.get("status", "")
    else:
        value = getattr(order, "status", "")
    return str(value or "").rsplit(".", 1)[-1].upper()


def _filled(order: Any) -> int | None:
    value = order.get("filled_qty") if isinstance(order, dict) else \
        getattr(order, "filled_qty", None)
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _group_quantity(group: dict) -> int | None:
    quantities = {
        int(info.get("qty")) for info in group.get("legs", {}).values()
        if str(info.get("qty", "")).lstrip("-").isdigit()
    }
    if len(quantities) != 1:
        return None
    quantity = quantities.pop()
    return quantity if quantity > 0 else None


def _entered_hhmmss(group_id: str) -> str | None:
    part = group_id.split("@", 1)[0].rsplit(":", 1)[-1]
    return part if len(part) == 6 and part.isdigit() else None


def _entry_matches(group_id: str, group: dict, decisions: list[dict]) -> list[dict]:
    entered = _entered_hhmmss(group_id)
    if entered is None:
        return []
    matches = []
    for decision in decisions:
        if decision.get("kind") != "order_submitted":
            continue
        if (decision.get("engine"), decision.get("underlying"),
                decision.get("structure")) != (
                    group.get("engine"), group.get("underlying"),
                    group.get("structure")):
            continue
        try:
            recorded = datetime.fromisoformat(
                str(decision.get("at_utc", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        # The group id is stamped at cycle start while the broker call happens
        # later in that same cycle.  Minute precision is the strongest shared
        # fact; engine/underlying/structure below keep it fail-closed if a
        # cycle ever submits two otherwise identical entries.
        if recorded.strftime("%H%M") == entered[:4] and decision.get("order_id"):
            matches.append(decision)
    return matches


def _mapping_candidates(meta: dict, decisions: list[dict]) -> dict[str, dict]:
    """Surface uniquely-shaped legacy candidates without silently trusting them."""
    candidates: dict[str, dict] = {}
    for group_id, group in meta.get("groups", {}).items():
        if group.get("closed") or group.get("close_order_id"):
            continue
        matches = _entry_matches(group_id, group, decisions)
        if len(matches) == 1:
            row = matches[0]
            order_id = str(row.get("order_id", ""))
            client_id = str(row.get("client_order_id", ""))
            if order_id and client_id:
                candidates[group_id] = {
                    "order_id": order_id, "client_order_id": client_id}
    return candidates


def _mapping_matches_evidence(mapping: Any, matches: list[dict], order: Any,
                              group: dict) -> bool:
    """A legacy group changes only through an explicit, cross-checked map."""
    if not isinstance(mapping, dict):
        return False
    order_id = str(mapping.get("order_id", ""))
    client_id = str(mapping.get("client_order_id", ""))
    if not order_id or not client_id:
        return False
    if str(_value(order, "id") or "") != order_id:
        return False
    decision = next((row for row in matches
                     if str(row.get("order_id", "")) == order_id
                     and str(row.get("client_order_id", "")) == client_id), None)
    if decision is None:
        return False
    observed = (order.get("client_order_id") if isinstance(order, dict)
                else getattr(order, "client_order_id", None))
    return (str(observed or "") == client_id
            and _order_legs_match_group(order, group))


def _value(row: Any, key: str) -> Any:
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _order_legs_match_group(order: Any, group: Any) -> bool:
    """Verify the mapped order's effective legs against ledger-owned legs."""
    if not isinstance(group, dict):
        return False
    expected = {
        str(symbol): (str(info.get("side", "")).lower(),
                      _positive_int(info.get("qty")))
        for symbol, info in group.get("legs", {}).items()
        if isinstance(info, dict)
    }
    if not expected or any(qty is None for _side, qty in expected.values()):
        return False
    legs = _value(order, "legs") or []
    observed: dict[str, tuple[str, int | None]] = {}
    if legs:
        top_qty = _positive_int(_value(order, "qty"))
        if top_qty is None:
            return False
        for leg in legs:
            ratio = _positive_int(_value(leg, "ratio_qty"))
            if ratio is None:
                return False
            symbol = str(_value(leg, "symbol") or "")
            side = str(_value(leg, "side") or "").rsplit(".", 1)[-1].lower()
            observed[symbol] = (side, ratio * top_qty)
    else:
        symbol = str(_value(order, "symbol") or "")
        side = str(_value(order, "side") or "").rsplit(".", 1)[-1].lower()
        observed[symbol] = (side, _positive_int(_value(order, "qty")))
    return observed == expected


def _expired_absent(group: dict, broker_symbols: set[str] | None,
                    as_of: date | None) -> bool:
    """Recognize only an already-expired, entirely absent historical group."""
    if broker_symbols is None or as_of is None:
        return False
    try:
        expiry = date.fromisoformat(str(group.get("expiry", "")))
    except ValueError:
        return False
    symbols = set(group.get("legs", {}))
    return bool(symbols) and expiry < as_of and symbols.isdisjoint(broker_symbols)


def reconcile_meta(meta: dict, decisions: list[dict], orders: dict[str, Any],
                   mappings: dict[str, dict] | None = None, *,
                   broker_symbols: set[str] | None = None,
                   as_of: date | None = None
                   ) -> tuple[dict, dict]:
    """Return corrected copy and deterministic report from supplied evidence."""
    corrected = copy.deepcopy(meta)
    report = {
        "changed": [], "blockers": [],
        "mapping_candidates": _mapping_candidates(meta, decisions),
    }
    groups = corrected.setdefault("groups", {})

    for group_id, group in groups.items():
        if group.get("closed"):
            continue
        expected = _group_quantity(group)
        if expected is None:
            report["blockers"].append({
                "code": "GROUP_QUANTITY_UNREPRESENTABLE", "group": group_id})
            continue

        close_id = group.get("close_order_id")
        if close_id:
            close = orders.get(str(close_id))
            if close is None:
                report["blockers"].append({
                    "code": "CLOSE_ORDER_NOT_FOUND", "group": group_id})
                continue
            if _status(close) == "FILLED" and _filled(close) == expected:
                group["closed"] = True
                group["close_pending"] = False
                group["terminal_outcome"] = "CLOSE_FILLED"
                group["close_filled_qty"] = expected
                report["changed"].append(group_id)
                continue
            report["blockers"].append({
                "code": "CLOSE_ORDER_UNRESOLVED", "group": group_id})
            continue

        matches = _entry_matches(group_id, group, decisions)
        mapping = (mappings or {}).get(group_id)
        if mapping is None:
            report["blockers"].append({
                "code": "LEGACY_ENTRY_MAPPING_REQUIRED", "group": group_id})
            continue
        if len(matches) != 1:
            report["blockers"].append({
                "code": "ENTRY_ORDER_AMBIGUOUS", "group": group_id})
            continue
        order_id = str(mapping.get("order_id", ""))
        order = orders.get(order_id)
        if order is None:
            report["blockers"].append({
                "code": "ENTRY_ORDER_NOT_FOUND", "group": group_id})
            continue
        if not _mapping_matches_evidence(mapping, matches, order, group):
            report["blockers"].append({
                "code": "ENTRY_ORDER_MAPPING_MISMATCH", "group": group_id})
            continue
        status, filled = _status(order), _filled(order)
        group["entry_order_id"] = order_id
        group["entry_broker_status"] = status.lower()
        group["entry_filled_qty"] = filled
        if status in {"CANCELED", "REJECTED", "EXPIRED"} and filled == 0:
            group["closed"] = True
            group["terminal_outcome"] = "ENTRY_NOT_FILLED"
            report["changed"].append(group_id)
            continue
        if status == "FILLED" and filled == expected:
            if _expired_absent(group, broker_symbols, as_of):
                group["closed"] = True
                group["terminal_outcome"] = "ENTRY_EXPIRED"
            report["changed"].append(group_id)
            continue
        report["blockers"].append({
            "code": "ENTRY_ORDER_UNRESOLVED", "group": group_id})

    report["changed"].sort()
    report["blockers"].sort(key=lambda row: (row["code"], row["group"]))
    return corrected, report


def _read_decisions(path: Path) -> list[dict]:
    rows = []
    for raw in path.read_text().splitlines():
        if raw.strip():
            rows.append(json.loads(raw))
    return rows


def _broker_snapshot(env_path: Path) -> tuple[dict[str, Any], set[str]]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    key, secret = creds(load_env(env_path))
    if not key or not secret:
        raise RuntimeError("competition Alpaca credentials are unavailable")
    client = TradingClient(key, secret, paper=True)
    orders = client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500))
    return ({str(order.id): order for order in orders},
            {str(position.symbol) for position in client.get_all_positions()})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta", type=Path, default=STRUCTURE_META)
    parser.add_argument("--decisions", type=Path,
                        default=ROOT / "state" / "decisions.jsonl")
    parser.add_argument("--report", type=Path,
                        default=ROOT / "state" / "structure_reconciliation_report.json")
    parser.add_argument("--env", type=Path, default=ROOT / ".env",
                        help="Alpaca credentials file; never copied into this tool")
    parser.add_argument("--mapping", type=Path,
                        help="explicit JSON group-to-order/client-id mapping")
    parser.add_argument("--apply", action="store_true",
                        help="write corrected metadata only when report is clean")
    args = parser.parse_args()

    if args.apply and args.mapping is None:
        parser.error("--apply requires an explicit --mapping audit file")
    meta = json.loads(args.meta.read_text())
    mappings = (json.loads(args.mapping.read_text()) if args.mapping else {})
    orders, broker_symbols = _broker_snapshot(args.env)
    corrected, report = reconcile_meta(
        meta, _read_decisions(args.decisions), orders, mappings,
        broker_symbols=broker_symbols, as_of=datetime.now(timezone.utc).date())
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["meta_path"] = str(args.meta)
    report["would_apply"] = bool(report["changed"]) and not report["blockers"]
    atomic_write(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.apply:
        if report["blockers"]:
            print("REFUSED: reconciliation report contains blockers")
            return 2
        atomic_write(args.meta, corrected)
        print(f"APPLIED: {len(report['changed'])} structure record(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
