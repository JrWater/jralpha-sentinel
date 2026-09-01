#!/usr/bin/env python3
"""Local ledger + append-only decision log. The reconcile gate's counterpart.

The ledger mirrors what the broker reports, so pos-merge disagreements surface
before sizing, not after a fill. It is intentionally boring: JSON on disk,
atomic writes, no cleverness. Secrets never live here; decisions do.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

from agent.broker_errors import is_explicit_client_order_absence

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "state" / "ledger.json"
DECISIONS = ROOT / "state" / "decisions.jsonl"
STRUCTURE_META = ROOT / "state" / "positions_meta.json"


def atomic_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=".ledger-", suffix=".tmp", dir=str(path.parent))
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w") as handle:
            if isinstance(payload, (dict, list)):
                json.dump(payload, handle, indent=2, sort_keys=True)
            else:
                handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_ledger(path: Path = LEDGER) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"positions": [], "updated_utc": None}


def save_ledger(positions: list[dict], path: Path = LEDGER) -> dict:
    payload = {
        "positions": positions,
        "updated_utc": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write(path, payload)
    return payload


def append_decision(record: dict, path: Path = DECISIONS) -> None:
    """One JSON line per decision. Corruption-proof: append-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def ledger_positions(path: Path = LEDGER) -> list[dict]:
    return load_ledger(path).get("positions", [])


def mirror_from_broker(position_list, path: Path = LEDGER) -> dict:
    """Normalize broker positions to the ledger's shape and persist."""
    rows = []
    for p in position_list:
        rows.append({
            "symbol": p.symbol,
            "qty": int(float(p.qty)),
            "avg_entry_price": float(getattr(p, "avg_entry_price", 0) or 0),
            "market_value": float(getattr(p, "market_value", 0) or 0),
        })
    return save_ledger(rows, path)


