"""Narrow, broker-specific error classifications used by safety state machines."""
from __future__ import annotations

import json


def is_explicit_client_order_absence(exc: Exception,
                                     client_order_id: str) -> bool:
    """Return true only for Alpaca's definitive 404 response for this ID.

    A null lookup or transport failure can be eventual consistency.  This
    exact structured response instead says that the queried client-order
    identity is not present in the account's order store.  Matching both the
    provider error code and client ID prevents an unrelated 404 from releasing
    reserved risk or a pending entry.
    """
    try:
        payload = json.loads(str(exc))
    except (TypeError, ValueError):
        return False
    return (
        payload.get("code") == 40410000
        and payload.get("message") == f"order not found for {client_order_id}"
    )
