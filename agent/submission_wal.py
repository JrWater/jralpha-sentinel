#!/usr/bin/env python3
"""Crash-safe journal for entry submissions.

A broker call has three outcomes, not two. It can be accepted, it can be
refused, and it can *not answer* — a timeout or a dropped connection says
nothing about whether Alpaca received the order. Treating the third case as
"nothing happened" is how a system releases risk it is still carrying and
then places the same trade again.

So the journal is written before the call, not after it, and an unanswered
call stays `DISPATCHING` until a reconciler proves what happened by looking
the order up under a key chosen in advance. Nothing releases a reservation on
an exception.

Three identities, deliberately separate:

  intent_fingerprint     what the order is
  logical_submission_id  which authorised attempt this is
  client_order_id        the broker's name for that attempt

Collapsing the first two would make two legitimate identical entries — a
repeated gap continuation, say — collide at the broker, and the second would
be refused as a duplicate of the first.

Risk is a projection over this journal, never an accumulator. Every budget
bug this file exists to prevent was a `+=` that ran a second time, or failed
to run at all; a deduplicated sum over committed records cannot double-count.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

SCHEMA_VERSION = 1

HELD = "HELD"
DISPATCHING = "DISPATCHING"
COMMITTED = "COMMITTED"
RELEASED = "RELEASED"
RECONCILE_OBSERVED = "RECONCILE_OBSERVED"

TERMINAL = (COMMITTED, RELEASED)

# state -> the states it may legally become
TRANSITIONS = {
    None: (HELD,),
    HELD: (DISPATCHING, RELEASED),
    DISPATCHING: (COMMITTED, RELEASED),
    COMMITTED: (),
    RELEASED: (),
}


class InvalidTransition(RuntimeError):
    """A journal transition the state machine does not allow.

    This is a policy error, not a market outcome: it means the code or the
    journal is wrong. It must stop the cycle rather than be absorbed as an
    ordinary refusal.
    """


class BrokerProtocolError(RuntimeError):
    """The broker returned a response that cannot identify an order."""


class Reservation(NamedTuple):
    """What a single authorised entry attempt holds while it is in flight."""
    logical_submission_id: str
    client_order_id: str
    account_id: str
    trading_date_et: date
    cycle_id: str
    intent_fingerprint: str
    manifest_sha: str
    git_head: str
    max_loss_cents: int
    fire_keys: tuple[str, ...] = ()
    gap_counters: tuple[tuple[str, int], ...] = ()


def make_client_order_id(*, manifest_sha: str, intent_fingerprint: str,
                         logical_submission_id: str) -> str:
    """Stable broker-side name for one authorised attempt.

    Deterministic so a retry of the *same* attempt is idempotent at the
    broker, and distinct per attempt so a second legitimate entry with
    identical content is not mistaken for that retry.
    """
    digest = hashlib.sha256(
        "\x1f".join((manifest_sha, intent_fingerprint,
                     logical_submission_id)).encode()
    ).hexdigest()
    return "sentinel-%s" % digest[:32]


# ── replay ───────────────────────────────────────────────────────────────────

@dataclass
class SubmissionState:
    logical_submission_id: str
    state: str
    client_order_id: str = ""
    reservation: Reservation | None = None
    broker_order_id: str | None = None
    broker_status: str | None = None
    reason_code: str | None = None


class Risk(NamedTuple):
    committed_cents: int
    held_cents: int
    fire_keys: tuple[str, ...]
    gap_units: dict[str, int]


@dataclass
class JournalView:
    by_submission: dict[str, SubmissionState] = field(default_factory=dict)
    integrity_ok: bool = True
    problems: tuple[str, ...] = ()
    fatal_problems: tuple[str, ...] = ()
    recoverable_torn_dispatches: tuple[str, ...] = ()

    @property
    def unresolved_dispatches(self) -> tuple[str, ...]:
        return tuple(sid for sid, rec in self.by_submission.items()
                     if rec.state == DISPATCHING)

    @property
    def entries_allowed(self) -> bool:
        """Fail closed: a damaged journal is as blocking as an open dispatch.

        Skipping a line we cannot read would mean trading against a risk
        picture we know is incomplete — the "half-loaded chain" mistake, one
        layer down.
        """
        return self.integrity_ok and not self.unresolved_dispatches

    def risk_for(self, trading_date: date) -> Risk:
        committed = held = 0
        fire: list[str] = []
        gaps: dict[str, int] = {}
        for rec in self.by_submission.values():
            res = rec.reservation
            if res is None or res.trading_date_et != trading_date:
                continue
            if rec.state == RELEASED:
                continue
            if rec.state == COMMITTED:
                committed += res.max_loss_cents
            else:                                    # HELD or DISPATCHING
                held += res.max_loss_cents
            for key in res.fire_keys:
                if key not in fire:
                    fire.append(key)
            for key, units in res.gap_counters:
                gaps[key] = gaps.get(key, 0) + units
        return Risk(committed, held, tuple(fire), gaps)


# A torn final line still has to give up the two keys a reconciler needs, so
# they are written first. See SubmissionJournal._line.
_KEY_RE = re.compile(
    r'"logical_submission_id"\s*:\s*"([^"]+)".*?"client_order_id"\s*:\s*"([^"]+)"',
    re.S)


class SubmissionJournal:
    """Append-only journal of entry submissions, one JSON object per line."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    # ── writing ──────────────────────────────────────────────────────────────

    def _line(self, event: str, logical_submission_id: str,
              client_order_id: str, **payload: Any) -> str:
        """Lookup keys first, on purpose.

        A crash during append truncates the tail. If `client_order_id` sat at
        the end of the line, the one record that proves an order may exist
        would be the one we could not read, and the reconciler would have
        nothing to look up.
        """
        record: dict[str, Any] = {
            "event": event,
            "logical_submission_id": logical_submission_id,
            "client_order_id": client_order_id,
            "schema_version": SCHEMA_VERSION,
            "at_utc": datetime.now(timezone.utc).isoformat(),
        }
        record.update(payload)
        return json.dumps(record, separators=(",", ":"), sort_keys=False)

    def _append(self, line: str) -> None:
        created = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_separator = False
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open("rb") as existing:
                existing.seek(-1, os.SEEK_END)
                needs_separator = existing.read(1) != b"\n"
        with self.path.open("a", encoding="utf-8") as handle:
            # A process may have died halfway through the preceding append.
            # Keep that torn evidence on its own line so the resolving record
            # remains independently parseable.
            if needs_separator:
                handle.write("\n")
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            # fsync on the file does not make its *directory entry* durable;
            # a crash could otherwise lose the whole journal on the day it
            # was created.
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _require(self, logical_submission_id: str, target: str
                 ) -> SubmissionState | None:
        view = self.replay()
        if not view.integrity_ok:
            rec = view.by_submission.get(logical_submission_id)
            resolving_one_torn_dispatch = (
                target in TERMINAL
                and not view.fatal_problems
                and view.recoverable_torn_dispatches == (
                    logical_submission_id,)
                and rec is not None
                and rec.reservation is not None
            )
            if not resolving_one_torn_dispatch:
                raise InvalidTransition(
                    "submission journal is not trustworthy: "
                    + "; ".join(view.problems))
        rec = view.by_submission.get(logical_submission_id)
        current = rec.state if rec else None
        if target in TRANSITIONS.get(current, ()):
            return rec
        raise InvalidTransition(
            f"{logical_submission_id}: {current or '<absent>'} -> {target} "
            f"is not a legal transition")

    def hold(self, reservation: Reservation) -> None:
        self._require(reservation.logical_submission_id, HELD)
        self._append(self._line(
            HELD, reservation.logical_submission_id,
            reservation.client_order_id,
            account_id=reservation.account_id,
            trading_date_et=reservation.trading_date_et.isoformat(),
            cycle_id=reservation.cycle_id,
            intent_fingerprint=reservation.intent_fingerprint,
            manifest_sha=reservation.manifest_sha,
            git_head=reservation.git_head,
            reservation={
                "max_loss_cents": reservation.max_loss_cents,
                "fire_keys": list(reservation.fire_keys),
                "gap_counters": [{"key": k, "units": u}
                                 for k, u in reservation.gap_counters],
            }))

    def mark_dispatching(self, logical_submission_id: str) -> None:
        rec = self._require(logical_submission_id, DISPATCHING)
        self._append(self._line(DISPATCHING, logical_submission_id,
                                rec.reservation.client_order_id))

    def commit(self, logical_submission_id: str, *, broker_order_id: str,
               broker_status: str) -> None:
        rec = self.replay().by_submission.get(logical_submission_id)
        if rec is not None and rec.state == COMMITTED:
            if (rec.broker_order_id == broker_order_id
                    and rec.broker_status == broker_status):
                return                       # replay-safe duplicate
            raise InvalidTransition(
                f"{logical_submission_id}: already COMMITTED as "
                f"{rec.broker_order_id}/{rec.broker_status}")
        rec = self._require(logical_submission_id, COMMITTED)
        self._append(self._line(COMMITTED, logical_submission_id,
                                rec.reservation.client_order_id,
                                broker_order_id=broker_order_id,
                                broker_status=broker_status))

    def release(self, logical_submission_id: str, *, reason_code: str) -> None:
        rec = self.replay().by_submission.get(logical_submission_id)
        if rec is not None and rec.state == RELEASED:
            if rec.reason_code == reason_code:
                return
            raise InvalidTransition(
                f"{logical_submission_id}: already RELEASED as "
                f"{rec.reason_code}")
        rec = self._require(logical_submission_id, RELEASED)
        self._append(self._line(RELEASED, logical_submission_id,
                                rec.reservation.client_order_id,
                                reason_code=reason_code))

    def observe(self, logical_submission_id: str, *, result: str,
                detail: str = "") -> None:
        """Record what reconciliation saw. Evidence only — never a transition.

        The resolving transition is written separately and afterwards, so the
        journal always shows why a dispatch was resolved before it shows that
        it was.
        """
        rec = self.replay().by_submission.get(logical_submission_id)
        cid = rec.client_order_id if rec else ""
        self._append(self._line(RECONCILE_OBSERVED, logical_submission_id, cid,
                                result=result, detail=detail))

    # ── reading ──────────────────────────────────────────────────────────────

    def replay(self) -> JournalView:
        view = JournalView()
        if not self.path.exists():
            return view
        fatal_problems: list[str] = []
        torn_problems: dict[str, str] = {}
        for number, raw in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                match = _KEY_RE.search(raw)
                if match:
                    # A torn tail could have been HELD or DISPATCHING, and the
                    # two have opposite safety meanings. Read it as the one
                    # that keeps the reservation held.
                    sid, cid = match.group(1), match.group(2)
                    rec = view.by_submission.get(sid)
                    if rec is None:
                        rec = SubmissionState(sid, DISPATCHING,
                                              client_order_id=cid)
                        view.by_submission[sid] = rec
                    elif rec.state not in TERMINAL:
                        rec.state = DISPATCHING
                        if not rec.client_order_id:
                            rec.client_order_id = cid
                    torn_problems[sid] = (
                        f"line {number}: torn record for {sid}")
                else:
                    fatal_problems.append(f"line {number}: unreadable")
                continue
            self._apply(view, record, number, fatal_problems)

        active_torn = {
            sid: problem for sid, problem in torn_problems.items()
            if (sid not in view.by_submission
                or view.by_submission[sid].state not in TERMINAL)
        }
        recoverable = tuple(
            sid for sid in active_torn
            if view.by_submission.get(sid) is not None
            and view.by_submission[sid].reservation is not None)
        view.fatal_problems = tuple(fatal_problems)
        view.recoverable_torn_dispatches = recoverable
        view.problems = tuple(fatal_problems) + tuple(active_torn.values())
        view.integrity_ok = not view.problems
        return view

    @staticmethod
    def _apply(view: JournalView, record: dict, number: int,
               problems: list[str]) -> None:
        event = record.get("event")
        sid = record.get("logical_submission_id")
        cid = record.get("client_order_id")
        known_events = {
            HELD, DISPATCHING, COMMITTED, RELEASED, RECONCILE_OBSERVED}
        if record.get("schema_version") != SCHEMA_VERSION:
            view.integrity_ok = False
            problems.append(f"line {number}: unsupported schema")
            return
        if not sid or not cid or event is None:
            view.integrity_ok = False
            problems.append(f"line {number}: missing keys")
            return
        if event not in known_events:
            view.integrity_ok = False
            problems.append(f"line {number}: unknown event {event!r}")
            return
        rec = view.by_submission.get(sid)
        if event == RECONCILE_OBSERVED:
            if rec is None or rec.state != DISPATCHING:
                view.integrity_ok = False
                problems.append(
                    f"line {number}: observation for non-dispatching {sid}")
            elif rec.client_order_id != cid:
                view.integrity_ok = False
                problems.append(f"line {number}: client order id drift for {sid}")
            return
        if event == HELD:
            if rec is not None:
                view.integrity_ok = False
                problems.append(f"line {number}: duplicate HELD for {sid}")
                return
            try:
                payload = record["reservation"]
                reservation = Reservation(
                    logical_submission_id=sid,
                    client_order_id=cid,
                    account_id=str(record["account_id"]),
                    trading_date_et=date.fromisoformat(
                        str(record["trading_date_et"])),
                    cycle_id=str(record["cycle_id"]),
                    intent_fingerprint=str(record["intent_fingerprint"]),
                    manifest_sha=str(record["manifest_sha"]),
                    git_head=str(record["git_head"]),
                    max_loss_cents=int(payload["max_loss_cents"]),
                    fire_keys=tuple(payload.get("fire_keys", ())),
                    gap_counters=tuple(
                        (str(g["key"]), int(g["units"]))
                        for g in payload.get("gap_counters", ())),
                )
            except (KeyError, TypeError, ValueError) as exc:
                view.integrity_ok = False
                problems.append(f"line {number}: invalid HELD: {exc}")
                return
            view.by_submission[sid] = SubmissionState(
                sid, HELD, client_order_id=cid, reservation=reservation)
            return
        if rec is None:
            view.integrity_ok = False
            problems.append(f"line {number}: {event} for {sid} with no HELD")
            return
        if rec.client_order_id != cid:
            view.integrity_ok = False
            problems.append(f"line {number}: client order id drift for {sid}")
            return
        if event not in TRANSITIONS.get(rec.state, ()):
            view.integrity_ok = False
            problems.append(
                f"line {number}: illegal {rec.state} -> {event} for {sid}")
            return
        rec.state = event
        if event == COMMITTED:
            rec.broker_order_id = record.get("broker_order_id")
            rec.broker_status = record.get("broker_status")
        elif event == RELEASED:
            rec.reason_code = record.get("reason_code")


