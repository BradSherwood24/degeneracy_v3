"""Prove the Executor's order payloads pass the ACTUAL degeneracy-proxy cap parser.

House-law constraint (CONFESSED): a plain ``import proxy`` runs the module-level ``CONFIG = Config()``
-> ``load_dotenv(PROXY_DIR/'.env')`` -> ``_load_signer`` which would READ the proxy's .env and load
the RSA PEM (key material) into this process. That is forbidden. So instead of importing the module
we parse proxy.py with ``ast`` and exec ONLY its pure parser defs (the caps functions + the
_ORDER_CREATE_PATHS constant) in an isolated namespace. This uses the REAL proxy source text — a
drift in the proxy's parser breaks this test — without ever touching .env / *.pem or the network.
"""

from __future__ import annotations

import ast
import os
from decimal import Decimal, InvalidOperation

import pytest

from service.orders.envelope import (
    BATCH_CREATE_PATH,
    SINGLE_CREATE_PATH,
    build_batch,
    build_entry,
)
from service.ledger import IntentLeg

_HERE = os.path.dirname(os.path.abspath(__file__))
# tests -> pilot -> degeneracy_v3 -> Python_stuff -> degeneracy-proxy/proxy.py
_PROXY = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "degeneracy-proxy", "proxy.py")
)

# The pure parser surface we extract (NO Config, NO Signer, NO OrderBudget, NO handler).
_WANT = {
    "_ORDER_CREATE_PATHS",
    "BodyParseError",
    "is_order_create",
    "parse_count",
    "parse_order_entries",
    "check_order_caps",
}


def _load_proxy_parser():
    """Exec only the named pure defs/constants from proxy.py in an isolated namespace."""
    with open(_PROXY, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=_PROXY)
    kept: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in _WANT:
            kept.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & _WANT:
                kept.append(node)
    module = ast.Module(body=kept, type_ignores=[])
    ns: dict = {"json": __import__("json"), "Decimal": Decimal, "InvalidOperation": InvalidOperation}
    exec(compile(module, _PROXY, "exec"), ns)  # noqa: S102 - trusted local source, isolated ns
    missing = _WANT - set(ns)
    assert not missing, f"proxy parser extraction missing {missing}"
    return ns


PROXY = _load_proxy_parser()

MAX_CONTRACTS = 2
PREFIXES = ("KXBTC15M", "KXBTCD")


def leg(ticker, side, action, count, price, cid="c1", exchange_index=None):
    return IntentLeg(ticker, side, action, count, Decimal(str(price)), cid,
                     exchange_index=exchange_index)


def _passes_caps(entries):
    """Mirror the proxy's own order: body parse -> per-entry caps (excludes the stateful budget)."""
    violation = PROXY["check_order_caps"](entries, MAX_CONTRACTS, PREFIXES)
    return violation


def test_envelope_paths_are_recognized_creates_by_the_proxy():
    # End-to-end: the EXACT paths the envelope POSTs to must be recognized as order-creates by the
    # real proxy source (so they route to the orders host AND get capped). This is the load-bearing
    # cross-check after the 2026-08-21 events/orders endpoint fix on both sides.
    assert SINGLE_CREATE_PATH == "/trade-api/v2/portfolio/events/orders"
    assert BATCH_CREATE_PATH == "/trade-api/v2/portfolio/events/orders/batched"
    assert PROXY["is_order_create"]("POST", SINGLE_CREATE_PATH)
    assert PROXY["is_order_create"]("POST", BATCH_CREATE_PATH)
    # a GET on the same path is never a create; a per-order amend under the events namespace isn't
    assert not PROXY["is_order_create"]("GET", SINGLE_CREATE_PATH)
    assert not PROXY["is_order_create"]("POST", SINGLE_CREATE_PATH + "/ORD-1/amend")


def test_single_entry_passes_proxy_parser():
    e = build_entry(leg("KXBTCD-26AUG", "no", "buy", 1, "0.57"))
    entries = PROXY["parse_order_entries"](__import__("json").dumps(e).encode(), is_batch=False)
    assert PROXY["is_order_create"]("POST", SINGLE_CREATE_PATH)
    assert _passes_caps(entries) is None, _passes_caps(entries)


def test_batch_entry_passes_proxy_parser():
    e1 = build_entry(leg("KXBTCD-26AUG-H", "no", "buy", 1, "0.57", cid="a"))
    e2 = build_entry(leg("KXBTCD-26AUG-L", "yes", "buy", 1, "0.24", cid="b"))
    body = build_batch([e1, e2])
    import json

    assert PROXY["is_order_create"]("POST", BATCH_CREATE_PATH)
    entries = PROXY["parse_order_entries"](json.dumps(body).encode(), is_batch=True)
    assert len(entries) == 2
    assert _passes_caps(entries) is None


def test_fifteen_minute_deci_cent_ticker_passes():
    e = build_entry(leg("KXBTC15M-X", "yes", "buy", 1, "0.0010", cid="d"))
    import json

    entries = PROXY["parse_order_entries"](json.dumps(e).encode(), is_batch=False)
    assert _passes_caps(entries) is None


def test_shard2_entry_with_exchange_index_passes_proxy_parser():
    # Exchange sharding (2026-08-27 incident): the wire body now carries exchange_index. The REAL
    # proxy cap parser must still accept the entry (it reads count/ticker/price only; the extra key
    # is ignored), and the proxy forwards the body bytes UNCHANGED so exchange_index reaches Kalshi.
    import json
    e = build_entry(leg("KXBTCD-26AUG2620-T79199.99", "no", "buy", 1, "0.99", exchange_index=2))
    assert e["exchange_index"] == 2
    entries = PROXY["parse_order_entries"](json.dumps(e).encode(), is_batch=False)
    assert _passes_caps(entries) is None
    # the parsed entry retains the field (proof it is not stripped before forwarding)
    assert entries[0].get("exchange_index") == 2


def test_over_max_contracts_is_rejected_by_proxy():
    e = build_entry(leg("KXBTCD-X", "yes", "buy", 3, "0.46"))  # 3 > MAX_CONTRACTS 2
    import json

    entries = PROXY["parse_order_entries"](json.dumps(e).encode(), is_batch=False)
    v = _passes_caps(entries)
    assert v is not None and v["cap"] == "max_contracts_per_order"


def test_bad_ticker_prefix_is_rejected_by_proxy():
    e = build_entry(leg("NOTBTC-X", "yes", "buy", 1, "0.46"))
    import json

    entries = PROXY["parse_order_entries"](json.dumps(e).encode(), is_batch=False)
    v = _passes_caps(entries)
    assert v is not None and v["cap"] == "order_ticker_prefixes"


def test_count_and_price_are_fixed_point_strings_the_proxy_accepts():
    # the proxy's parse_count must accept our "1.00" count string as Decimal(1)
    assert PROXY["parse_count"]("1.00") == Decimal(1)
    assert PROXY["parse_count"]("2.00") == Decimal(2)


def test_extraction_touched_no_env_or_key_symbols():
    # the isolated namespace must NOT contain any config/signing symbol (proof we didn't run them)
    for forbidden in ("Config", "CONFIG", "Signer", "OrderBudget", "load_dotenv", "SESSION"):
        assert forbidden not in PROXY, f"{forbidden} leaked into the extracted parser namespace"
