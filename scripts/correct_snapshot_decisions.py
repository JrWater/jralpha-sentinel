#!/usr/bin/env python3
"""Reclassify published decisions whose old `accepted` field was ambiguous.

Until 2026-08-27 the snapshot was built before the pretrade gates ran, so its
`accepted` flag meant "the proposer picked this and preflight was clean" while
reading, in public, as "the gates let this trade through". Most affected rows
were competition-account proposals refused before submission, but an isolated
legacy-paper end-to-end test was also copied into the same snapshot. A global
"zero orders" assumption would therefore replace one false history with
another.

The records are not deleted. Deleting them would remove the evidence that the
gates refused these proposals, which is the very claim the write-up asks a
judge to check. They are reclassified into the four facts that were always
distinct — selected, authorized, submitted, and the refusal source — and
stamped with why.

The function is pure and takes its evidence as arguments: same inputs, same
output, no clock, no I/O. A reviewer can re-run it and get the identical file,
which is the only way a correction to a public record is worth anything.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "snapshot.json"

REFUSED_CORRECTION_CODE = "LEGACY_ACCEPTED_BEFORE_PRETRADE"
SUBMITTED_CORRECTION_CODE = "LEGACY_ACCEPTED_REAL_SUBMISSION"
REFUSED_CORRECTION_REASON = (
    "The snapshot was published before the pretrade gates ran, so `accepted` "
    "could not observe a pretrade refusal. Reclassified from cycle evidence; "
    "the broker held no orders in this window."
)
SUBMITTED_CORRECTION_REASON = (
    "The old `accepted` field also captured an isolated legacy-paper test. "
    "Reclassified from the test journal and broker order history as a filled "
    "entry followed by a filled system-managed exit."
)


class CorrectionEvidenceError(ValueError):
    """The correction refused to guess.

    A public record is corrected from evidence or not at all. Rewriting rows
    by position, or on the assumption that the account was flat, would be the
    same class of unfounded claim the correction exists to remove.
    """


def _key(row: dict) -> tuple:
    return (row.get("at"), row.get("engine"), row.get("underlying"),
            row.get("structure"))


def correct_decisions(rows, *, cycle_evidence, broker_orders_by_account):
    """Reclassify `rows` from per-decision and per-account broker evidence."""

    evidence = {_key(row): row for row in cycle_evidence}
    corrected = []
    for row in rows:
        found = evidence.get(_key(row))
        if found is None:
            raise CorrectionEvidenceError(
                f"no cycle evidence for decision {_key(row)}; refusing to "
                f"reclassify a public record by position or assumption")
        scope = found.get("account_scope")
        if not scope or scope not in broker_orders_by_account:
            raise CorrectionEvidenceError(
                f"no broker evidence for account scope {scope!r}")
        orders = broker_orders_by_account[scope]
        order_by_id = {str(order.get("id")): order for order in orders}
        selected = found.get("selected")
        authorized = found.get("authorized")
        submitted = found.get("submitted")
        refused_by = list(found.get("refused_by", ()))
        if not all(isinstance(value, bool)
                   for value in (selected, authorized, submitted)):
            raise CorrectionEvidenceError(
                f"decision {_key(row)} lacks explicit boolean outcome facts")
        if submitted and not authorized:
            raise CorrectionEvidenceError(
                f"decision {_key(row)} claims submission without authorization")
        if authorized and refused_by:
            raise CorrectionEvidenceError(
                f"decision {_key(row)} is both authorized and refused")
        if not authorized and not refused_by:
            raise CorrectionEvidenceError(
                f"decision {_key(row)} is unauthorized without a refusal")

        broker_order_id = found.get("broker_order_id")
        broker_order = None
        if submitted:
            if not broker_order_id or str(broker_order_id) not in order_by_id:
                raise CorrectionEvidenceError(
                    f"broker evidence has no matching order {broker_order_id!r} "
                    f"for decision {_key(row)}")
            broker_order = order_by_id[str(broker_order_id)]
        elif orders:
            raise CorrectionEvidenceError(
                f"broker reported {len(orders)} order(s) for {scope}; a "
                "submitted=false correction needs an order-free evidence "
                "window")

        out = copy.deepcopy(row)
        out.pop("accepted", None)
        out["selected"] = selected
        out["authorized"] = authorized
        out["submitted"] = submitted
        out["account_scope"] = scope
        out["refused_by"] = refused_by
        for field in (
                "broker_order_id", "lifecycle_status",
                "closed_by_order_id"):
            if field in found:
                out[field] = found[field]
        if broker_order is not None:
            for source, target in (
                    ("status", "broker_status"),
                    ("filled_qty", "filled_qty"),
                    ("filled_avg_price", "filled_avg_price"),
                    ("submitted_at", "broker_submitted_at"),
                    ("filled_at", "broker_filled_at")):
                if source in broker_order:
                    out[target] = broker_order[source]
        closed_by = found.get("closed_by_order_id")
        if closed_by:
            close = order_by_id.get(str(closed_by))
            if close is None:
                raise CorrectionEvidenceError(
                    f"broker evidence has no matching close order "
                    f"{closed_by!r} for decision {_key(row)}")
            out["close_broker_status"] = close.get("status")
        out["correction"] = {
            "code": (SUBMITTED_CORRECTION_CODE if submitted
                     else REFUSED_CORRECTION_CODE),
            "reason": (SUBMITTED_CORRECTION_REASON if submitted
                       else REFUSED_CORRECTION_REASON),
        }
        corrected.append(out)
    return corrected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", default=str(SNAPSHOT))
    ap.add_argument("--evidence", required=True,
                    help="JSON list of per-decision cycle evidence")
    ap.add_argument("--broker-evidence", required=True,
                    help="JSON object mapping account scope to broker orders")
    ap.add_argument("--legacy-decisions",
                    help="captured pre-correction decision list; useful when "
                         "repairing a damaged snapshot container")
    ap.add_argument("--write", action="store_true",
                    help="write the corrected snapshot back (default: preview)")
    args = ap.parse_args()

    path = Path(args.snapshot)
    payload = json.loads(path.read_text(encoding="utf-8"))
    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    broker_evidence = json.loads(
        Path(args.broker_evidence).read_text(encoding="utf-8"))
    legacy_rows = (json.loads(Path(args.legacy_decisions).read_text(
        encoding="utf-8")) if args.legacy_decisions
        else payload.get("decisions", []))

    corrected = correct_decisions(legacy_rows,
                                  cycle_evidence=evidence,
                                  broker_orders_by_account=broker_evidence)
    payload["decisions"] = corrected
    payload["schema_version"] = 2
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.write:
        path.write_text(rendered + "\n", encoding="utf-8")
        print(f"corrected {len(corrected)} decision(s) in {path}")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
