"""Structure-owned option exits.

Alpaca reports one net position per contract symbol, but Sentinel creates
structures.  A contract may therefore belong to more than one structure at a
time.  This module keeps those identities separate: a structure's recorded
quantity and its own close order decide its lifecycle; a net broker symbol
never does.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from agent.ledger import StructureLedger, append_decision
from strategy.data import parse_contract
from strategy.exits import GroupView, build_close_proposal, decide_exit, pnl_of
from strategy.proposal import OptionLeg, Proposal


def _order_status(order: Any) -> str:
    return str(getattr(order, "status", "") or "").rsplit(".", 1)[-1].upper()


def _quantity(value: Any) -> int | None:
    try:
        quantity = int(float(value))
    except (TypeError, ValueError):
        return None
    return quantity if quantity >= 0 else None


def _group_quantity(group: dict) -> int | None:
    """Return the declared structure quantity, or None for an invalid ratio."""
    quantities = {_quantity(info.get("qty")) for info in group.get("legs", {}).values()}
    if None in quantities or len(quantities) != 1:
        return None
    quantity = quantities.pop()
    return quantity if quantity > 0 else None


def _contract(state: Any, symbol: str):
    parsed = parse_contract(symbol)
    if not parsed:
        return None
    for contract in state.chains.get(parsed[0], []):
        if contract.symbol == symbol:
            return contract
    return None


def _underlying(symbol: str) -> str:
    parsed = parse_contract(symbol)
    return parsed[0] if parsed else symbol[:4]


class PositionLifecycle:
    """Deep module for structure exits and close-order reconciliation.

    ``manage`` is the interface: it owns close-order status interpretation,
    group quantities, market-touch pricing, broker submissions, durable group
    state, and defensive orphan handling.  Callers receive only the count of
    newly submitted close orders.
    """

    def __init__(self, *, record_decision: Callable[[dict], None] = append_decision):
        self._record = record_decision

    def manage(self, state: Any, manifest: Any, executor: Any, *,
               structures: StructureLedger) -> int:
        from zoneinfo import ZoneInfo

        now_et = state.now_utc.astimezone(ZoneInfo("America/New_York"))
        final_day = manifest.final_day_rules()
        final_date = final_day.trading_date.isoformat()
        flatten_at = final_day.flatten_at
        meta = structures.load()
        groups = meta.get("groups", {})
        broker = {position.symbol: position for position in state.positions}
        submitted = 0
        meta_changed = False

        # A group owns a symbol until its own close order proves otherwise.
        # This also prevents a same-cycle accepted structure close becoming an
        # orphan close against the unchanged broker snapshot.
        managed_symbols = {
            symbol
            for group in groups.values()
            if not group.get("closed")
            for symbol in group.get("legs", {})
        }

        # First reconcile every previously submitted close.  Ownership is
        # computed only after those outcomes are known, so a filled close
        # releases its recorded legs while a pending one keeps reserving them.
        for group_id, group in groups.items():
            if group.get("closed"):
                continue
            if group.get("close_pending"):
                resolution = self._reconcile_close(group_id, group, executor)
                if resolution == "closed":
                    meta_changed = True
                    continue
                if resolution in {"pending", "quarantined"}:
                    meta_changed = meta_changed or resolution == "quarantined"
                    continue
                # A zero-fill terminal rejection/cancellation is the only
                # state allowed to return to ordinary exit evaluation.
                meta_changed = True

        meta_changed = self._reconcile_expired_absent(
            groups, broker, getattr(state, "non_option_positions", None),
            now_et.date(), state.now_utc) or meta_changed

        # Net broker positions do not identify lots.  Before deciding any new
        # close, prove that all still-live group claims for each symbol fit in
        # that symbol's one-sided net position.  A claim conflict is not a
        # reason to choose an arbitrary group; every claimant is quarantined
        # and the Entry Authority gate becomes red.
        for group_id in self._ownership_conflicts(groups, broker):
            group = groups[group_id]
            if not group.get("reconciliation_required"):
                self._quarantine(group, "shared_symbol_ownership_unresolved")
                meta_changed = True

        for group_id, group in groups.items():
            if group.get("closed") or group.get("close_pending") or \
                    group.get("reconciliation_required"):
                continue

            legs = self._owned_legs(group, broker)
            if legs is None:
                self._quarantine(group, "group_position_unresolved")
                meta_changed = True
                continue

            prices, touch = self._prices(state, legs)
            if not prices:
                continue
            quantity = _group_quantity(group)
            if quantity is None:
                self._quarantine(group, "group_quantity_unrepresentable")
                meta_changed = True
                continue

            net = sum(prices.values())
            view = GroupView(
                group_id=group_id, engine=group.get("engine", ""),
                underlying=group.get("underlying", ""),
                expiry=group.get("expiry", ""), kind=group.get("kind", "debit"),
                entry_net=float(group.get("entry_net", 0.0)),
                ref_amount=float(group.get("ref_amount", 0.0) or 0.0),
                take_profit_fraction=float(group.get("take_profit_fraction", 0.0)),
                stop_loss_fraction=float(group.get("stop_loss_fraction", 0.0)),
                event_exit_date=group.get("event_exit_date", ""),
                event_exit_time=group.get("event_exit_time", ""),
                legs=legs,
            )
            reason = decide_exit(
                view, pnl_of(view.entry_net, net), now_et=now_et,
                final_date=final_date, flatten_at=flatten_at)
            if not reason:
                continue
            if group.get("take_profit_fraction", 0.0) <= 0 and \
                    not any(word in reason for word in ("event", "flatten", "time-stop")):
                continue

            close = build_close_proposal(view, touch)
            if not close.legs:
                continue
            try:
                order = executor.submit(close, closing=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  EXIT FAILED {group_id}: {exc}")
                continue
            self._record({"kind": "structure_close_submitted", "group": group_id,
                          "reason": reason,
                          "pnl": round(pnl_of(view.entry_net, net) * quantity, 2),
                          "order_id": str(order.id)})
            group["close_pending"] = True
            group["close_order_id"] = str(order.id)
            group["close_reason"] = reason
            meta_changed = True
            submitted += 1
            print(f"  EXIT {group_id} {reason} net {close.limit_price:.2f}")

        submitted += self._close_orphans(state, executor, managed_symbols)
        if meta_changed:
            structures.save(meta)
        return submitted

    def _reconcile_close(self, group_id: str, group: dict, executor: Any) -> str:
        order_id = group.get("close_order_id")
        if not order_id:
            self._quarantine(group, "close_pending_without_order_id")
            return "quarantined"
        try:
            order = executor.get_order_by_id(order_id)
        except Exception:  # noqa: BLE001
            self._quarantine(group, "close_order_status_unavailable")
            return "quarantined"

        expected = _group_quantity(group)
        filled = _quantity(getattr(order, "filled_qty", None))
        status = _order_status(order)
        if expected is None or filled is None:
            self._quarantine(group, "close_order_quantity_unreadable")
            return "quarantined"
        if status == "FILLED" and filled == expected:
            group["closed"] = True
            group["close_pending"] = False
            group.pop("reconciliation_required", None)
            group.pop("reconciliation_detail", None)
            self._record({"kind": "structure_close_confirmed", "group": group_id,
                          "order_id": order_id, "filled_qty": filled})
            return "closed"
        if status == "FILLED" or status in {"PARTIALLY_FILLED", "UNKNOWN", ""}:
            self._quarantine(group, f"close_{status.lower() or 'unknown'}_{filled}_of_{expected}")
            return "quarantined"
        if status in {"CANCELED", "REJECTED", "EXPIRED"}:
            if filled == 0:
                group["close_pending"] = False
                group["last_close_order_id"] = order_id
                group.pop("close_order_id", None)
                return "retry"
            self._quarantine(group, f"close_{status.lower()}_{filled}_of_{expected}")
            return "quarantined"
        # NEW, ACCEPTED, PENDING_NEW, HELD, and broker statuses introduced
        # later remain owned by this close order; duplicate close submission is
        # never a safe fallback.
        return "pending"

    @staticmethod
    def _quarantine(group: dict, detail: str) -> None:
        group["reconciliation_required"] = True
        group["reconciliation_detail"] = detail

    def _reconcile_expired_absent(self, groups: dict, broker: dict,
                                  non_option_positions: list | None,
                                  today: date, now_utc) -> bool:
        """Close expired structures only when their resulting exposure is zero.

        Broker option legs disappear after expiry. That is not sufficient proof
        of closure because exercise or assignment can leave stock behind.
        Those groups remain quarantined until the underlying is independently
        reconciled or flattened.
        """
        non_option_symbols = set()
        non_option_state_known = non_option_positions is not None
        for position in non_option_positions or []:
            symbol = getattr(position, "symbol", None)
            if not isinstance(symbol, str) or not symbol.strip():
                non_option_state_known = False
                continue
            non_option_symbols.add(symbol.upper())
        changed = False
        for group_id, group in groups.items():
            if group.get("closed") or group.get("close_pending"):
                continue
            try:
                expiry = date.fromisoformat(str(group.get("expiry", "")))
            except ValueError:
                continue
            legs = set(group.get("legs", {}))
            if not legs or expiry >= today or legs.intersection(broker):
                continue
            parsed_legs = [parse_contract(symbol) for symbol in legs]
            leg_underlyings = {parsed[0].upper() for parsed in parsed_legs if parsed}
            declared_underlying = str(group.get("underlying", "")).upper()
            if (not all(parsed_legs) or len(leg_underlyings) != 1 or
                    any(parsed[1] != expiry for parsed in parsed_legs)):
                if group.get("reconciliation_detail") != \
                        "expired_structure_leg_identity_unresolved":
                    self._quarantine(group, "expired_structure_leg_identity_unresolved")
                    changed = True
                continue
            if (not declared_underlying or
                    declared_underlying not in leg_underlyings):
                if group.get("reconciliation_detail") != \
                        "expired_structure_underlying_unresolved":
                    self._quarantine(group, "expired_structure_underlying_unresolved")
                    changed = True
                continue
            underlying = next(iter(leg_underlyings))
            if not non_option_state_known:
                if group.get("reconciliation_detail") != \
                        "non_option_position_state_unavailable":
                    self._quarantine(group, "non_option_position_state_unavailable")
                    changed = True
                continue
            if underlying in non_option_symbols:
                if group.get("reconciliation_detail") != \
                        "underlying_exposure_after_expiry":
                    self._quarantine(group, "underlying_exposure_after_expiry")
                    changed = True
                continue
            group["closed"] = True
            group["terminal_outcome"] = "ENTRY_EXPIRED"
            group["terminal_reconciliation"] = {
                "reconciled_at_utc": now_utc.isoformat(),
                "declared_option_legs": sorted(legs),
                "broker_option_legs_observed": [],
                "broker_underlying_positions_observed": [],
            }
            group.pop("reconciliation_required", None)
            group.pop("reconciliation_detail", None)
            self._record({"kind": "structure_expired_absent", "group": group_id,
                          "expiry": expiry.isoformat(), "outcome": "ENTRY_EXPIRED"})
            changed = True
        return changed

    @staticmethod
    def _ownership_conflicts(groups: dict, broker: dict) -> set[str]:
        """Return group ids whose symbol ownership cannot be proved safely.

        Alpaca gives one signed quantity per OCC symbol.  If several active
        structures claim that symbol, their claims are safe only when they all
        have the same side and their declared quantities fit inside that net
        broker quantity.  This proves a close cannot exceed the account's
        exposure without pretending broker netting supplies lot identities.
        """
        claims: dict[str, list[tuple[str, str, int | None]]] = {}
        for group_id, group in groups.items():
            if group.get("closed"):
                continue
            for symbol, info in group.get("legs", {}).items():
                claims.setdefault(symbol, []).append(
                    (group_id, info.get("side", ""), _quantity(info.get("qty"))))

        conflicts: set[str] = set()
        for symbol, symbol_claims in claims.items():
            claimants = {group_id for group_id, _side, _qty in symbol_claims}
            quantities = [qty for _group_id, _side, qty in symbol_claims]
            sides = {side for _group_id, side, _qty in symbol_claims}
            position = broker.get(symbol)
            if (position is None or None in quantities or
                    sides not in ({"buy"}, {"sell"})):
                conflicts.update(claimants)
                continue
            try:
                broker_qty = int(float(position.qty))
            except (AttributeError, TypeError, ValueError):
                conflicts.update(claimants)
                continue
            side = next(iter(sides))
            expected_sign = 1 if side == "buy" else -1
            if broker_qty * expected_sign < sum(quantities):
                conflicts.update(claimants)
        return conflicts

    @staticmethod
    def _owned_legs(group: dict, broker: dict) -> list | None:
        """Return the group's declared legs, never the broker net quantity."""
        result = []
        for symbol, info in group.get("legs", {}).items():
            quantity = _quantity(info.get("qty"))
            position = broker.get(symbol)
            if quantity is None or quantity <= 0 or position is None:
                return None
            try:
                broker_qty = int(float(position.qty))
            except (AttributeError, TypeError, ValueError):
                return None
            expected_sign = 1 if info.get("side") == "buy" else -1
            if broker_qty * expected_sign < quantity:
                return None
            result.append((symbol, info.get("side"), quantity))
        return result or None

    @staticmethod
    def _prices(state: Any, legs: list) -> tuple[dict, dict]:
        prices, touch = {}, {}
        broker = {position.symbol: position for position in state.positions}
        for symbol, side, _quantity_value in legs:
            position = broker[symbol]
            price = float(getattr(position, "current_price", 0.0) or 0.0)
            contract = _contract(state, symbol)
            if price <= 0 and contract is not None:
                price = contract.bid if side == "buy" else contract.ask
            if price <= 0:
                continue
            prices[symbol] = price if side == "buy" else -price
            quoted = (contract.bid if side == "buy" else contract.ask) if contract else None
            touch[symbol] = quoted or price
        return prices, touch

    def _close_orphans(self, state: Any, executor: Any, managed_symbols: set[str]) -> int:
        submitted = 0
        for position in state.positions:
            symbol = position.symbol
            if symbol in managed_symbols:
                continue
            contract = _contract(state, symbol)
            if contract is None:
                continue
            try:
                quantity = int(float(position.qty))
            except (TypeError, ValueError):
                continue
            if quantity == 0:
                continue
            price = contract.bid if quantity > 0 else contract.ask
            if price is None or price <= 0:
                continue
            close = Proposal(
                engine="exit", underlying=_underlying(symbol), direction="neutral",
                structure="single_close",
                legs=[OptionLeg(symbol=symbol, side="sell" if quantity > 0 else "buy",
                                quantity=abs(quantity), strike=contract.strike,
                                contract_type=contract.contract_type,
                                expiration=contract.expiration,
                                ref_bid=contract.bid or 0.0,
                                ref_ask=contract.ask or 0.0)],
                limit_price=price, max_loss_dollars=0.0,
                thesis="orphan close", reason="ORPHAN")
            try:
                order = executor.submit(close, closing=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  EXIT FAILED orphan {symbol}: {exc}")
                continue
            self._record({"kind": "orphan_closed", "symbol": symbol,
                          "order_id": str(order.id)})
            submitted += 1
            print(f"  EXIT orphan {symbol} @ {price:.2f}")
        return submitted
