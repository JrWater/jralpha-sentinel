#!/usr/bin/env python3
"""Prove the auto-trade path end to end, without sending an order.

The question this answers is the one no other check in this repo can:
*will tomorrow's cron actually place a trade?* Nothing else reaches that far.

  - `run_cycle.py --dry-run` stops before the submit loop entirely.
  - `validate_delay_chain.py --live` does fill a real order, but it bypasses
    agent/executor.py on purpose, so it exercises pricing, not the agent.
  - The unit tests drive the Executor with a FakeClient and a hand-built
    Proposal, so they never see a real chain, real strikes, or real gates.
  - And the live path cannot be rehearsed on either account: before kickoff
    `competition_window` blocks the competition account, and `account_identity`
    blocks every other one.

So this runs the REAL cycle — real clock, real quotes, real regime, real
engines, real proposer, all seventeen gates, the real Executor — with exactly
two substitutions:

  1. `session.competition_starts_utc` is backdated one hour, so
     `competition_window` opens naturally. Nothing else is distorted: the
     clock, the bars and the chain stay exactly as they are, and the entry
     window (10:00-15:30 ET) is genuinely open while this runs. Backdating one
     value beats moving the clock, which would age today's bars past
     `underlying_data`'s freshness limit and refuse the permit for the wrong
     reason.
  2. `TradingClient.submit_order` is replaced by a recorder that captures the
     wire request and returns a stand-in order id.

The manifest sha therefore differs from production for this run — expected,
and the reason `manifest_identity` reports a different hash below.

Every disk write on the path is stubbed — including `agent.snapshot.write`,
which does NOT go through `atomic_write` and so escaped the first version of
this harness, briefly publishing the rehearsal's manifest hash to the public
dashboard. The script hashes state/ and docs/snapshot.json before and after
and exits non-zero if anything moved, so that failure cannot pass silently. Nothing is sent
to the broker; reads are ordinary market data.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WATCHED = ["state/day_state.json", "state/entry_permit.json",
           "state/ledger.json", "state/decisions.jsonl", "docs/snapshot.json",
           "state/positions_meta.json"]


def fingerprint() -> dict:
    out = {}
    for rel in WATCHED:
        p = ROOT / rel
        out[rel] = (hashlib.sha256(p.read_bytes()).hexdigest()[:16]
                    if p.exists() else "<absent>")
    return out


class RecordingClient:
    """Passes reads through to the real client; captures writes."""

    def __init__(self, real):
        self._real = real
        self.orders: list = []
        self.cancellations: list = []

    def __getattr__(self, name):
        return getattr(self._real, name)

    def submit_order(self, request):
        self.orders.append(request)
        return mock.Mock(id=f"REHEARSAL-{len(self.orders)}")

    def cancel_order_by_id(self, order_id):
        """Capture cleanup intent; a rehearsal must never mutate the broker."""
        self.cancellations.append(order_id)


def describe(request) -> str:
    lines = [f"  order_class = {getattr(request, 'order_class', None)}",
             f"  type        = {type(request).__name__}",
             f"  symbol      = {getattr(request, 'symbol', None)}",
             f"  qty         = {getattr(request, 'qty', None)}",
             f"  side        = {getattr(request, 'side', None)}",
             f"  limit_price = {getattr(request, 'limit_price', None)}",
             f"  tif         = {getattr(request, 'time_in_force', None)}"]
    for leg in (getattr(request, "legs", None) or []):
        lines.append(f"    leg  {leg.symbol}  {leg.side}  "
                     f"ratio={leg.ratio_qty}  intent={leg.position_intent}")
    return "\n".join(lines)


def main() -> int:
    from policy import loader
    from scripts import run_cycle as rc

    raw = json.loads((ROOT / "policy" / "manifest.json").read_text())
    real_start = raw["session"]["competition_starts_utc"]
    opened = (datetime.now(timezone.utc) - timedelta(hours=1))
    raw["session"]["competition_starts_utc"] = opened.isoformat()
    rehearsal_manifest = loader.Manifest(raw)

    print(f"dress rehearsal — real clock ({datetime.now(timezone.utc).isoformat()})")
    print(f"  competition_starts_utc {real_start} -> {opened.isoformat()}")
    print(f"  everything else is live\n")
    before = fingerprint()

    recorders: list[RecordingClient] = []
    real_executor = rc.Executor

    def executor_factory(client, mf, **kw):
        rec = RecordingClient(client)
        recorders.append(rec)
        return real_executor(rec, mf, **kw)

    with mock.patch.object(rc, "load_manifest", lambda *a, **k: rehearsal_manifest), \
         mock.patch.object(rc, "Executor", executor_factory), \
         mock.patch.object(rc, "write_permit"), \
         mock.patch.object(rc, "atomic_write"), \
         mock.patch.object(rc, "append_decision"), \
         mock.patch.object(rc, "mirror_from_broker"), \
         mock.patch("agent.executor.append_decision"), \
         mock.patch("agent.snapshot.write"):
        sys.argv = ["run_cycle.py"] + sys.argv[1:]
        code = rc.main()

    sent = [o for r in recorders for o in r.orders]
    canceled = [o for r in recorders for o in r.cancellations]
    print(f"\n{'='*66}")
    print(f"cycle exit code: {code}")
    print(f"orders the agent would have placed: {len(sent)}")
    for i, req in enumerate(sent, 1):
        print(f"\n[{i}] wire request the Executor built:")
        print(describe(req))
    print(f"broker cancellations captured, not sent: {len(canceled)}")

    after = fingerprint()
    drifted = [k for k in before if before[k] != after[k]]
    print(f"\nstate untouched: {'YES' if not drifted else 'NO -> ' + str(drifted)}")
    return 0 if not drifted else 1


if __name__ == "__main__":
    raise SystemExit(main())
