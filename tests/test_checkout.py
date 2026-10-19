from uuid import uuid4

import pytest

from acmg.commerce import Actor, build_commerce, money
from acmg.payments import MockPaymentProvider
from acmg.store import IdempotencyConflict, MemoryStore


@pytest.fixture
async def world():
    store = MemoryStore()
    await store.seed()
    payments = MockPaymentProvider(store)
    return build_commerce(store, payments), store, payments


def actor(tool: str, reason: str = "Shopper confirmed the step.") -> Actor:
    return Actor(
        actor_type="agent",
        actor_id="ledgerlane-concierge",
        customer_id="demo-shopper",
        session_id="sess-1",
        reason=reason,
        correlation_id=str(uuid4()),
        tool_name=tool,
    )


async def _quote(world):
    commerce, store, payments = world
    found = await commerce.search_catalog(actor("search_catalog"), "trail running shoes", max_price_cents=12000)
    sku = found["products"][0]["sku"]
    cart = await commerce.add_to_cart(actor("add_to_cart"), sku, 1)
    quote = await commerce.prepare_checkout(actor("prepare_checkout"), cart["cart_id"])
    return commerce, store, payments, quote, sku


async def test_place_order_confirms_and_writes_who_what_why(world):
    commerce, store, payments, quote, sku = await _quote(world)
    before = (await store.get_product(sku))["stock"]
    result = await commerce.place_order(
        actor("place_order", "Shopper said yes to the quoted total."),
        quote["quote_id"],
        confirm=True,
        idempotency_key="place-1",
    )
    assert result["status"] == "confirmed"
    assert result["total"] == money(quote["total_cents"])
    assert result["tracking_number"].startswith("NL")
    assert (await store.get_product(sku))["stock"] == before - 1
    payment = await store.get_payment_by_idempotency("place-1:auth")
    assert payment["status"] == "captured"
    events = await commerce.list_audit(correlation_id=None, session_id="sess-1")
    place_events = [event for event in events if event["correlation_id"] and event["tool_name"] == "place_order"]
    assert place_events
    assert all(event["actor_id"] == "ledgerlane-concierge" for event in place_events)
    assert any(event["reason"] == "Shopper said yes to the quoted total." for event in place_events)
    actions = [event["action"] for event in place_events]
    assert "saga.capture_payment.succeeded" in actions
    assert "saga.open_shipment.succeeded" in actions


async def test_capture_failure_compensates_in_reverse(world):
    commerce, store, payments, quote, sku = await _quote(world)
    before = (await store.get_product(sku))["stock"]
    result = await commerce.place_order(
        actor("place_order", "Shopper asked to see a failed capture."),
        quote["quote_id"],
        confirm=True,
        idempotency_key="place-fail",
        simulate_failure="capture_payment",
    )
    assert result["ok"] is False
    assert result["status"] == "cancelled"
    assert "rolled the checkout back" in result["message"]
    assert (await store.get_product(sku))["stock"] == before
    payment = await store.get_payment_by_idempotency("place-fail:auth")
    assert payment["status"] == "voided"
    order = await store.get_order(result["order_id"])
    assert order["status"] == "cancelled"
    quote_row = await store.get_quote(quote["quote_id"])
    assert quote_row["status"] == "open"
    events = await store.list_audit(session_id="sess-1")
    actions = [event["action"] for event in events if event["saga_id"]]
    assert actions.index("saga.capture_payment.failed") < actions.index("saga.create_order.compensated")
    assert actions.index("saga.create_order.compensated") < actions.index("saga.authorize_payment.compensated")
    assert actions.index("saga.authorize_payment.compensated") < actions.index("saga.reserve_inventory.compensated")
    assert "saga.open_shipment.started" not in actions


async def test_idempotency_replays_without_a_second_charge(world):
    commerce, store, payments, quote, sku = await _quote(world)
    first = await commerce.place_order(actor("place_order"), quote["quote_id"], True, "same-key")
    second = await commerce.place_order(actor("place_order"), quote["quote_id"], True, "same-key")
    assert second["replayed"] is True
    assert second["order_id"] == first["order_id"]
    assert payments.authorize_calls == 1
    assert payments.capture_calls == 1


async def test_idempotency_conflict_on_different_payload(world):
    commerce, store, payments, quote, sku = await _quote(world)
    await commerce.place_order(actor("place_order"), quote["quote_id"], True, "shared")
    with pytest.raises(IdempotencyConflict):
        await commerce.place_order(
            actor("place_order"),
            quote["quote_id"],
            True,
            "shared",
            payment_method="declined_card",
        )


async def test_confirm_false_does_not_charge(world):
    commerce, store, payments, quote, sku = await _quote(world)
    result = await commerce.place_order(actor("place_order"), quote["quote_id"], False, "nope")
    assert result["confirmation_required"] is True
    assert payments.authorize_calls == 0
    assert await store.latest_order("demo-shopper") is None


async def test_declined_card_releases_stock(world):
    commerce, store, payments, quote, sku = await _quote(world)
    before = (await store.get_product(sku))["stock"]
    result = await commerce.place_order(
        actor("place_order"),
        quote["quote_id"],
        True,
        "declined",
        payment_method="declined_card",
    )
    assert result["status"] == "cancelled"
    assert result["order_id"] is None
    assert payments.authorize_calls == 1
    assert payments.capture_calls == 0
    assert (await store.get_product(sku))["stock"] == before
    assert await store.latest_order("demo-shopper") is None


async def test_sold_out_does_not_authorize(world):
    commerce, store, payments, quote, sku = await _quote(world)
    store.products[sku]["stock"] = 0
    result = await commerce.place_order(actor("place_order"), quote["quote_id"], True, "stock")
    assert result["status"] == "aborted"
    assert payments.authorize_calls == 0
    assert "sold out" in result["message"]
