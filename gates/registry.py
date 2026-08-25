#!/usr/bin/env python3
"""Gate metadata — one gate is one record.

A gate is a check that answers exactly one question: **if this is red, why
should the agent not open new exposure right now?** A check that cannot answer
that question does not get to be BLOCKING.

Why a registry instead of scattering the definitions
----------------------------------------------------
In the system this design is ported from, the full definition of a single gate
once lived in *five* separate hand-maintained tables: what to run, what counts
as passing, which operational dimension it belongs to, whether red blocks
trading, and what upstream task it depends on. Adding a gate meant editing two
to four places, and **missing any one of them raised no error** — it silently
produced one less layer of meaning.

That is not hypothetical. It failed twice:

  1. The severity lookup had a default of ATTENTION. A gate was added without
     registering its severity, which produced a gate that could never block
     trading — the exact opposite of why it was added. Fail-open on the one
     axis that matters.

  2. A loop-breaking exemption was written as `name != "entry_authority"`,
     while the gate had been called `base_entry_authority` since the day it
     landed. That line never matched anything, but it stood ready to silently
     disable a trading-safety gate the day somebody renamed the check.

So all six fields are **mandatory**. Omitting `severity` or `dimension` is no
longer "we find out next time it fails to block" — it is a TypeError at import.

This module is pure data. It imports nothing from the rest of the package and
touches no filesystem, so any process can read the gate list without dragging
in a broker session.
"""
from __future__ import annotations

from typing import Callable, NamedTuple

# Phases decide when a gate runs.
#   preflight  before any decision is made — cheap, always runs
#   pretrade   immediately before an order is submitted
#   postgate   after the permit snapshot is written; observes this run's result
PHASES = ("preflight", "pretrade", "postgate")

# The only criterion for BLOCKING: **it is red, so why should the agent not
# open new exposure?** If you cannot answer that, it does not get to zero out
# a trading session.
SEVERITIES = ("BLOCKING", "ATTENTION", "INFO")

# Dimensions are an enum, not a free string: a typo must not quietly grow a
# sixth dimension, it must fail at import.
DIMENSIONS = (
    "Process Health",     # did the machinery obey its operational contract
    "Data Readiness",     # is every input the decision needs actually present
    "Delivery Health",    # if something breaks, will anyone find out
    "Release Integrity",  # is the running code the code that was verified
    "Entry Authority",    # is this account, in this mode, allowed to trade
)


class GateResult(NamedTuple):
    """What a gate check returns. `detail` is shown to humans and logged."""
    ok: bool
    detail: str = ""


class Gate(NamedTuple):
    """The complete metadata for one gate.

    ``check``   callable(ctx) -> GateResult. Receives the evaluation context;
                never reads global state of its own, so tests and production
                take the same path.
    ``rationale`` why this gate exists. Required, because a gate nobody can
                justify is a gate nobody will dare delete later.
    """
    name: str
    check: Callable
    phase: str
    severity: str
    dimension: str
    rationale: str


def validate(gates) -> None:
    """Self-check of the registry itself.

    Two layers, deliberately separate: field **presence** is the NamedTuple
    signature's job (missing one is a TypeError at construction), field **value
    validity** is this function's job. Writing "BLOCKNIG" is exactly as
    dangerous as omitting severity — both yield a gate that stops nothing,
    and the typo additionally looks fine at a glance.
    """
    seen = set()
    for gate in gates:
        where = f"gate {gate.name!r}"
        if not gate.name:
            raise ValueError("a gate must have a name")
        if gate.name in seen:
            raise ValueError(f"{where}: duplicate gate name")
        seen.add(gate.name)
        if gate.phase not in PHASES:
            raise ValueError(f"{where}: phase {gate.phase!r} not in {PHASES}")
        if gate.severity not in SEVERITIES:
            raise ValueError(
                f"{where}: severity {gate.severity!r} not in {SEVERITIES}")
        if gate.dimension not in DIMENSIONS:
            raise ValueError(
                f"{where}: dimension {gate.dimension!r} not in {DIMENSIONS}")
        if not callable(gate.check):
            raise ValueError(f"{where}: check must be callable")
        if not gate.rationale.strip():
            raise ValueError(f"{where}: rationale is mandatory")


def severity_of(gates, name: str) -> str:
    """Severity for a gate name.

    Fail **closed**: an unregistered name is BLOCKING, not a default of
    ATTENTION. If we are asked about a gate we do not know, the safe answer is
    "stop", never "carry on". This is the exact defect described in the module
    docstring, inverted.
    """
    for gate in gates:
        if gate.name == name:
            return gate.severity
    return "BLOCKING"


def blockers(gates, results) -> tuple:
    """Names of BLOCKING gates that did not pass.

    There are no exemptions by name. Not one.

    A previous version of this logic carried a hardcoded `name != "..."`
    exemption meant to break a circular dependency. The cycle was already
    broken by construction elsewhere, so the exemption never matched anything —
    but a never-matching exemption is not harmless dead code. It is a loaded
    gun: the day somebody renames a check to match it, a BLOCKING gate stops
    blocking and no test goes red. If an exemption is genuinely needed, it goes
    through review as its own change; it does not hide inside this expression.
    """
    return tuple(sorted(
        name for name, result in results.items()
        if not result.ok and severity_of(gates, name) == "BLOCKING"
    ))
