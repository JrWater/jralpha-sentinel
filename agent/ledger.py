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
from typing import Any

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

    def record_entry(self, proposal: Any, entered_at: str) -> str:
        """Persist one accepted entry and return its durable group identity."""
        # Imported here to keep this persistence module independent of the
        # strategy engine at module load, while keeping group-id construction
        # in the single strategy function that also reconstructs exits.
        from strategy.exits import group_key

        groups = self.load()
        group_id = group_key(
            proposal.engine, proposal.underlying,
            proposal.expiry.isoformat() if proposal.expiry else "", entered_at)
        entry_net = sum(
            leg.mid * (1.0 if leg.side == "buy" else -1.0)
            for leg in proposal.legs)
        kind = ("credit" if proposal.structure in
                ("credit_vertical", "iron_condor") else "debit")
        ref_amount = -entry_net if kind == "credit" else entry_net
        groups.setdefault("groups", {})[group_id] = {
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
        self.save(groups)
        return group_id

    def set_exit_thresholds(self, group_id: str, *, take_profit: float,
                            stop_loss: float) -> None:
        groups = self.load()
        group = groups.get("groups", {}).get(group_id)
        if group is None:
            return
        group["take_profit_fraction"] = take_profit
        group["stop_loss_fraction"] = stop_loss
        self.save(groups)
