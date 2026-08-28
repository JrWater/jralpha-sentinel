"""Pure public projection of one decision's lifecycle facts."""
from __future__ import annotations

from typing import NamedTuple


class DecisionView(NamedTuple):
    icon: str
    label: str
    account_scope: str


def summarize_decisions(rows: list[dict]) -> dict[str, int]:
    return {
        "recorded": len(rows),
        "submitted": sum(bool(row.get("submitted")) for row in rows),
        "refused": sum(bool(row.get("refused_by")) for row in rows),
    }


def proposer_summary(row: dict) -> str:
    """Human-readable evidence of whether a model actually made the choice."""
    evidence = row.get("proposer") or {}
    if evidence.get("decision_mode") == "llm":
        return (f"LLM · {evidence.get('provider', 'unknown')} / "
                f"{evidence.get('model', 'unknown')}")
    reason = evidence.get("fallback_reason") or "unspecified"
    return f"deterministic fallback · {reason}"


def classify_decision(row: dict) -> DecisionView:
    scope = str(row.get("account_scope") or "unspecified")
    refused_by = list(row.get("refused_by") or ())
    if row.get("lifecycle_status") == "closed":
        return DecisionView("✅", "filled and closed", scope)
    if str(row.get("broker_status", "")).lower() == "filled":
        return DecisionView("✅", "filled", scope)
    if row.get("submitted"):
        status = str(row.get("broker_status") or "broker accepted")
        return DecisionView("📤", f"submitted · {status}", scope)
    if row.get("submission_uncertain"):
        return DecisionView(
            "⚠️", "submission uncertain; reconciliation pending", scope)
    if refused_by:
        return DecisionView("⛔", f"refused by {', '.join(refused_by)}", scope)
    if row.get("authorized"):
        return DecisionView("⚠️", "authorized, not submitted", scope)
    if row.get("selected"):
        return DecisionView("◻️", "selected, not authorized", scope)
    return DecisionView("·", "not selected", scope)
