#!/usr/bin/env python3
"""Load the Strategy Policy Manifest and bind an immutable identity to it.

Why the manifest has a SHA
--------------------------
"Which parameters was the agent running when it made that trade?" has to be
answerable after the fact, from the trade record alone. A version string does
not do that — it is a promise a human made, and humans edit files without
bumping versions. The SHA is computed from the canonical content, so it changes
whether or not anyone remembered to.

Every decision the agent logs carries this SHA. A forward record produced under
a different SHA is a different experiment and is never silently compared to
this one.

Keys beginning with ``_comment`` are documentation and are excluded from the
identity, so clarifying a comment does not invalidate a run in progress.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.json"


def _strip_comments(node: Any) -> Any:
    """Remove documentation keys before hashing."""
    if isinstance(node, dict):
        return {k: _strip_comments(v) for k, v in node.items()
                if not k.startswith("_comment")}
    if isinstance(node, list):
        return [_strip_comments(v) for v in node]
    return node


class OrderShape(NamedTuple):
    """The wire form of an order this policy may emit.

    One declaration both builds an order and validates one seen at the broker.
    Two declarations would eventually disagree, and the disagreement would
    surface as a live order nobody authorized.
    """
    id: str
    asset_class: str
    order_class: str
    type: str
    time_in_force: str
    legs: int

    def matches(self, *, order_class: str, type: str, time_in_force: str,
                legs: int) -> bool:
        return (self.order_class == order_class and self.type == type
                and self.time_in_force == time_in_force and self.legs == legs)


class LossBudget(str, Enum):
    """Named strategy loss intents; JSON field names stay inside policy."""

    STANDARD = "standard"
    PRE_EVENT = "pre_event"
    PEAD = "pead"
    GAP_ADDON = "gap_addon"


_LOSS_BUDGET_FIELDS = {
    LossBudget.STANDARD: "max_loss_per_trade_fraction",
    LossBudget.PRE_EVENT: "pre_event_max_loss_per_trade_fraction",
    LossBudget.PEAD: "pead_max_loss_per_trade_fraction",
    LossBudget.GAP_ADDON: "addon_max_loss_per_trade_fraction",
}

_EXIT_LOSS_FIELDS = {
    "fraction": "stop_loss_fraction",
    "multiple": "stop_loss_multiple",
}

_FINAL_DAY_EVENT_LABELS = {"nfp_gap": "event-nfp-gap"}


@dataclass(frozen=True)
class EntryWindow:
    """The one authorized new-exposure window for a strategy engine."""

    timezone: str
    opens_at: str
    closes_at: str

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown policy timezone {self.timezone!r}") from exc
        if _clock_time(self.opens_at) >= _clock_time(self.closes_at):
            raise ValueError("policy entry window must open before it closes")

    def contains(self, now_utc: datetime) -> bool:
        return self.phase_at(now_utc) == "inside"

    def phase_at(self, now_utc: datetime) -> str:
        """Return ``before``, ``inside``, or ``after`` for a UTC instant."""
        local = now_utc.astimezone(ZoneInfo(self.timezone)).time()
        start = _clock_time(self.opens_at)
        end = _clock_time(self.closes_at)
        if local < start:
            return "before"
        if local >= end:
            return "after"
        return "inside"


@dataclass(frozen=True)
class RiskLimits:
    """Cross-strategy risk policy facts derived from declared authority."""

    starting_equity: float
    at_risk_cap: float
    daily_new_exposure_cap: float
    per_position_loss_cap: float
    equity_floor: float
    daily_loss_kill_fraction: float
    drawdown_scale_fraction: float
    max_concurrent_positions: int
    max_positions_per_underlying: int


@dataclass(frozen=True)
class FinalDayRules:
    """Final-day entry freeze and required lifecycle schedule."""

    trading_date: date
    entry_freeze_enabled: bool
    flatten_at: str
    event_exception: str

    def is_entry_frozen(self, trading_date: date) -> bool:
        return self.entry_freeze_enabled and trading_date == self.trading_date

    def allows_event_candidate(self, trading_date: date, label: str) -> bool:
        """Whether a declared Event Vector candidate survives this date."""
        return (not self.is_entry_frozen(trading_date)
                or label == _FINAL_DAY_EVENT_LABELS[self.event_exception])


@dataclass(frozen=True)
class ExitIntent:
    """Declared thresholds; loss is a fraction for debit and multiple for credit."""

    take_profit: float
    stop_loss_factor: float


def _clock_time(value: str):
    hour, minute = map(int, value.split(":"))
    from datetime import time
    return time(hour, minute)


class Manifest:
    """Read-only view over the manifest. Never mutated after construction."""

    def __init__(self, raw: dict):
        self._raw = raw
        self.sha = hashlib.sha256(
            json.dumps(_strip_comments(raw), sort_keys=True,
                       separators=(",", ":")).encode()
        ).hexdigest()
        self.policy_id: str = raw["policy_id"]
        self.version: str = raw["version"]
        self.order_shapes = tuple(
            OrderShape(**{k: v for k, v in shape.items()
                          if not k.startswith("_comment")})
            for shape in raw["order_shapes"]
        )
        self._shapes_by_id = {shape.id: shape for shape in self.order_shapes}
        if len(self._shapes_by_id) != len(self.order_shapes):
            raise ValueError("manifest order shape ids must be unique")
        competition = raw.get("competition")
        if not isinstance(competition, dict):
            raise ValueError("manifest competition policy must be a mapping")
        required = competition.get("requires_options_component")
        if not isinstance(required, bool):
            raise ValueError(
                "manifest competition.requires_options_component must be boolean")
        self.competition_requires_options_component = required
        if required:
            self._validate_options_composition()
        self._validate_semantic_policy()

    def _validate_options_composition(self) -> None:
        """Refuse a competition strategy that has no declared option component.

        This is deliberately narrower than an Alpaca asset-class restriction.
        A future mixed strategy may declare equities, ETFs, or crypto alongside
        ``us_option``.  A standalone non-options strategy needs a fresh
        competition-eligibility decision before it can enter this manifest.
        """
        for name, config in self.get("strategies").items():
            if name.startswith("_comment"):
                continue
            if not isinstance(config, dict):
                raise ValueError(f"strategy {name} must be a mapping")
            shape_ids = config.get("execution_shape_ids")
            if not isinstance(shape_ids, list) or not shape_ids or not all(
                    isinstance(shape_id, str) for shape_id in shape_ids):
                raise ValueError(
                    f"strategy {name} must declare execution_shape_ids")
            unknown = sorted(set(shape_ids) - set(self._shapes_by_id))
            if unknown:
                raise ValueError(
                    f"strategy {name} declares unknown order shape(s): "
                    f"{', '.join(unknown)}")
            if "us_option" not in self.strategy_execution_asset_classes(name):
                raise ValueError(
                    f"competition strategy {name} must incorporate us_option")

    def _validate_semantic_policy(self) -> None:
        """Reject an incomplete parameter authority before it can pass gates."""
        self.risk_limits()
        self.final_day_rules()
        self.event_gap_entry_limit()
        for engine, config in self.get("strategies").items():
            if engine.startswith("_comment"):
                continue
            if not isinstance(config, dict):
                raise ValueError(f"strategy {engine} must be a mapping")
            self.entry_window_for(engine)
            self.engine_loss_cap(engine)
            structures = config.get("structures")
            if not isinstance(structures, list) or not structures:
                raise ValueError(f"strategy {engine} must declare structures")
            for structure in structures:
                self.exit_intent_for(engine, structure)

    def strategy_execution_asset_classes(self, name: str) -> frozenset[str]:
        """Asset classes reachable by one strategy's declared wire shapes."""
        shape_ids = self.get("strategies", name, "execution_shape_ids")
        return frozenset(self._shapes_by_id[shape_id].asset_class
                         for shape_id in shape_ids)

    def residual_equity_close_shape(self) -> OrderShape:
        """Return the one declared stock-only emergency close wire shape."""
        shape = self._shapes_by_id.get("residual_equity_close_limit_day")
        if (shape is None or shape.asset_class != "us_equity" or
                not shape.matches(order_class="simple", type="limit",
                                  time_in_force="day", legs=1)):
            raise ValueError(
                "manifest must declare residual_equity_close_limit_day as "
                "a one-leg Day equity limit close")
        return shape

    @property
    def identity(self) -> str:
        return f"{self.policy_id}@{self.version}+{self.sha[:12]}"

    def get(self, *path, default=...):
        node = self._raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                if default is ...:
                    raise KeyError(
                        f"manifest has no {'.'.join(map(str, path))}; the "
                        f"manifest is the parameter authority, so a missing "
                        f"key is a policy error, not a reason to guess")
                return default
            node = node[key]
        return node

    def declared_symbols(self) -> frozenset:
        return frozenset(self.get("universe", "core")
                         + self.get("universe", "satellite"))

    def entry_window_for(self, engine: str) -> EntryWindow:
        """Return the declared entry window, including a strategy exception.

        Gate callers should ask this policy fact rather than reconstructing the
        exception from a session field plus a strategy-specific override.
        """
        return EntryWindow(
            timezone=str(self.get("session", "timezone")),
            opens_at=str(self.get("strategies", engine,
                                  "entry_open_override",
                                  default=self.get(
                                      "session", "no_new_exposure_before"))),
            closes_at=str(self.get("session", "no_new_exposure_after")),
        )

    def risk_limits(self) -> RiskLimits:
        """Project all cross-strategy risk limits into their usable units."""
        starting_equity = float(
            self.get("environment", "required_starting_equity"))

        def amount(name: str) -> float:
            return starting_equity * float(self.get("risk_caps", name))

        return RiskLimits(
            starting_equity=starting_equity,
            at_risk_cap=amount("at_risk_cap_fraction"),
            daily_new_exposure_cap=amount("daily_new_exposure_cap_fraction"),
            per_position_loss_cap=amount("max_loss_per_position_fraction"),
            equity_floor=amount("equity_floor_fraction"),
            daily_loss_kill_fraction=float(
                self.get("risk_caps", "daily_loss_kill_fraction")),
            drawdown_scale_fraction=float(
                self.get("risk_caps", "drawdown_scale_fraction")),
            max_concurrent_positions=int(
                self.get("risk_caps", "max_concurrent_positions")),
            max_positions_per_underlying=int(
                self.get("risk_caps", "max_positions_per_underlying")),
        )

    def engine_loss_cap(self, engine: str, *, scale: float = 1.0,
                        budget: LossBudget = LossBudget.STANDARD) -> float:
        """Return one engine's scaled maximum loss in dollars."""
        config = self.get("strategies", engine)
        if not isinstance(config, dict):
            raise KeyError(f"strategy {engine} is not a mapping")
        fraction = float(self.get("strategies", engine,
                                  _LOSS_BUDGET_FIELDS[budget]))
        return self.risk_limits().starting_equity * fraction * scale

    def event_gap_entry_limit(self) -> int:
        """Return the declared total admission budget for Event Vector gaps."""
        limit = int(self.get("strategies", "event_macro",
                             "gap_max_entries_total"))
        if limit < 1:
            raise ValueError("event gap entry limit must be positive")
        return limit

    def final_day_rules(self) -> FinalDayRules:
        """Return the final-day rules shared by generation and exits."""
        event_exception = self.get("session", "final_day_event_exception")
        if event_exception not in _FINAL_DAY_EVENT_LABELS:
            raise ValueError(
                "session.final_day_event_exception must be a known event concept")
        return FinalDayRules(
            trading_date=date.fromisoformat(
                str(self.get("session", "final_trading_date"))),
            entry_freeze_enabled=bool(
                self.get("session", "no_new_exposure_on_final_date")),
            flatten_at=str(self.get("session", "flatten_all_at")),
            event_exception=event_exception,
        )

    def exit_intent_for(self, engine: str, structure: str) -> ExitIntent:
        """Return exit thresholds for a submitted structure's declared intent."""
        config = self.get("strategies", engine)
        if not isinstance(config, dict):
            raise KeyError(f"strategy {engine} is not a mapping")
        structures = self.get("strategies", engine, "structures")
        if structure not in structures:
            raise ValueError(
                f"strategy {engine} does not declare structure {structure}")
        take_profit = float(self.get("strategies", engine,
                                     "take_profit_fraction"))
        loss_kind = self.get("strategies", engine, "exit_loss_kind")
        try:
            stop_key = _EXIT_LOSS_FIELDS[loss_kind]
        except KeyError as exc:
            raise ValueError(
                f"strategy {engine} declares unknown exit_loss_kind {loss_kind!r}") \
                from exc
        return ExitIntent(
            take_profit=take_profit,
            stop_loss_factor=float(self.get("strategies", engine, stop_key)),
        )

    def find_shape(self, *, order_class: str, type: str, time_in_force: str,
                   legs: int) -> OrderShape | None:
        """The declared shape matching these wire attributes, or None.

        None means refuse. It never means "close enough, submit it anyway".
        """
        for shape in self.order_shapes:
            if shape.matches(order_class=order_class, type=type,
                             time_in_force=time_in_force, legs=legs):
                return shape
        return None

    def find_shape_for_strategy(self, strategy: str, *, order_class: str,
                                type: str, time_in_force: str,
                                legs: int) -> OrderShape | None:
        """Return a declared wire shape only when this strategy owns it.

        ``order_shapes`` is the global vocabulary.  A strategy's
        ``execution_shape_ids`` is its capability subset.  Entries must cross
        both checks, so declaring a safe option shape for eligibility cannot
        authorize a different strategy to emit an unrelated global shape.
        """
        config = self.get("strategies").get(strategy)
        if not isinstance(config, dict):
            return None
        shape = self.find_shape(order_class=order_class, type=type,
                                time_in_force=time_in_force, legs=legs)
        if shape is None or shape.id not in config["execution_shape_ids"]:
            return None
        return shape


def load(path: Path = MANIFEST_PATH) -> Manifest:
    return Manifest(json.loads(path.read_text()))


if __name__ == "__main__":
    m = load()
    print(f"MANIFEST_IDENTITY={m.identity}")
    print(f"MANIFEST_SHA={m.sha}")
    print(f"DECLARED_SHAPES={[s.id for s in m.order_shapes]}")
    print(f"DECLARED_SYMBOLS={sorted(m.declared_symbols())}")
