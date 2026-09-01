"""Policy facts permitted to appear in public deliverables.

Documentation, media, and their checker share this narrow projection instead
of each reconstructing dollar values from raw manifest paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def _get(manifest, *path, default=...):
    if isinstance(manifest, Mapping):
        value = manifest
        for part in path:
            if not isinstance(value, Mapping) or part not in value:
                if default is ...:
                    raise KeyError(path)
                return default
            value = value[part]
        return value
    return manifest.get(*path, default=default)


@dataclass(frozen=True)
class PublicPolicyClaims:
    """The public value vocabulary projected from declared policy facts."""

    risk_values: dict[str, float]
    legal_dollar_values: frozenset[int]


def project_public_claims(manifest) -> PublicPolicyClaims:
    """Expose only human-facing values that a deliverable may claim."""
    starting_equity = float(_get(
        manifest, "environment", "required_starting_equity"))
    risk = _get(manifest, "risk_caps")
    risk_values = {
        "per_trade_hard_cap": (
            starting_equity * float(risk["max_loss_per_position_fraction"])),
        "at_risk_cap": starting_equity * float(risk["at_risk_cap_fraction"]),
        "daily_kill": starting_equity * float(risk["daily_loss_kill_fraction"]),
        "equity_floor": starting_equity * float(risk["equity_floor_fraction"]),
    }
    legal = {round(value) for value in risk_values.values()}
    legal.add(round(starting_equity * float(
        risk["daily_new_exposure_cap_fraction"])))
    legal.add(round(starting_equity))
    for config in _get(manifest, "strategies", default={}).values():
        if not isinstance(config, Mapping):
            continue
        for key, value in config.items():
            if key.startswith("_") or "fraction" not in key:
                continue
            try:
                legal.add(round(starting_equity * float(value)))
            except (TypeError, ValueError):
                continue
    return PublicPolicyClaims(risk_values, frozenset(legal))
