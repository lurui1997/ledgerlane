"""Catalog, cart, checkout, and refund workflows on top of the saga runner."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import timedelta
from typing import Any
from uuid import uuid4

from acmg.catalog import QUOTE_TTL_MINUTES, shipping_cents
from acmg.payments import AuthorizeRequest, PaymentError, PaymentProvider
from acmg.saga import AuditLog, SagaContext, SagaEngine, SagaFailure, SagaStep
from acmg.store import IdempotencyConflict, InsufficientStock, StoreError, new_id, utcnow

STOPWORDS = {"a", "an", "the", "for", "me", "my", "some", "please", "under", "below"}


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def request_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class Actor:
    def __init__(
        self,
        *,
        actor_type: str,
        actor_id: str,
        customer_id: str,
        session_id: str,
        reason: str,
        correlation_id: str,
        tool_name: str,
    ):
        self.actor_type = actor_type
        self.actor_id = actor_id
        self.customer_id = customer_id
        self.session_id = session_id
        self.reason = reason[:500]
        self.correlation_id = correlation_id
        self.tool_name = tool_name

    def context(self, saga_name: str) -> SagaContext:
        return SagaContext(
            name=saga_name,
            saga_id=str(uuid4()),
            correlation_id=self.correlation_id,
            session_id=self.session_id,
            actor_type=self.actor_type,
            actor_id=self.actor_id,
            customer_id=self.customer_id,
            reason=self.reason,
            tool_name=self.tool_name,
        )


class CommerceService:
    def __init__(self, store: object, payments: PaymentProvider, audit: AuditLog, engine: SagaEngine):
        self.store = store
        self.payments = payments
        self.audit = audit
        self.engine = engine

    async def search_catalog(
        self,
        actor: Actor,
        query: str,
        category: str | None = None,
        max_price_cents: int | None = None,
        sort: str = "relevance",
        limit: int = 5,
    ) -> dict[str, Any]:
        products = await self.store.search_products()  # type: ignore[attr-defined]
        tokens = [token for token in re.split(r"\s+", query.lower()) if token and token not in STOPWORDS]
        matched = []
        for product in products:
            if category and product["category"] != category:
                continue
            if max_price_cents is not None and product["price_cents"] > max_price_cents:
                continue
            hay = " ".join(
                [
                    product["name"],
                    product["brand"],
                    product["category"],
                    product["description"],
                    product["synonyms"],
                ]
            ).lower()
            if tokens and not all(token in hay for token in tokens):
                continue
            hits = sum(hay.count(token) for token in tokens) if tokens else 0
            matched.append((hits, product))
        if sort == "price_asc":
            matched.sort(key=lambda item: (item[1]["price_cents"], -item[1]["rating"]))
        elif sort == "price_desc":
            matched.sort(key=lambda item: (-item[1]["price_cents"], -item[1]["rating"]))
        elif sort == "rating":
            matched.sort(key=lambda item: (-item[1]["rating"], item[1]["price_cents"]))
        else:
            matched.sort(key=lambda item: (-item[0], -item[1]["rating"], item[1]["price_cents"]))
        limit = max(1, min(limit, 8))
        found = [_public_product(item[1]) for item in matched[:limit]]
        payload = {
            "search_id": new_id("S-"),
            "query": query,
            "products": found,
            "message": _search_message(found),
        }
        await self._tool_event(actor, "succeeded", {"query": query, "sort": sort}, {"count": len(found), "skus": [item["sku"] for item in found]})
        return payload

    async def get_product(self, actor: Actor, sku: str) -> dict[str, Any]:
        product = await self.store.get_product(sku)  # type: ignore[attr-defined]
        if product is None:
            raise StoreError("not_found", "I can't find that product.")
        payload = {"product": _public_product(product)}
        await self._tool_event(actor, "succeeded", {"sku": sku}, {"sku": sku})
        return payload

    async def compare_products(self, actor: Actor, skus: list[str]) -> dict[str, Any]:
        if len(skus) < 2:
            raise StoreError("need_two", "Give me at least two products to compare.")
        products = []
        for sku in skus[:4]:
            product = await self.store.get_product(sku)  # type: ignore[attr-defined]
            if product is None:
                raise StoreError("not_found", f"I can't find {sku}.")
            products.append(_public_product(product))
        ranked = sorted(products, key=lambda item: (item["price_cents"], -item["rating"]))
        cheapest = ranked[0]
        best_rated = max(products, key=lambda item: (item["rating"], -item["price_cents"]))
        others = [item for item in products if item["sku"] != cheapest["sku"]]
        if cheapest["sku"] == best_rated["sku"] and others:
            other = others[0]
            message = (
                f"{cheapest['name']} is the lower price at {cheapest['price']} "
                f"and the higher rating at {cheapest['rating']}. "
                f"{other['name']} is {other['price']}."
            )
        else:
            message = (
                f"{cheapest['name']} is the lower price at {cheapest['price']}. "
                f"{best_rated['name']} has the higher rating at {best_rated['rating']}."
            )
        payload = {
            "products": products,
            "cheapest_sku": cheapest["sku"],
            "recommended_sku": best_rated["sku"],
            "message": message,
        }
        await self._tool_event(actor, "succeeded", {"skus": skus}, {"cheapest_sku": cheapest["sku"]})
        return payload

    async def add_to_cart(self, actor: Actor, sku: str, qty: int = 1, cart_id: str | None = None) -> dict[str, Any]:
        if qty < 1 or qty > 10:
            raise StoreError("bad_qty", "I can add between 1 and 10 of an item.")
        product = await self.store.get_product(sku)  # type: ignore[attr-defined]
        if product is None:
            raise StoreError("not_found", "I can't find that product.")
        if product["stock"] < qty:
            raise StoreError("out_of_stock", f"{product['name']} doesn't have enough left in stock.")
        if cart_id:
            cart = await self.store.get_cart(cart_id)  # type: ignore[attr-defined]
            if cart is None or cart["customer_id"] != actor.customer_id:
                raise StoreError("not_found", "I can't find that cart.")
        else:
            cart = await self.store.create_cart(actor.customer_id)  # type: ignore[attr-defined]
        existing = next((item for item in cart["items"] if item["sku"] == sku), None)
        new_qty = qty + (existing["qty"] if existing else 0)
        if product["stock"] < new_qty:
            raise StoreError("out_of_stock", f"Only {product['stock']} of {product['name']} are left.")
        cart = await self.store.save_cart_item(  # type: ignore[attr-defined]
            cart["id"],
            sku,
            new_qty,
            product["price_cents"],
            product["name"],
        )
        payload = _cart_payload(cart, f"Added {product['name']}. Cart total is {_cart_total_text(cart)}.")
        await self._tool_event(actor, "succeeded", {"sku": sku, "qty": qty}, {"cart_id": cart["id"]})
        return payload

    async def update_cart(self, actor: Actor, cart_id: str, sku: str, qty: int) -> dict[str, Any]:
        cart = await self._own_cart(actor, cart_id)
        if qty < 0 or qty > 10:
            raise StoreError("bad_qty", "Quantity has to be between 0 and 10. Zero removes the item.")
        product = await self.store.get_product(sku)  # type: ignore[attr-defined]
        if product is None:
            raise StoreError("not_found", "I can't find that product.")
        if qty > product["stock"]:
            raise StoreError("out_of_stock", f"Only {product['stock']} of {product['name']} are left.")
        name = product["name"]
        price = product["price_cents"]
        existing = next((item for item in cart["items"] if item["sku"] == sku), None)
        if existing:
            name = existing["name"]
            price = existing["unit_price_cents"]
        cart = await self.store.save_cart_item(cart_id, sku, qty, price, name)  # type: ignore[attr-defined]
        payload = _cart_payload(cart, "Updated the cart." if qty else f"Removed {name}.")
        await self._tool_event(actor, "succeeded", {"cart_id": cart_id, "sku": sku, "qty": qty}, {"cart_id": cart_id})
        return payload

    async def view_cart(self, actor: Actor, cart_id: str) -> dict[str, Any]:
        cart = await self._own_cart(actor, cart_id)
        payload = _cart_payload(cart, f"Your cart total is {_cart_total_text(cart)}.")
        await self._tool_event(actor, "succeeded", {"cart_id": cart_id}, {"items": len(cart["items"])})
        return payload

    async def prepare_checkout(self, actor: Actor, cart_id: str) -> dict[str, Any]:
        cart = await self._own_cart(actor, cart_id)
        if not cart["items"]:
            raise StoreError("empty_cart", "The cart is empty. Add something before checkout.")
        subtotal = sum(item["qty"] * item["unit_price_cents"] for item in cart["items"])
        shipping = shipping_cents(subtotal)
        quote = {
            "id": new_id("Q-"),
            "cart_id": cart_id,
            "customer_id": actor.customer_id,
            "currency": "USD",
            "subtotal_cents": subtotal,
            "shipping_cents": shipping,
            "total_cents": subtotal + shipping,
            "items": cart["items"],
            "status": "open",
            "expires_at": utcnow() + timedelta(minutes=QUOTE_TTL_MINUTES),
            "created_at": utcnow(),
        }
        stored = await self.store.save_quote(quote)  # type: ignore[attr-defined]
        names = ", ".join(f"{item['qty']} {item['name']}" for item in stored["items"])
        message = (
            f"Checkout {stored['id']} is {money(stored['total_cents'])} "
            f"({money(stored['subtotal_cents'])} plus {money(stored['shipping_cents'])} shipping) "
            f"for {names}. Nothing is charged until you confirm."
        )
        payload = {
            "quote_id": stored["id"],
            "cart_id": cart_id,
            "items": stored["items"],
            "subtotal_cents": stored["subtotal_cents"],
            "shipping_cents": stored["shipping_cents"],
            "total_cents": stored["total_cents"],
            "total": money(stored["total_cents"]),
            "expires_at": stored["expires_at"].isoformat(),
            "confirmation_required": True,
            "message": message,
        }
        await self._tool_event(actor, "succeeded", {"cart_id": cart_id}, {"quote_id": stored["id"], "total_cents": stored["total_cents"]})
        return payload

    async def place_order(
        self,
        actor: Actor,
        quote_id: str,
        confirm: bool,
        idempotency_key: str,
        payment_method: str = "mock_card",
        simulate_failure: str | None = None,
    ) -> dict[str, Any]:
        quote = await self.store.get_quote(quote_id)  # type: ignore[attr-defined]
        if quote is None or quote["customer_id"] != actor.customer_id:
            raise StoreError("not_found", "I can't find that checkout.")
        if not confirm:
            payload = {
                "confirmation_required": True,
                "quote_id": quote_id,
                "total_cents": quote["total_cents"],
                "total": money(quote["total_cents"]),
                "message": (
                    f"The total is {money(quote['total_cents'])}. "
                    "Ask the shopper to confirm before I place the order. Nothing has been charged."
                ),
            }
            await self._tool_event(actor, "succeeded", {"quote_id": quote_id, "confirm": False}, {"confirmation_required": True})
            return payload
        if not idempotency_key:
            raise StoreError("missing_key", "An idempotency key is required so a retry cannot charge twice.")
        fingerprint = request_hash(
            {
                "quote_id": quote_id,
                "customer_id": actor.customer_id,
                "payment_method": payment_method,
                "simulate_failure": simulate_failure or "",
            }
        )
        begun = await self.store.idempotency_begin("place_order", idempotency_key, fingerprint)  # type: ignore[attr-defined]
        if begun["state"] == "exists":
            record = begun["record"]
            if record["status"] == "in_progress" or record["response"] is None:
                raise StoreError("in_progress", "That order is already being placed. Wait rather than retrying it.")
            replay = dict(record["response"])
            replay["replayed"] = True
            replay["idempotency_key"] = idempotency_key
            replay["charges"] = await self._charges_for_quote(quote_id)
            await self._tool_event(
                actor,
                "succeeded",
                {"idempotency_key": idempotency_key},
                {"replayed": True, "order_id": replay.get("order_id"), "charges": replay["charges"]},
            )
            return replay

        ctx = actor.context("place_order")
        ctx.bag.update(
            {
                "quote_id": quote_id,
                "payment_method": payment_method,
                "idempotency_key": idempotency_key,
                "simulate_failure": simulate_failure or "",
            }
        )
        result = await self.engine.run(ctx, self._place_steps())
        response = self._present_place(result, ctx)
        response["idempotency_key"] = idempotency_key
        response["charges"] = await self._charges_for_quote(quote_id)
        await self.store.idempotency_finish(  # type: ignore[attr-defined]
            "place_order",
            idempotency_key,
            "completed" if result.status == "completed" else "failed",
            response,
        )
        await self._tool_event(
            actor,
            "succeeded" if result.status == "completed" else "failed",
            {"quote_id": quote_id, "simulate_failure": simulate_failure},
            {"status": result.status, "order_id": response.get("order_id")},
            error=result.error,
        )
        return response

    async def get_order(self, actor: Actor, order_id: str) -> dict[str, Any]:
        order = await self._own_order(actor, order_id)
        payload = _order_payload(order, _order_message(order))
        await self._tool_event(actor, "succeeded", {"order_id": order_id}, {"status": order["status"]})
        return payload

    async def track_order(self, actor: Actor, order_id: str | None = None) -> dict[str, Any]:
        if order_id:
            order = await self._own_order(actor, order_id)
        else:
            order = await self.store.latest_order(actor.customer_id)  # type: ignore[attr-defined]
            if order is None:
                raise StoreError("not_found", "There isn't an order to track yet.")
        payload = _order_payload(order, _order_message(order))
        await self._tool_event(actor, "succeeded", {"order_id": order["id"]}, {"status": order["status"]})
        return payload

    async def refund_order(
        self,
        actor: Actor,
        order_id: str,
        confirm: bool,
        idempotency_key: str,
        simulate_failure: str | None = None,
    ) -> dict[str, Any]:
        order = await self._own_order(actor, order_id)
        if not confirm:
            payload = {
                "confirmation_required": True,
                "order_id": order_id,
                "total": money(order["total_cents"]),
                "message": (
                    f"I can refund {money(order['total_cents'])} for order {order_id} "
                    "and put the items back in stock. Ask the shopper to confirm. Nothing has been refunded."
                ),
            }
            await self._tool_event(actor, "succeeded", {"order_id": order_id, "confirm": False}, {"confirmation_required": True})
            return payload
        fingerprint = request_hash(
            {"order_id": order_id, "customer_id": actor.customer_id, "simulate_failure": simulate_failure or ""}
        )
        begun = await self.store.idempotency_begin("refund_order", idempotency_key, fingerprint)  # type: ignore[attr-defined]
        if begun["state"] == "exists":
            record = begun["record"]
            if record["response"] is None:
                raise StoreError("in_progress", "That refund is already running.")
            replay = dict(record["response"])
            replay["replayed"] = True
            await self._tool_event(actor, "succeeded", {"idempotency_key": idempotency_key}, {"replayed": True})
            return replay
        if order["status"] == "refunded":
            await self.store.idempotency_finish(  # type: ignore[attr-defined]
                "refund_order",
                idempotency_key,
                "failed",
                {"ok": False, "message": f"Order {order_id} is already refunded."},
            )
            raise StoreError("already_refunded", f"Order {order_id} is already refunded.")
        if order["status"] != "confirmed":
            await self.store.idempotency_finish(  # type: ignore[attr-defined]
                "refund_order",
                idempotency_key,
                "failed",
                {"ok": False, "message": f"Order {order_id} is {order['status']} and can't be refunded."},
            )
            raise StoreError("not_refundable", f"Order {order_id} is {order['status']} and can't be refunded.")
        ctx = actor.context("refund_order")
        ctx.bag.update(
            {
                "order": order,
                "order_id": order_id,
                "idempotency_key": idempotency_key,
                "simulate_failure": simulate_failure or "",
            }
        )
        result = await self.engine.run(ctx, self._refund_steps())
        response = self._present_refund(result, ctx)
        await self.store.idempotency_finish(  # type: ignore[attr-defined]
            "refund_order",
            idempotency_key,
            "completed" if result.status == "completed" else "failed",
            response,
        )
        await self._tool_event(
            actor,
            "succeeded" if result.status == "completed" else "failed",
            {"order_id": order_id},
            {"status": result.status},
            error=result.error,
        )
        return response

    async def list_audit(self, session_id: str | None = None, correlation_id: str | None = None) -> list[dict[str, Any]]:
        rows = await self.store.list_audit(session_id=session_id, correlation_id=correlation_id)  # type: ignore[attr-defined]
        public = []
        for row in rows:
            created = row["created_at"]
            public.append(
                {
                    "id": row["id"],
                    "event_id": row["event_id"],
                    "correlation_id": row["correlation_id"],
                    "saga_id": row.get("saga_id"),
                    "session_id": row.get("session_id"),
                    "actor_type": row["actor_type"],
                    "actor_id": row["actor_id"],
                    "customer_id": row.get("customer_id"),
                    "tool_name": row.get("tool_name"),
                    "action": row["action"],
                    "reason": row.get("reason"),
                    "status": row["status"],
                    "error": row.get("error"),
                    "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created),
                }
            )
        return public

    async def _charges_for_quote(self, quote_id: str) -> dict[str, int]:
        """Count real payment rows for this quote: how many auths exist and how many are captured."""
        rows = await self.store.payments_with_key_fragment(quote_id)  # type: ignore[attr-defined]
        return {
            "authorizations": len(rows),
            "captured": sum(1 for row in rows if row.get("status") == "captured"),
            "voided": sum(1 for row in rows if row.get("status") == "voided"),
        }

    def _place_steps(self) -> list[SagaStep]:
        return [
            SagaStep("reserve_inventory", self._reserve, self._release),
            SagaStep("authorize_payment", self._authorize, self._void),
            SagaStep("create_order", self._create_order, self._cancel_order),
            SagaStep("capture_payment", self._capture, self._refund_capture),
            SagaStep("open_shipment", self._ship, self._cancel_shipment),
        ]

    def _refund_steps(self) -> list[SagaStep]:
        return [
            SagaStep("refund_payment", self._refund_payment, None),
            SagaStep("restock_items", self._restock, None),
            SagaStep("mark_refunded", self._mark_refunded, None),
        ]

    async def _reserve(self, ctx: SagaContext) -> dict[str, Any]:
        self._maybe_fail(ctx, "reserve_inventory")
        quote = await self.store.get_quote(ctx.bag["quote_id"])  # type: ignore[attr-defined]
        if quote is None:
            raise SagaFailure("I can't find that checkout.")
        try:
            await self.store.claim_quote_and_reserve(quote["id"], quote["items"])  # type: ignore[attr-defined]
        except InsufficientStock as exc:
            raise SagaFailure(f"{exc.sku} just sold out. Nothing was charged.") from exc
        except StoreError as exc:
            raise SagaFailure(exc.message) from exc
        ctx.bag["quote"] = quote
        ctx.bag["reserved_items"] = quote["items"]
        return {"quote_id": quote["id"], "items": quote["items"]}

    async def _release(self, ctx: SagaContext, output: dict[str, Any]) -> None:
        await self.store.release_reservation(output["quote_id"], output["items"])  # type: ignore[attr-defined]

    async def _authorize(self, ctx: SagaContext) -> dict[str, Any]:
        self._maybe_fail(ctx, "authorize_payment")
        quote = ctx.bag["quote"]
        result = await self.payments.authorize(
            AuthorizeRequest(
                amount_cents=quote["total_cents"],
                currency=quote["currency"],
                customer_id=ctx.customer_id,
                order_ref=quote["id"],
                idempotency_key=ctx.bag["idempotency_key"] + ":auth",
                payment_method=ctx.bag["payment_method"],
            )
        )
        ctx.bag["authorization_id"] = result.authorization_id
        return {"authorization_id": result.authorization_id}

    async def _void(self, ctx: SagaContext, output: dict[str, Any]) -> None:
        await self.payments.void(output["authorization_id"], ctx.bag["idempotency_key"] + ":void")

    async def _create_order(self, ctx: SagaContext) -> dict[str, Any]:
        self._maybe_fail(ctx, "create_order")
        quote = ctx.bag["quote"]
        order = {
            "id": new_id("LL-"),
            "customer_id": ctx.customer_id,
            "quote_id": quote["id"],
            "status": "payment_authorized",
            "currency": quote["currency"],
            "subtotal_cents": quote["subtotal_cents"],
            "shipping_cents": quote["shipping_cents"],
            "total_cents": quote["total_cents"],
            "items": quote["items"],
            "payment_authorization_id": ctx.bag["authorization_id"],
            "payment_capture_id": None,
            "payment_method": ctx.bag["payment_method"],
            "tracking_number": None,
            "shipment_status": None,
            "idempotency_key": ctx.bag["idempotency_key"],
        }
        stored = await self.store.create_order(order)  # type: ignore[attr-defined]
        ctx.bag["order_id"] = stored["id"]
        return {"order_id": stored["id"]}

    async def _cancel_order(self, ctx: SagaContext, output: dict[str, Any]) -> None:
        await self.store.update_order(output["order_id"], status="cancelled", shipment_status="cancelled")  # type: ignore[attr-defined]

    async def _capture(self, ctx: SagaContext) -> dict[str, Any]:
        self._maybe_fail(ctx, "capture_payment")
        result = await self.payments.capture(ctx.bag["authorization_id"], ctx.bag["idempotency_key"] + ":capture")
        await self.store.update_order(  # type: ignore[attr-defined]
            ctx.bag["order_id"],
            payment_capture_id=result.capture_id,
            status="paid",
        )
        ctx.bag["capture_id"] = result.capture_id
        return {"capture_id": result.capture_id, "order_id": ctx.bag["order_id"]}

    async def _refund_capture(self, ctx: SagaContext, output: dict[str, Any]) -> None:
        quote = ctx.bag["quote"]
        await self.payments.refund(
            output["capture_id"],
            quote["total_cents"],
            ctx.bag["idempotency_key"] + ":compensate-refund",
            "Checkout saga compensation",
        )

    async def _ship(self, ctx: SagaContext) -> dict[str, Any]:
        self._maybe_fail(ctx, "open_shipment")
        tracking = "NL" + "".join(secrets.choice("0123456789") for _ in range(8))
        await self.store.update_order(  # type: ignore[attr-defined]
            ctx.bag["order_id"],
            status="confirmed",
            tracking_number=tracking,
            shipment_status="label_created",
        )
        return {"order_id": ctx.bag["order_id"], "tracking_number": tracking}

    async def _cancel_shipment(self, ctx: SagaContext, output: dict[str, Any]) -> None:
        await self.store.update_order(output["order_id"], shipment_status="cancelled")  # type: ignore[attr-defined]

    async def _refund_payment(self, ctx: SagaContext) -> dict[str, Any]:
        self._maybe_fail(ctx, "refund_payment")
        order = ctx.bag["order"]
        if not order.get("payment_capture_id"):
            raise SagaFailure("There is no charge on that order to refund.")
        result = await self.payments.refund(
            order["payment_capture_id"],
            order["total_cents"],
            ctx.bag["idempotency_key"] + ":refund",
            ctx.reason,
        )
        return {"refund_id": result.refund_id, "capture_id": result.capture_id}

    async def _restock(self, ctx: SagaContext) -> dict[str, Any]:
        self._maybe_fail(ctx, "restock_items")
        order = ctx.bag["order"]
        for item in order["items"]:
            product = await self.store.get_product(item["sku"])  # type: ignore[attr-defined]
            if product is None:
                raise SagaFailure(f"I refunded the charge but could not restock {item['sku']}.")
            # release_reservation also reopens a quote; restock is a direct stock add.
            await self._add_stock(item["sku"], item["qty"])
        return {"items": order["items"]}

    async def _add_stock(self, sku: str, qty: int) -> None:
        if hasattr(self.store, "products"):
            self.store.products[sku]["stock"] += qty  # type: ignore[attr-defined]
            return
        pool = self.store.pool  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            await conn.execute("UPDATE products SET stock = stock + $2 WHERE sku = $1", sku, qty)

    async def _mark_refunded(self, ctx: SagaContext) -> dict[str, Any]:
        self._maybe_fail(ctx, "mark_refunded")
        order = await self.store.update_order(  # type: ignore[attr-defined]
            ctx.bag["order_id"],
            status="refunded",
            shipment_status="cancelled",
        )
        return {"order_id": order["id"], "status": order["status"]}

    def _maybe_fail(self, ctx: SagaContext, step: str) -> None:
        if ctx.bag.get("simulate_failure") == step:
            raise SagaFailure(f"Simulated failure at {step.replace('_', ' ')}. Nothing further was committed.")

    def _present_place(self, result: Any, ctx: SagaContext) -> dict[str, Any]:
        saga = result.as_dict()
        if result.status == "completed":
            order_id = ctx.bag["order_id"]
            tracking = next(step.output.get("tracking_number") for step in result.steps if step.name == "open_shipment")
            total = ctx.bag["quote"]["total_cents"]
            return {
                "ok": True,
                "order_id": order_id,
                "status": "confirmed",
                "total_cents": total,
                "total": money(total),
                "tracking_number": tracking,
                "message": (
                    f"Order {order_id} is confirmed for {money(total)}. "
                    f"Tracking number {tracking}. A label has been created."
                ),
                "saga": saga,
            }
        return {
            "ok": False,
            "order_id": ctx.bag.get("order_id"),
            "status": "cancelled" if result.status == "compensated" else result.status,
            "message": (
                "I rolled the checkout back. The card was not left captured, and reserved stock was released. "
                + (result.error or "")
            ),
            "saga": saga,
        }

    def _present_refund(self, result: Any, ctx: SagaContext) -> dict[str, Any]:
        order = ctx.bag["order"]
        saga = result.as_dict()
        if result.status == "completed":
            return {
                "ok": True,
                "order_id": order["id"],
                "status": "refunded",
                "total": money(order["total_cents"]),
                "message": f"Refunded {money(order['total_cents'])} for order {order['id']}. The items are back in stock.",
                "saga": saga,
            }
        return {
            "ok": False,
            "order_id": order["id"],
            "status": result.status,
            "message": "I could not finish the refund. " + (result.error or "Please try again."),
            "saga": saga,
        }

    async def _own_cart(self, actor: Actor, cart_id: str) -> dict[str, Any]:
        cart = await self.store.get_cart(cart_id)  # type: ignore[attr-defined]
        if cart is None or cart["customer_id"] != actor.customer_id:
            raise StoreError("not_found", "I can't find that cart.")
        return cart

    async def _own_order(self, actor: Actor, order_id: str) -> dict[str, Any]:
        order = await self.store.get_order(order_id)  # type: ignore[attr-defined]
        if order is None or order["customer_id"] != actor.customer_id:
            raise StoreError("not_found", "I can't find that order.")
        return order

    async def _tool_event(
        self,
        actor: Actor,
        status: str,
        tool_input: Any,
        tool_output: Any,
        error: str | None = None,
    ) -> None:
        ctx = actor.context(actor.tool_name)
        await self.audit.record(
            ctx,
            action=f"tool.{actor.tool_name}",
            status=status,
            input=tool_input,
            output=tool_output,
            error=error,
        )


def _public_product(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "sku": product["sku"],
        "name": product["name"],
        "brand": product["brand"],
        "category": product["category"],
        "description": product["description"],
        "price_cents": product["price_cents"],
        "price": money(product["price_cents"]),
        "currency": product["currency"],
        "stock": product["stock"],
        "in_stock": product["stock"] > 0,
        "rating": product["rating"],
        "attributes": product["attributes"],
    }


def _search_message(products: list[dict[str, Any]]) -> str:
    if not products:
        return "Nothing matched. Try a broader ask, or drop the price cap."
    lead = products[0]
    return f"I found {len(products)}. The first is {lead['name']} at {lead['price']}."


def _cart_total_cents(cart: dict[str, Any]) -> int:
    return sum(item["qty"] * item["unit_price_cents"] for item in cart["items"])


def _cart_total_text(cart: dict[str, Any]) -> str:
    return money(_cart_total_cents(cart))


def _cart_payload(cart: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "cart_id": cart["id"],
        "items": cart["items"],
        "subtotal_cents": _cart_total_cents(cart),
        "subtotal": _cart_total_text(cart),
        "message": message,
    }


def _order_message(order: dict[str, Any]) -> str:
    if order["status"] == "confirmed":
        return (
            f"Order {order['id']} is confirmed for {money(order['total_cents'])}. "
            f"Shipment status is {order['shipment_status']}. Tracking {order['tracking_number']}."
        )
    if order["status"] == "refunded":
        return f"Order {order['id']} was refunded for {money(order['total_cents'])}."
    if order["status"] == "cancelled":
        return f"Order {order['id']} was cancelled. No capture remains."
    return f"Order {order['id']} is {order['status']}."


def _order_payload(order: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "order_id": order["id"],
        "status": order["status"],
        "items": order["items"],
        "total_cents": order["total_cents"],
        "total": money(order["total_cents"]),
        "tracking_number": order.get("tracking_number"),
        "shipment_status": order.get("shipment_status"),
        "message": message,
    }


def build_commerce(store: object, payments: PaymentProvider) -> CommerceService:
    audit = AuditLog(store)
    engine = SagaEngine(audit)
    return CommerceService(store, payments, audit, engine)
