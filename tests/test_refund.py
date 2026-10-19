from uuid import uuid4

from acmg.commerce import Actor, build_commerce
from acmg.payments import MockPaymentProvider
from acmg.store import MemoryStore, StoreError


def actor(tool: str) -> Actor:
    return Actor(
        actor_type="agent",
        actor_id="ledgerlane-concierge",
        customer_id="demo-shopper",
        session_id="sess-refund",
        reason="The size does not fit.",
        correlation_id=str(uuid4()),
        tool_name=tool,
    )


async def _confirmed():
    store = MemoryStore()
    await store.seed()
    payments = MockPaymentProvider(store)
    commerce = build_commerce(store, payments)
    found = await commerce.search_catalog(actor("search_catalog"), "merino socks")
    sku = found["products"][0]["sku"]
    before = (await store.get_product(sku))["stock"]
    cart = await commerce.add_to_cart(actor("add_to_cart"), sku, 2)
    quote = await commerce.prepare_checkout(actor("prepare_checkout"), cart["cart_id"])
    order = await commerce.place_order(actor("place_order"), quote["quote_id"], True, "refund-place")
    return commerce, store, payments, order, sku, before


async def test_refund_restocks_and_reverses_the_capture():
    commerce, store, payments, order, sku, before = await _confirmed()
    assert (await store.get_product(sku))["stock"] == before - 2
    preview = await commerce.refund_order(actor("refund_order"), order["order_id"], False, "refund-ask")
    assert preview["confirmation_required"] is True
    assert payments.refund_calls == 0
    done = await commerce.refund_order(actor("refund_order"), order["order_id"], True, "refund-1")
    assert done["status"] == "refunded"
    assert (await store.get_product(sku))["stock"] == before
    payment = await store.get_payment_by_idempotency("refund-place:auth")
    assert payment["status"] == "refunded"
    replay = await commerce.refund_order(actor("refund_order"), order["order_id"], True, "refund-1")
    assert replay["replayed"] is True
    assert payments.refund_calls == 1


async def test_second_refund_key_is_rejected_after_success():
    commerce, store, payments, order, sku, before = await _confirmed()
    await commerce.refund_order(actor("refund_order"), order["order_id"], True, "refund-a")
    try:
        await commerce.refund_order(actor("refund_order"), order["order_id"], True, "refund-b")
        raised = False
    except StoreError as exc:
        raised = True
        assert exc.code == "already_refunded"
    assert raised
