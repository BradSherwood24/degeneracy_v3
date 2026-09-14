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


# ---------------------------------------------------------------------------
# Order-path audit (2026-09-14): the PRIMARY order V3.2 sends is the RESTING bucket-NO on a RANGE
# ticker (KXBTC-...), NOT a strike (KXBTCD-...). Every create test above uses a strike/15M ticker, so
# the resting-order create — the one that fires ~40x/window — was never cross-checked against the real
# proxy cap parser, and the proxy's DEFAULT prefixes ("KXBTC15M,KXBTCD") do NOT cover a KXBTC- range
# ticker. These pin (a) the ACTUAL executor REST body is proxy-accepted ONLY under a range-covering
# prefix, and (b) the arming gate (v32_caps_agree) and the proxy cap AGREE on the range series in the
# SAFE direction: any prefix set the arming gate accepts, the proxy also accepts a real KXBTC- bucket
# ticker under — so an armed window can never send a create the proxy then rejects for its prefix.
# ---------------------------------------------------------------------------
_RANGE_BUCKET_TICKER = "KXBTC-26SEP1418-B78850"   # a real range bucket-NO ticker (starts "KXBTC-")
_STRIKE_TICKER = "KXBTCD-26SEP1418-T78849.99"     # a real strike ticker (starts "KXBTCD-")


def _executor_rest_body():
    """The EXACT wire body the LiveExecutor POSTs for a resting bucket-NO (via its own _rest_body —
    not a hand-rolled dict), captured through a recording writer. No network, no proxy dialed."""
    from decimal import Decimal as _D

    from service.proxy_writer import WriteResponse
    from service.v32.actions import ActionKind, V32Action
    from service.v32.executor import LiveExecutor

    captured: dict = {}

    class _RecW:
        def rest_post(self, path, body):
            captured["body"] = body
            return WriteResponse(201, {"order": {"order_id": "oid", "client_order_id":
                                                 body.get("client_order_id"), "fill_count": "0.00",
                                                 "remaining_count": "1.00"}}, True)

        def rest_get(self, path, params=None):
            return {}   # pre-place invariant: empty resting list -> proceed

        def rest_delete(self, path):
            return WriteResponse(200, {"reduced_by": "0.00"}, True)

    ex = LiveExecutor(_RecW(), {_RANGE_BUCKET_TICKER: (78850.0, 78949.99)},
                      {_RANGE_BUCKET_TICKER: 2}, _FakeJournal(), 1789423200, 300,
                      clock=lambda: 0.0, sleep=lambda _s: None)
    act = V32Action(kind=ActionKind.PLACE_REST, ticker=_RANGE_BUCKET_TICKER, side="no",
                    action="buy", count=1, price=_D("0.54"),
                    expiration_epoch=1789423200 - 300, client_order_id="v32-x")
    ex.on_action(act, None, 1789423200 - 600)
    return captured["body"]


class _FakeJournal:
    def append(self, *a, **k):
        pass


def test_executor_rest_bucket_no_body_capped_only_under_range_prefix():
    import json
    body = _executor_rest_body()
    # sanity: the live-verified wire shape for a NO bid at n=0.54 (2026-09-14 incident-2 create body).
    assert body["ticker"] == _RANGE_BUCKET_TICKER
    assert body["side"] == "ask" and body["price"] == "0.4600"
    assert body["count"] == "1.00" and body["post_only"] is True
    assert body["time_in_force"] == "good_till_canceled" and body["exchange_index"] == 2
    entries = PROXY["parse_order_entries"](json.dumps(body).encode(), is_batch=False)
    # DEFAULT proxy prefixes do NOT cover a KXBTC- range ticker -> the resting create would be capped.
    v_default = PROXY["check_order_caps"](entries, MAX_CONTRACTS, ("KXBTC15M", "KXBTCD"))
    assert v_default is not None and v_default["cap"] == "order_ticker_prefixes"
    # a range-covering prefix accepts it (and exchange_index survives the parse, reaching Kalshi).
    assert PROXY["check_order_caps"](entries, MAX_CONTRACTS, ("KXBTC",)) is None
    assert entries[0].get("exchange_index") == 2


def test_arming_gate_prefix_acceptance_implies_proxy_accepts_both_series():
    # The SAFE-direction invariant that ties the arming gate to the proxy cap: for any prefix set the
    # arming check accepts, the REAL proxy must accept BOTH a range (KXBTC-) and a strike (KXBTCD-)
    # create — so an armed window never sends a create the proxy rejects for its ticker prefix. (The
    # reverse is allowed to be conservative: the gate may refuse a set the proxy would have accepted.)
    import json

    from service.v32.stops import v32_caps_agree

    range_entry = PROXY["parse_order_entries"](
        json.dumps({"ticker": _RANGE_BUCKET_TICKER, "count": "1.00", "price": "0.4600"}).encode(),
        is_batch=False)
    strike_entry = PROXY["parse_order_entries"](
        json.dumps({"ticker": _STRIKE_TICKER, "count": "1.00", "price": "0.9900"}).encode(),
        is_batch=False)
    candidate_prefix_sets = [
        ("KXBTC15M", "KXBTCD"),      # proxy default: range NOT covered -> gate must refuse
        ("KXBTC",),                  # covers both
        ("KXBTC-", "KXBTCD-"),       # covers both, exact
        ("KXBTCD",),                 # covers strike only -> gate must refuse (range uncovered)
        ("KX",),                     # covers both (broad)
        ("NOTBTC",),                 # covers neither -> gate must refuse
    ]
    for prefixes in candidate_prefix_sets:
        health = {"orders_enabled": True,
                  "caps": {"max_contracts_per_order": 2, "ticker_prefixes": list(prefixes)},
                  "orders_remaining_today": 1000}
        gate_ok, _ = v32_caps_agree(health, 1)
        if gate_ok:
            # arming accepted this prefix set -> the proxy MUST accept BOTH series' creates under it.
            assert PROXY["check_order_caps"](range_entry, MAX_CONTRACTS, prefixes) is None, (
                f"gate armed on {prefixes} but proxy caps the range create")
            assert PROXY["check_order_caps"](strike_entry, MAX_CONTRACTS, prefixes) is None, (
                f"gate armed on {prefixes} but proxy caps the strike create")
