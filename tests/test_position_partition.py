"""Broker position classification shared by the cycle and public status."""
from types import SimpleNamespace

from strategy.data import parse_contract, partition_positions


def test_partition_positions_preserves_non_option_broker_exposure():
    options, non_options = partition_positions([
        SimpleNamespace(symbol="TSLA260904C00367500", asset_class="AssetClass.US_OPTION"),
        SimpleNamespace(symbol="TSLA", asset_class="AssetClass.US_EQUITY"),
    ])

    assert [position.symbol for position in options] == ["TSLA260904C00367500"]
    assert [position.symbol for position in non_options] == ["TSLA"]


def test_calendar_invalid_occ_symbol_is_not_a_contract():
    assert parse_contract("TSLA269931C00367500") is None