# ── the two operations that touch the broker ─────────────────────────────────

def dispatch_entry(journal: SubmissionJournal, reservation: Reservation,
                   submit: Callable[[str], Any]) -> Any:
    """Hold, make the dispatch durable, then call the broker.

    If `submit` raises, the journal is left at DISPATCHING and the exception
    propagates. That is the whole point: the reservation stays held because a
    lost response is not evidence that the order was not placed.
    """
    journal.hold(reservation)
    journal.mark_dispatching(reservation.logical_submission_id)
    order = submit(reservation.client_order_id)
    order_id = _order_value(order, "id")
    if not order_id:
        raise BrokerProtocolError(
            "broker response carried no order id; dispatch remains unresolved")
    journal.commit(reservation.logical_submission_id,
                   broker_order_id=str(order_id),
                   broker_status=str(_order_value(order, "status") or ""))
    return order


def _order_value(order: Any, key: str) -> Any:
    if isinstance(order, dict):
        return order.get(key)
    return getattr(order, key, None)


def reconcile_unresolved(journal: SubmissionJournal, broker: Any) -> JournalView:
    """Resolve open dispatches by looking each one up under its own key.

    An unreachable broker resolves nothing: the dispatch stays open and entry
    stays blocked. Guessing from symbol, quantity and timestamp is exactly the
    ambiguity the client order id exists to remove.
    """
    for sid in journal.replay().unresolved_dispatches:
        rec = journal.replay().by_submission[sid]
        cid = rec.client_order_id
        if not cid:
            continue
        try:
            order = broker.get_order_by_client_id(cid)
        except Exception as exc:                            # noqa: BLE001
            journal.observe(sid, result="UNAVAILABLE", detail=str(exc))
            continue
        if order is None:
            journal.observe(sid, result="ABSENT",
                            detail="one lookup returned no order; still "
                                   "unresolved pending definitive evidence")
            continue
        order_id = _order_value(order, "id")
        if not order_id:
            journal.observe(sid, result="MALFORMED",
                            detail="broker response carried no order id")
            continue
        journal.observe(sid, result="FOUND",
                        detail=f"broker order {order_id}")
        try:
            journal.commit(
                sid, broker_order_id=str(order_id),
                broker_status=str(_order_value(order, "status") or ""))
        except InvalidTransition:
            # A torn record with no preceding HELD tells us an order exists
            # but not how much risk it reserved. Preserve the evidence and
            # remain blocking; startup reconciliation must not prevent exits.
            continue
    return journal.replay()


