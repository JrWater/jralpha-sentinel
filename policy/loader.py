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


def load(path: Path = MANIFEST_PATH) -> Manifest:
    return Manifest(json.loads(path.read_text()))


if __name__ == "__main__":
    m = load()
    print(f"MANIFEST_IDENTITY={m.identity}")
    print(f"MANIFEST_SHA={m.sha}")
    print(f"DECLARED_SHAPES={[s.id for s in m.order_shapes]}")
    print(f"DECLARED_SYMBOLS={sorted(m.declared_symbols())}")
