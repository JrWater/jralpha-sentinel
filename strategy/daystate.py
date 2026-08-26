#!/usr/bin/env python3
"""Day-state ledger: the daily risk gates that were declared in the manifest
but, until v2.1, enforced nowhere.

Pure logic here (no I/O), so every gate is unit-testable. run_cycle owns the
JSON file through agent.ledger.atomic_write.

The three gates:
  * daily new-exposure cap  — max_loss of entries submitted today <= cap
  * daily kill switch       — day P&L <= -3% of day-start equity kills new
                              entries for the rest of the day
  * drawdown scaling        — a killed day halves sizing the next day
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass
class DayState:
    date: str                       # ISO date the state belongs to
    start_equity: float
    new_risk_dollars: float = 0.0
    killed: bool = False
    scale: float = 1.0              # 0.5 after a killed day
    fired_once: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "date": self.date,
            "start_equity": self.start_equity,
            "new_risk_dollars": self.new_risk_dollars,
            "killed": self.killed,
            "scale": self.scale,
            "fired_once": self.fired_once,
        }

    @staticmethod
    def from_dict(d: dict) -> "DayState":
        return DayState(
            date=str(d.get("date", "")),
            start_equity=float(d.get("start_equity", 0.0)),
            new_risk_dollars=float(d.get("new_risk_dollars", 0.0)),
            killed=bool(d.get("killed", False)),
            scale=float(d.get("scale", 1.0)),
            fired_once=list(d.get("fired_once", [])),
        )


def load_or_reset(raw: dict | None, *, today: str, equity_now: float,
                  scale_fraction: float = 0.5) -> DayState:
    """Roll the state onto today. A killed yesterday halves today's sizing.

    `scale_fraction` is a plain float, not a Manifest lookup, so this stays
    pure logic with no I/O (see module docstring) — the caller reads
    risk_caps.drawdown_scale_fraction from the manifest and passes the
    number in, rather than this module importing policy.loader itself. The
    default matches the manifest's declared value only by convention; the
    production call site in scripts/run_cycle.py must pass it explicitly so
    an edited manifest actually changes behavior instead of silently
    drifting from a hardcoded copy.
    """
    if raw and str(raw.get("date")) == today:
        return DayState.from_dict(raw)
    scale = 1.0
    if raw and bool(raw.get("killed", False)):
        scale = scale_fraction
    return DayState(date=today, start_equity=equity_now, scale=scale)


def record_risk(ds: DayState, dollars: float, cap: float) -> bool:
    """True when this trade's max loss fits inside today's remaining cap."""
    if ds.new_risk_dollars + dollars > cap:
        return False
    ds.new_risk_dollars += dollars
    return True


def check_kill(ds: DayState, equity_now: float, kill_fraction: float) -> bool:
    """Trip the kill switch. Once tripped it stays tripped for the day."""
    if ds.killed:
        return True
    if ds.start_equity > 0 and (equity_now - ds.start_equity) <= -kill_fraction * ds.start_equity:
        ds.killed = True
    return ds.killed


def fired(ds: DayState, key: str) -> bool:
    return key in ds.fired_once


def mark_fired(ds: DayState, key: str) -> None:
    if key not in ds.fired_once:
        ds.fired_once.append(key)


def fire_key(engine: str, underlying: str, date_iso: str) -> str:
    return f"{engine}:{underlying}:{date_iso}"