def refresh_committed_orders(journal: SubmissionJournal,
                             broker: Any) -> dict[str, dict[str, Any]]:
    """Read current broker facts for already identified submissions.

    This is presentation reconciliation, not a state transition. A failed
    read preserves the last public observation and never changes entry
    authority; unresolved dispatches are handled by `reconcile_unresolved`.
    """
    updates: dict[str, dict[str, Any]] = {}
    for rec in journal.replay().by_submission.values():
        if rec.state != COMMITTED or not rec.client_order_id:
            continue
        try:
            order = broker.get_order_by_client_id(rec.client_order_id)
        except Exception:                                   # noqa: BLE001
            continue
        if order is None:
            continue
        order_id = _order_value(order, "id")
        if not order_id or (rec.broker_order_id
                            and str(order_id) != rec.broker_order_id):
            continue
        status = str(_order_value(order, "status") or "")
        if "." in status:
            status = status.rsplit(".", 1)[-1]
        filled_qty = _order_value(order, "filled_qty")
        try:
            filled_qty = float(filled_qty or 0)
        except (TypeError, ValueError):
            filled_qty = 0.0
        filled_avg = _order_value(order, "filled_avg_price")
        try:
            filled_avg = float(filled_avg) if filled_avg is not None else None
        except (TypeError, ValueError):
            filled_avg = None
        updates[rec.client_order_id] = {
            "broker_order_id": str(order_id),
            "broker_status": status.lower(),
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg,
        }
    return updates
