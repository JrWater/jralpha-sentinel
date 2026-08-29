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
from pathlib import Path
from typing import Any, NamedTuple

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

    def strategy_execution_asset_classes(self, name: str) -> frozenset[str]:
        """Asset classes reachable by one strategy's declared wire shapes."""
        shape_ids = self.get("strategies", name, "execution_shape_ids")
        return frozenset(self._shapes_by_id[shape_id].asset_class
                         for shape_id in shape_ids)

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