@dataclass(frozen=True)
class StructureLedger:
    """Persistent structure records owned beside the position ledger.

    A broker position is a leg; the policy sizes and exits a structure.  This
    small repository is the only boundary allowed to know the JSON file that
    bridges those two representations across independent cycle processes.
    """

    path: Path = STRUCTURE_META

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {"groups": {}}

    def save(self, payload: dict) -> None:
        atomic_write(self.path, payload)

    def protected_open_order_ids(self) -> frozenset[str]:
        """Return lifecycle-owned orders that generic cleanup must not cancel."""
        return frozenset(
            str(order_id)
            for group in self.load().get("groups", {}).values()
            for pending, order_id in (
                (group.get("close_pending"), group.get("close_order_id")),
                (group.get("residual_equity_close_pending"),
                 group.get("residual_equity_close_order_id")),
            )
            if pending and order_id)

    def pending_entry_client_order_ids(self) -> frozenset[str]:
        """Return accepted entry identities awaiting definitive broker outcome."""
        return frozenset(
            str(client_order_id)
            for client_order_id in self.load().get("pending_entries", {})
            if client_order_id)

    def _group_record(self, proposal: Any) -> dict:
        """Serialize the strategy-owned facts of one eventual structure."""
        entry_net = sum(
            leg.mid * (1.0 if leg.side == "buy" else -1.0)
            for leg in proposal.legs)
        kind = ("credit" if proposal.structure in
                ("credit_vertical", "iron_condor") else "debit")
        ref_amount = -entry_net if kind == "credit" else entry_net
        return {
            "engine": proposal.engine,
            "underlying": proposal.underlying,
            "structure": proposal.structure,
            "expiry": proposal.expiry.isoformat() if proposal.expiry else "",
            "kind": kind,
            "entry_net": round(entry_net, 4),
            "ref_amount": round(abs(ref_amount), 4),
            "max_loss_dollars": proposal.max_loss_dollars,
            "take_profit_fraction": 0.0,
            "stop_loss_fraction": 0.0,
            "event_exit_date": proposal.event_exit_date,
            "event_exit_time": proposal.event_exit_time,
            "legs": {leg.symbol: {"side": leg.side, "qty": leg.quantity,
                                  "entry_mid": leg.mid}
                     for leg in proposal.legs},
            "closed": False,
        }

    @staticmethod
    def _expected_order_quantity(proposal: Any) -> int:
        quantities = {int(leg.quantity) for leg in proposal.legs}
        if not quantities or len(quantities) != 1 or next(iter(quantities)) <= 0:
            raise ValueError("entry legs do not express one positive order quantity")
        return next(iter(quantities))

    def record_entry(self, proposal: Any, entered_at: str) -> str:
        """Persist a broker-confirmed structure and return its group identity.

        This compatibility method is intentionally for already-confirmed
        entries.  The live submission path records a pending entry first and
        promotes it here only after exact broker reconciliation.
        """
        # Imported here to keep this persistence module independent of the
        # strategy engine at module load, while keeping group-id construction
        # in the single strategy function that also reconstructs exits.
        from strategy.exits import group_key

        groups = self.load()
        group_id = group_key(
            proposal.engine, proposal.underlying,
            proposal.expiry.isoformat() if proposal.expiry else "", entered_at)
        groups.setdefault("groups", {})[group_id] = self._group_record(proposal)
        self.save(groups)
        return group_id

    def record_pending_entry(self, proposal: Any, entered_at: str,
                             entry_order_id: str, *,
                             take_profit: float = 0.0,
                             stop_loss: float = 0.0,
                             pre_expiry_underlying_qty: int | None = None) -> str:
        """Persist an accepted order without claiming a position exists yet."""
        from strategy.exits import group_key

        order_id = str(entry_order_id)
        if not order_id:
            raise ValueError("pending entry requires a broker order id")
        group_id = group_key(
            proposal.engine, proposal.underlying,
            proposal.expiry.isoformat() if proposal.expiry else "", entered_at,
            entry_identity=order_id)
        meta = self.load()
        pending = meta.setdefault("pending_entries", {})
        if order_id in pending:
            raise ValueError(f"duplicate pending entry order id {order_id}")
        group = self._group_record(proposal)
        group["take_profit_fraction"] = take_profit
        group["stop_loss_fraction"] = stop_loss
        if pre_expiry_underlying_qty is not None:
            if not isinstance(pre_expiry_underlying_qty, int):
                raise ValueError("pre-expiry underlying quantity must be an integer")
            group["pre_expiry_underlying_qty"] = pre_expiry_underlying_qty
        pending[order_id] = {
            "group_id": group_id,
            "expected_qty": self._expected_order_quantity(proposal),
            "group": group,
            "reconciliation_required": False,
        }
        self.save(meta)
        return group_id

    def reconcile_pending_entries(
            self, get_order: Callable[[str], Any]) -> "EntryReconciliation":
        """Promote only full fills; retain uncertainty rather than guessing."""
        meta = self.load()
        pending = meta.setdefault("pending_entries", {})
        groups = meta.setdefault("groups", {})
        outcomes = meta.setdefault("entry_outcomes", {})
        activated: list[str] = []
        discarded: list[str] = []
        quarantined: list[str] = []
        changed = False

        for order_id, entry in list(pending.items()):
            expected = entry.get("expected_qty")
            try:
                expected = int(expected)
            except (TypeError, ValueError):
                expected = 0
            try:
                order = get_order(order_id)
                status = _broker_status(order)
                filled = _broker_quantity(order)
            except Exception as exc:  # noqa: BLE001
                if is_explicit_client_order_absence(exc, order_id):
                    outcomes[order_id] = {
                        "code": "ENTRY_NOT_SUBMITTED",
                        "broker_status": "not_found",
                        "filled_qty": 0,
                        "group_id": entry.get("group_id"),
                    }
                    del pending[order_id]
                    discarded.append(order_id)
                    changed = True
                    continue
                status, filled = "UNKNOWN", None

            if status == "FILLED" and filled == expected and expected > 0:
                group = entry.get("group")
                group_id = entry.get("group_id")
                if not isinstance(group, dict) or not group_id:
                    entry["reconciliation_required"] = True
                    entry["reconciliation_detail"] = "entry_record_malformed"
                    quarantined.append(order_id)
                    changed = True
                    continue
                group = dict(group)
                group["entry_order_id"] = order_id
                broker_order_id = _broker_value(order, "id")
                if broker_order_id:
                    group["entry_broker_order_id"] = str(broker_order_id)
                group["entry_filled_qty"] = filled
                group["entry_broker_status"] = status.lower()
                groups[str(group_id)] = group
                del pending[order_id]
                activated.append(str(group_id))
                changed = True
                continue

            if status in {"CANCELED", "REJECTED", "EXPIRED"} and filled == 0:
                outcomes[order_id] = {
                    "code": "ENTRY_NOT_FILLED",
                    "broker_status": status.lower(),
                    "filled_qty": 0,
                    "group_id": entry.get("group_id"),
                }
                del pending[order_id]
                discarded.append(order_id)
                changed = True
                continue

            if status in {"FILLED", "PARTIALLY_FILLED", "UNKNOWN", ""}:
                entry["reconciliation_required"] = True
                detail_status = status.lower() or "unknown"
                entry["reconciliation_detail"] = (
                    f"entry_{detail_status}_{filled}_of_{expected}")
                quarantined.append(order_id)
                changed = True

        if changed:
            self.save(meta)
        return EntryReconciliation(tuple(activated), tuple(discarded),
                                   tuple(quarantined))

    def set_exit_thresholds(self, group_id: str, *, take_profit: float,
                            stop_loss: float) -> None:
        groups = self.load()
        group = groups.get("groups", {}).get(group_id)
        if group is None:
            return
        group["take_profit_fraction"] = take_profit
        group["stop_loss_fraction"] = stop_loss
        self.save(groups)

    def unresolved_structure_close_count(self) -> int:
        """Count close outcomes that require reconciliation before new risk."""
        return sum(
            1 for group in self.load().get("groups", {}).values()
            if not group.get("closed") and group.get("reconciliation_required")
        )

    def unresolved_entry_reconciliation_count(self) -> int:
        """Count broker-accepted entries not yet resolved to fill/no-fill."""
        return len(self.load().get("pending_entries", {}))


class EntryReconciliation(NamedTuple):
    """The only externally useful facts from pending-entry reconciliation."""

    activated: tuple[str, ...]
    discarded: tuple[str, ...]
    quarantined: tuple[str, ...]


def _broker_status(order: Any) -> str:
    return str(_broker_value(order, "status") or "").rsplit(".", 1)[-1].upper()


def _broker_quantity(order: Any) -> int | None:
    try:
        return int(float(_broker_value(order, "filled_qty")))
    except (TypeError, ValueError):
        return None


def _broker_value(order: Any, key: str) -> Any:
    return order.get(key) if isinstance(order, dict) else getattr(order, key, None)
