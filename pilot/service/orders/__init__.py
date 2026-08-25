"""orders/ — the order wire layer for the Phase-3 Executor.

Two pieces:
  * ``translate.to_v2_order`` — the direction-critical legacy->V2 mapping, PORTED VERBATIM from
    ``degeneracy_v2/kalshi/order_translate.py`` (its unit tests carried over verbatim too). This is
    the ONLY automated guard on the buy/sell bid/ask direction; a sign-inverted exit is invisible to
    every replay/sim test, so the mapping is locked by tests.
  * ``envelope`` — the NEW wire-envelope builder for the verified V2 order shape (single + batch),
    the per-entry response parser (synchronous fill truth), and the proxy CREATE paths.
"""

from service.orders.envelope import (  # noqa: F401
    BATCH_CREATE_PATH,
    SINGLE_CREATE_PATH,
    OrderResponse,
    build_batch,
    build_entry,
    new_client_order_id,
    parse_batch_response,
    parse_single_response,
)
from service.orders.translate import to_v2_order  # noqa: F401
