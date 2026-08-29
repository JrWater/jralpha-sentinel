"""Competition eligibility must be a manifest property, not a prose claim."""
from __future__ import annotations

import copy

import pytest

from policy.loader import Manifest, load


def _raw() -> dict:
    return copy.deepcopy(load()._raw)


def test_every_declared_strategy_has_an_explicit_option_component() -> None:
    manifest = Manifest(_raw())

    assert manifest.competition_requires_options_component is True
    for name in manifest.get("strategies"):
        if name.startswith("_comment"):
            continue
        assert "us_option" in manifest.strategy_execution_asset_classes(name)


def test_competition_manifest_refuses_a_strategy_without_option_component() -> None:
    raw = _raw()
    raw["order_shapes"].append({
        "id": "equity_limit_day", "asset_class": "us_equity",
        "order_class": "simple", "type": "limit",
        "time_in_force": "day", "legs": 1,
    })
    raw["strategies"]["trend_income"]["execution_shape_ids"] = [
        "equity_limit_day"
    ]

    with pytest.raises(ValueError, match="trend_income.*us_option"):
        Manifest(raw)


def test_strategy_component_is_derived_from_declared_order_shapes() -> None:
    raw = _raw()
    raw["strategies"]["trend_income"]["execution_shape_ids"] = [
        "not-a-declared-shape"
    ]

    with pytest.raises(ValueError, match="trend_income.*not-a-declared-shape"):
        Manifest(raw)


def test_missing_strategy_component_declaration_fails_closed() -> None:
    raw = _raw()
    del raw["strategies"]["trend_income"]["execution_shape_ids"]

    with pytest.raises(ValueError, match="trend_income.*execution_shape_ids"):
        Manifest(raw)
