#!/usr/bin/env python3
"""The public snapshot: what a judge sees, written by the agent itself.

Why a snapshot rather than a live dashboard
-------------------------------------------
The submission requires a public, interactive Application URL. The obvious
build is a Streamlit app that holds the Alpaca keys and queries the account
live — which means putting the competition account's credentials into a
hosted service so that anyone who visits the page is, indirectly, trading with
them. That is a bad trade for a page whose entire job is to *display* state.

So the agent writes a credential-free JSON snapshot at the end of every cycle
and the dashboard renders that. The page needs no secrets, it works on
Streamlit Community Cloud with zero configuration, and the worst case if the
hosting is compromised is that someone reads numbers that are on the
submission form anyway.

The account number is deliberately included: the rules require submitting it
so judges can verify the P&L themselves.

What goes in is chosen to make the project's central claim checkable rather
than merely asserted: every gate with its dimension, severity and verdict, and
every proposal the model made together with what the gates did to it. A
refused proposal is more informative than an accepted one, so refusals are
never dropped from the tail.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Deliberately under docs/ and NOT gitignored: this file is a deliverable, and
# the dashboard reads it straight from the public repo.
SNAPSHOT = ROOT / "docs" / "snapshot.json"

MAX_EQUITY_POINTS = 500
MAX_DECISIONS = 120
SCHEMA_VERSION = 2


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _group_positions(positions: list) -> list:
    """Group option legs into the structures they belong to.

    A vertical is two Alpaca positions but one decision, one risk number and
    one exit. Showing legs would let a reader count twelve "positions" where
    the policy sees six structures, and the per-underlying cap counts
    contracts precisely because these two units are not interchangeable.
    """
    out = []
    for p in positions:
        symbol = getattr(p, "symbol", "")
        # OCC: root, then YYMMDD, then C/P, then strike*1000
        root = symbol[:-15] if len(symbol) > 15 else symbol
        out.append({
            "symbol": symbol,
            "underlying": root,
            "qty": int(_f(getattr(p, "qty", 0))),
            "avg_entry_price": round(_f(getattr(p, "avg_entry_price", 0)), 4),
            "market_value": round(_f(getattr(p, "market_value", 0)), 2),
            "unrealized_pl": round(_f(getattr(p, "unrealized_pl", 0)), 2),
        })
    return sorted(out, key=lambda r: (r["underlying"], r["symbol"]))


def _read_previous() -> dict:
    try:
        return json.loads(SNAPSHOT.read_text())
    except (OSError, ValueError):
        return {}


class DecisionSchemaError(ValueError):
    """A decision record carries the retired `accepted` field.

    `accepted` was written before the pretrade gates ran, so it meant "the
    proposer picked this and preflight was clean" while reading, in public, as
    "the gates let this trade through". On 2026-08-27 it conflated one real
    isolated legacy-paper submission with fourteen competition-account
    proposals that pretrade refused — the public record could not distinguish
    opposite outcomes.

    Four separate facts replace it: `selected`, `authorized`, `submitted` (the
    broker accepted the request) and the reconciled fill. Refusing the old
    field here, on both new records and history carried forward, is what stops
    the retired meaning from being republished by accumulation.
    """


def _refuse_legacy_accepted(rows) -> None:
    for row in rows:
        if "accepted" in row:
            raise DecisionSchemaError(
                "decision record carries the retired `accepted` field "
                f"({row.get('engine', '?')} {row.get('underlying', '?')} at "
                f"{row.get('at', '?')}); use selected / authorized / "
                f"submitted / filled instead. Correct existing history with "
                f"scripts/correct_snapshot_decisions.py.")


def build(*, manifest, account, clock, gate_results, gates, permit_status: str,
          blockers, positions: list, decisions: list, git_head: str | None,
          git_dirty: bool | None, regime=None, day_state: dict | None = None,
          decision_updates: dict[str, dict] | None = None,
          now_utc: datetime | None = None) -> dict:
    """Assemble the snapshot. Pure: callers decide when to write it."""
    now = now_utc or datetime.now(timezone.utc)
    previous = _read_previous()

    equity = _f(getattr(account, "equity", 0))
    start = _f(manifest.get("environment", "required_starting_equity"), 100000.0)

    history = list(previous.get("equity_history", []))
    history.append({"t": now.isoformat(), "equity": round(equity, 2)})
    history = history[-MAX_EQUITY_POINTS:]

    by_name = {g.name: g for g in gates}
    gate_rows = []
    for name, result in gate_results.items():
        gate = by_name.get(name)
        gate_rows.append({
            "name": name,
            "dimension": gate.dimension if gate else "unknown",
            # An unregistered gate is BLOCKING, never a softer default.
            "severity": gate.severity if gate else "BLOCKING",
            "phase": gate.phase if gate else "unknown",
            "ok": bool(result.ok),
            "detail": result.detail,
            "rationale": gate.rationale if gate else "",
        })
    gate_rows.sort(key=lambda r: (r["dimension"], r["name"]))

    tail = copy.deepcopy(
        list(previous.get("decisions", [])) + list(decisions))
    for row in tail:
        client_id = row.get("client_order_id")
        if client_id and client_id in (decision_updates or {}):
            row.update(decision_updates[client_id])
    _refuse_legacy_accepted(tail)
    refused = [d for d in tail if d.get("refused_by")]
    kept = tail[-MAX_DECISIONS:]
    # Never let a run of submitted proposals push every refusal out of the tail:
    # the refusals are the evidence that the gates do anything at all.
    for row in refused[-20:]:
        if row not in kept:
            kept.insert(0, row)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": now.isoformat(),
        "policy": {
            "identity": manifest.identity,
            "policy_id": manifest.policy_id,
            "version": manifest.version,
            "sha": manifest.sha,
        },
        "code": {"git_head": git_head, "worktree_dirty": git_dirty},
        "account": {
            "account_number": getattr(account, "account_number", None),
            "mode": "PAPER",
            "equity": round(equity, 2),
            "starting_equity": start,
            "cash": round(_f(getattr(account, "cash", 0)), 2),
            "options_buying_power": round(
                _f(getattr(account, "options_buying_power", 0)), 2),
            "pnl_dollars": round(equity - start, 2),
            "pnl_percent": round((equity / start - 1.0) * 100.0, 3) if start else 0.0,
            "options_level": getattr(account, "options_trading_level", None),
        },
        "market": {"is_open": bool(getattr(clock, "is_open", False))},
        "permit": {
            "status": permit_status,
            "blocking_gates": list(blockers),
            # The permit gates NEW exposure only. Exits and reconciliation run
            # regardless, which is what Entry Maintenance means in practice.
            "entry_maintenance": permit_status != "READY",
        },
        "gates": gate_rows,
        "regime": ({
            "mode": getattr(regime, "mode", None),
            "confidence": getattr(regime, "confidence", None),
            "reason": getattr(regime, "reason", ""),
        } if regime is not None else None),
        "day": day_state or {},
        "positions": _group_positions(positions),
        "decisions": kept[-MAX_DECISIONS:],
        "equity_history": history,
    }


def write(payload: dict, path: Path | None = None) -> Path:
    # Resolve at call time: the isolated paper-cycle harness replaces the
    # module path after import. A default bound at function definition time
    # silently wrote its test decision into the public production snapshot.
    path = path or SNAPSHOT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path
