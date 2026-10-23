"""Commerce persistence. MemoryStore backs unit tests; PostgresStore is the demo store."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from acmg.catalog import PRODUCTS


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    import secrets

    body = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"{prefix}{body}"


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=_json_default))


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class StoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class IdempotencyConflict(StoreError):
    def __init__(self) -> None:
        super().__init__(
            "idempotency_conflict",
            "That idempotency key was already used for a different checkout.",
        )


class InsufficientStock(StoreError):
    def __init__(self, sku: str) -> None:
        super().__init__("insufficient_stock", f"{sku} does not have enough stock.")
        self.sku = sku


class MemoryStore:
    def __init__(self) -> None:
        self.products: dict[str, dict[str, Any]] = {}
        self.carts: dict[str, dict[str, Any]] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, dict[str, Any]] = {}
        self.payments_by_idem: dict[str, str] = {}
        self.idempotency: dict[tuple[str, str], dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self._audit_seq = 0

    async def close(self) -> None:
        return None

    async def seed(self) -> None:
        if self.products:
            return
        for product in PRODUCTS:
            self.products[product["sku"]] = deepcopy(product)

    async def search_products(self) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self.products.values()]

    async def get_product(self, sku: str) -> dict[str, Any] | None:
        product = self.products.get(sku)
        return deepcopy(product) if product else None

    async def create_cart(self, customer_id: str) -> dict[str, Any]:
        cart = {
            "id": new_id("C-"),
            "customer_id": customer_id,
            "currency": "USD",
            "items": [],
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        self.carts[cart["id"]] = cart
        return deepcopy(cart)

    async def get_cart(self, cart_id: str) -> dict[str, Any] | None:
        cart = self.carts.get(cart_id)
        return deepcopy(cart) if cart else None

    async def save_cart_item(
        self,
        cart_id: str,
        sku: str,
        qty: int,
        unit_price_cents: int,
        name: str,
    ) -> dict[str, Any]:
        cart = self.carts[cart_id]
        items = [item for item in cart["items"] if item["sku"] != sku]
        if qty > 0:
            items.append(
                {
                    "sku": sku,
                    "qty": qty,
                    "unit_price_cents": unit_price_cents,
                    "name": name,
                }
            )
        cart["items"] = items
        cart["updated_at"] = utcnow()
        return deepcopy(cart)

    async def save_quote(self, quote: dict[str, Any]) -> dict[str, Any]:
        self.quotes[quote["id"]] = deepcopy(quote)
        return deepcopy(quote)

    async def get_quote(self, quote_id: str) -> dict[str, Any] | None:
        quote = self.quotes.get(quote_id)
        return deepcopy(quote) if quote else None

    async def claim_quote_and_reserve(self, quote_id: str, items: list[dict[str, Any]]) -> None:
        quote = self.quotes.get(quote_id)
        if quote is None or quote["status"] != "open" or quote["expires_at"] <= utcnow():
            raise StoreError("quote_unavailable", "That checkout is no longer open.")
        for item in items:
            product = self.products[item["sku"]]
            if product["stock"] < item["qty"]:
                raise InsufficientStock(item["sku"])
        quote["status"] = "consumed"
        for item in items:
            self.products[item["sku"]]["stock"] -= item["qty"]

    async def payments_with_key_fragment(self, fragment: str) -> list[dict[str, Any]]:
        return [deepcopy(p) for p in self.payments.values() if fragment in (p.get("idempotency_key") or "")]

    async def release_reservation(self, quote_id: str, items: list[dict[str, Any]]) -> None:
        quote = self.quotes.get(quote_id)
        if quote and quote["status"] == "consumed":
            quote["status"] = "open"
        for item in items:
            product = self.products.get(item["sku"])
            if product:
                product["stock"] += item["qty"]

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        order = deepcopy(order)
        order.setdefault("created_at", utcnow())
        order["updated_at"] = utcnow()
        self.orders[order["id"]] = order
        return deepcopy(order)

    async def update_order(self, order_id: str, **fields: Any) -> dict[str, Any]:
        order = self.orders[order_id]
        order.update(fields)
        order["updated_at"] = utcnow()
        return deepcopy(order)

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        order = self.orders.get(order_id)
        return deepcopy(order) if order else None

    async def latest_order(self, customer_id: str) -> dict[str, Any] | None:
        rows = [order for order in self.orders.values() if order["customer_id"] == customer_id]
        if not rows:
            return None
        rows.sort(key=lambda item: item["created_at"])
        return deepcopy(rows[-1])

    async def insert_payment(self, payment: dict[str, Any]) -> dict[str, Any]:
        self.payments[payment["authorization_id"]] = deepcopy(payment)
        self.payments_by_idem[payment["idempotency_key"]] = payment["authorization_id"]
        return deepcopy(payment)

    async def get_payment(self, authorization_id: str) -> dict[str, Any] | None:
        payment = self.payments.get(authorization_id)
        return deepcopy(payment) if payment else None

    async def get_payment_by_capture(self, capture_id: str) -> dict[str, Any] | None:
        for payment in self.payments.values():
            if payment.get("capture_id") == capture_id:
                return deepcopy(payment)
        return None

    async def get_payment_by_idempotency(self, key: str) -> dict[str, Any] | None:
        auth_id = self.payments_by_idem.get(key)
        if not auth_id:
            return None
        return await self.get_payment(auth_id)

    async def save_payment(self, payment: dict[str, Any]) -> dict[str, Any]:
        payment = deepcopy(payment)
        payment["updated_at"] = utcnow()
        self.payments[payment["authorization_id"]] = payment
        return deepcopy(payment)

    async def idempotency_begin(self, scope: str, key: str, request_hash: str) -> dict[str, Any]:
        existing = self.idempotency.get((scope, key))
        if existing:
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflict()
            return {"state": "exists", "record": deepcopy(existing)}
        record = {
            "scope": scope,
            "key": key,
            "request_hash": request_hash,
            "status": "in_progress",
            "response": None,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        self.idempotency[(scope, key)] = record
        return {"state": "started", "record": deepcopy(record)}

    async def idempotency_finish(
        self,
        scope: str,
        key: str,
        status: str,
        response: dict[str, Any],
    ) -> None:
        record = self.idempotency[(scope, key)]
        record["status"] = status
        record["response"] = jsonable(response)
        record["updated_at"] = utcnow()

    async def append_audit(self, event: dict[str, Any]) -> dict[str, Any]:
        self._audit_seq += 1
        row = deepcopy(event)
        row["id"] = self._audit_seq
        row.setdefault("event_id", str(uuid4()))
        row.setdefault("created_at", utcnow())
        row["input"] = jsonable(row.get("input"))
        row["output"] = jsonable(row.get("output"))
        self.audit.append(row)
        return deepcopy(row)

    async def list_audit(
        self,
        *,
        session_id: str | None = None,
        correlation_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        rows = self.audit
        if session_id:
            rows = [row for row in rows if row.get("session_id") == session_id]
        if correlation_id:
            rows = [row for row in rows if row.get("correlation_id") == correlation_id]
        return [deepcopy(row) for row in rows[-limit:]]
