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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "state" / "ledger.json"
DECISIONS = ROOT / "state" / "decisions.jsonl"


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
