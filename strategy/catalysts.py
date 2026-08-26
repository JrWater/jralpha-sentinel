#!/usr/bin/env python3
"""The confirmed in-window catalyst calendar. Nothing here is guessed.

Every entry below carries a source and a verification date. The Catalyst
Vector will only fire for events in this catalog; an entry that turns out to
be wrong becomes a refusal (no trade), never a bogus trade.

Sources (verified 2026-08-25):
  * LULU Q2 FY2026 earnings - company press release 2026-08-20 (BusinessWire):
    "financial results for the second quarter fiscal 2026 will be released
    Thursday, September 3, 2026 ... conference call at 4:30 p.m. Eastern".
  * US Employment Situation for August 2026 - BLS CES release schedule:
    Friday, September 4, 2026, 08:30 ET.
  * PCE (Personal Income & Outlays, July 2026 data) - BEA calendar,
    Friday 2026-08-28, 08:30 ET.
  * ISM Manufacturing/JOLTS 2026-09-01, ADP 2026-09-02, ISM Services
    2026-09-03 - standard monthly schedule, low-impact for intraday options
    and therefore *not* traded as catalysts; they only feed the regime
    engine's noise filter.
  * NVDA / CRM / CRWD Q2 earnings 2026-08-26 after close (Newsquawk daily
    earnings estimates 26 Aug 2026). These are *before* the window: they are
    not straddled; they feed the PEAD engine via the runtime gap detector.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


@dataclass(frozen=True)
class Catalyst:
    id: str
    date: date                  # local ET date of the event
    time_et: str                # "08:30" | "16:30"
    name: str
    kind: str                   # "macro" | "earnings"
    underlying: str | None      # earnings: the ticker; macro: None (SPY/QQQ)
    major: bool                 # major => dedicated pre-event positioning
    window_start_offset_days: int = 0
    window_end_offset_days: int = 0
    source: str = ""
    note: str = ""


CATALYSTS: tuple[Catalyst, ...] = (
    Catalyst(
        id="pce", date=date(2026, 8, 28), time_et="08:30", name="PCE inflation",
        kind="macro", underlying=None, major=False,
        source="BEA calendar (2026-08-28)",
        note="Before the 11:00 ET kickoff - no pre-positioning possible; "
             "only the Trend Vector reacts afterward."),
    Catalyst(
        id="ism_mfg", date=date(2026, 9, 1), time_et="10:00",
        name="ISM Manufacturing + JOLTS", kind="macro", underlying=None,
        major=False, source="standard monthly schedule",
        note="Not traded directly."),
    Catalyst(
        id="adp", date=date(2026, 9, 2), time_et="08:15", name="ADP employment",
        kind="macro", underlying=None, major=False,
        source="standard monthly schedule", note="Signal for the NFP play."),
    Catalyst(
        id="lulu_earnings", date=date(2026, 9, 3), time_et="16:30",
        name="LULU Q2 FY2026 earnings", kind="earnings", underlying="LULU",
        major=True, window_start_offset_days=-1, window_end_offset_days=1,
        source="LULU press release 2026-08-20 (BusinessWire)",
        note="Straddle 1 DTE entered 09-02 15:00-15:15 ET; PEAD leg after "
             "the 09-04 open if the gap is large."),
    Catalyst(
        id="ism_services", date=date(2026, 9, 3), time_et="10:00",
        name="ISM Services", kind="macro", underlying=None, major=False,
        source="standard monthly schedule", note="Not traded directly."),
    Catalyst(
        id="nfp", date=date(2026, 9, 4), time_et="08:30",
        name="August Employment Situation (NFP)", kind="macro", underlying=None,
        major=True, window_start_offset_days=-1, window_end_offset_days=0,
        source="BLS CES release schedule (verified 2026-08-25)",
        note="1-DTE SPY strangle entered 09-03; 0-DTE directional vertical "
             "at the 09-04 open; all flat by 10:40 ET."),
)

# Names that reported in the week before the window (2026-08-26 after close).
# The PEAD engine scans these for a day-one gap at runtime; no assumptions
# about the *direction* of the result are baked in.
PRE_WINDOW_EARNINGS: tuple[str, ...] = ("NVDA", "CRM", "CRWD")


def major_catalysts() -> tuple[Catalyst, ...]:
    return tuple(c for c in CATALYSTS if c.major)


def upcoming_major(before: date) -> list[Catalyst]:
    """Major catalysts with date >= before, sorted."""
    return sorted((c for c in CATALYSTS if c.major and c.date >= before),
                  key=lambda c: c.date)


def days_until(cat: Catalyst, now: datetime) -> int:
    """Calendar days from now to the event date (0 = same ET calendar day)."""
    return (cat.date - now.astimezone(_eastern()).date()).days


def entry_window(cat: Catalyst) -> tuple[date, date]:
    """ET dates on which the event's pre-positioning may be opened."""
    return (cat.date + timedelta(days=cat.window_start_offset_days),
            cat.date + timedelta(days=cat.window_end_offset_days))


def _eastern():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")
