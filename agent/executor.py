#!/usr/bin/env python3
"""The executor: the ONLY code that builds a wire order.

It validates the proposal against the manifest's declared order shapes again
(it built the order from that list, so an undeclared shape is structurally
impossible to submit), refuses any session that is not the declared paper
account, and manages exits: take-profit, stop-loss, time-stop, and the
final-day flatten.

The LLM never calls this module. The gates call it. Neither of them can be
prompted into a shape the manifest does not declare.
"""
from __future__ import annotations

from datetime import datetime, timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (OrderClass, OrderSide, PositionIntent,
                                  TimeInForce)
from alpaca.trading.requests import (LimitOrderRequest, OptionLegRequest)

from agent.ledger import append_decision
from strategy.proposal import Proposal

CONTRACT_RE = None  # parsing lives in strategy.data; keep this import-light


def _intent(side: str, closing: bool) -> PositionIntent:
    if closing:
        return (PositionIntent.SELL_TO_CLOSE if side == "sell"
                else PositionIntent.BUY_TO_CLOSE)
    return (PositionIntent.SELL_TO_OPEN if side == "sell"
            else PositionIntent.BUY_TO_OPEN)


class Executor:
    def __init__(self, client: TradingClient, manifest, *, verbose: bool = True):
        self.client = client
        self.manifest = manifest
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    # ── authority ────────────────────────────────────────────────────────────
    def _refuse_unless_authorized(self, *, now: datetime | None = None) -> None:
        """The one checkpoint every order — open OR close — must pass.

        `now` is injectable (defaults to the real clock) for the same reason
        gates/safety_gate.py::evaluate() takes one: a "before kickoff" branch
        that can only ever be exercised by a test running before 2026-08-28
        is not a test, it is a coin flip that expires.

        manage_exits() calls submit() directly and deliberately skips the
        pretrade gate loop (exits must survive a red entry gate). That made
        this check, not check_account_identity/check_competition_window, the
        only thing actually standing between a close order and the
        competition account before kickoff — and until now nothing stood
        here at all. There is no closing=True carve-out: a stray position
        that should not exist yet is a bug to surface, not one to quietly
        clean up by trading the pristine account again.

        Mirrors gates/checks.py::check_account_identity and
        check_competition_window so the two policies can't drift apart, but
        is stricter than either alone: it always requires the declared
        account, not just before kickoff.
        """
        if getattr(self.client, "_sandbox", None) is not True:
            raise RuntimeError(
                "refused: trading_client is not confirmed paper — this "
                "policy is PAPER-only by declaration")
        declared = self.manifest.get("environment", "competition_account_id",
                                     default=None)
        if not declared:
            raise RuntimeError(
                "refused: manifest declares no competition_account_id")
        account = self.client.get_account()
        actual = getattr(account, "account_number", None)
        if actual != declared:
            raise RuntimeError(
                f"refused: account {actual} != declared {declared} — order "
                f"authority is never carried forward to another account")
        starts = self.manifest.get("session", "competition_starts_utc",
                                   default=None)
        if not starts:
            raise RuntimeError(
                "refused: manifest declares no competition_starts_utc")
        start = datetime.fromisoformat(starts)
        now = now if now is not None else datetime.now(timezone.utc)
        if now < start:
            raise RuntimeError(
                f"refused: competition account {actual} is pristine until "
                f"{start.isoformat()} ({start - now} away) — trade the dev "
                f"account instead")

    # ── submission ───────────────────────────────────────────────────────────
    def submit(self, proposal: Proposal, closing: bool = False, *,
              now: datetime | None = None):
        """Submit a proposal as a DAY limit order. Returns the order or raises."""
        self._refuse_unless_authorized(now=now)
        shape = self.manifest.find_shape(
            order_class=proposal.order_class, type=proposal.type,
            time_in_force=proposal.time_in_force, legs=len(proposal.legs))
        if shape is None:
            raise RuntimeError(
                f"undeclared shape refused before submission: "
                f"{proposal.order_class}/{proposal.type}/"
                f"{proposal.time_in_force}/{len(proposal.legs)}leg — "
                f"this indicates a code bug, not a market decision")

        limit = proposal.limit_price
        if limit == 0:
            raise RuntimeError(f"zero limit price {limit}")
        if proposal.order_class == "simple" and limit < 0:
            raise RuntimeError(
                f"negative limit on a simple order: {limit} — only mleg "
                f"credit structures may carry a negative (credit) price")

        if len(proposal.legs) == 1:
            # alpaca-py's OrderRequest validator requires a top-level symbol
            # (and side/position_intent) for every order_class other than
            # mleg; a `legs=[OptionLegRequest(...)]` wrapper here is a
            # pydantic ValidationError, not a smaller/simpler mleg order.
            # That made every single-leg shape — the manifest declares one,
            # single_leg_limit_day, and the NFP gap play uses it — fail at
            # submission with an unhandled exception, never a clean refusal.
            leg = proposal.legs[0]
            order = self.client.submit_order(LimitOrderRequest(
                symbol=leg.symbol,
                qty=leg.quantity,
                side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
                position_intent=_intent(leg.side, closing),
                order_class=OrderClass.SIMPLE,
                time_in_force=TimeInForce.DAY,
                limit_price=limit,
            ))
        else:
            legs = [OptionLegRequest(
                symbol=leg.symbol,
                ratio_qty=1,
                side=OrderSide.BUY if leg.side == "buy" else OrderSide.SELL,
                position_intent=_intent(leg.side, closing),
            ) for leg in proposal.legs]
            order = self.client.submit_order(LimitOrderRequest(
                qty=proposal.legs[0].quantity,
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                limit_price=limit,
                legs=legs,
            ))
        self._log(f"  SUBMITTED {order.id} {proposal.structure} "
                  f"{proposal.underlying} @ {limit:.2f} -> {order.status}")
        append_decision({
            "kind": "order_submitted",
            "at_utc": datetime.now(timezone.utc).isoformat(),
            "order_id": str(order.id),
            "structure": proposal.structure,
            "underlying": proposal.underlying,
            "engine": proposal.engine,
            "limit_price": limit,
            "max_loss_dollars": proposal.max_loss_dollars,
            "thesis": proposal.thesis,
        })
        return order

    # ── exits ────────────────────────────────────────────────────────────────
    def close_position_by_limits(self, legs: list, net_limit: float,
                                 reason: str):
        """Close an open position with opposing limit legs (never market)."""
        proposal = Proposal(
            engine="exit", underlying="", direction="neutral",
            structure="close", legs=legs, limit_price=net_limit,
            max_loss_dollars=0.0, thesis=reason, reason=reason)
        return self.submit(proposal, closing=True)

    def retry_open_orders_cleanup(self) -> None:
        """Cancel stale open orders (they will not fill today)."""
        try:
            open_orders = self.client.get_orders(status="open")
        except Exception:                                   # noqa: BLE001
            return
        for o in open_orders:
            try:
                self.client.cancel_order_by_id(o.id)
                self._log(f"  CANCELLED stale {o.id} ({o.symbol})")
            except Exception:                               # noqa: BLE001
                pass
