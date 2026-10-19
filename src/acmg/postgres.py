"""Postgres-backed store. The audit log here is the system of record."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from importlib.resources import files
from typing import Any

import asyncpg

logger = logging.getLogger("ledgerlane.postgres")

from acmg.catalog import PRODUCTS
from acmg.store import IdempotencyConflict, InsufficientStock, StoreError, jsonable, new_id, utcnow


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buffer.append(line)
    for statement in "\n".join(buffer).split(";"):
        if statement.strip():
            statements.append(statement.strip())
    return statements


class PostgresStore:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, database_url: str) -> "PostgresStore":
        last_error: Exception | None = None
        for attempt in range(1, 21):
            try:
                pool = await asyncio.wait_for(
                    asyncpg.create_pool(database_url, min_size=1, max_size=5),
                    timeout=5,
                )
                store = cls(pool)
                await store._migrate()
                return store
            except Exception as exc:  # noqa: BLE001 - startup retry until Postgres is ready
                last_error = exc
                logger.warning("postgres not ready (attempt %s): %s", attempt, exc)
                await asyncio.sleep(1)
        raise RuntimeError(f"Could not connect to Postgres: {last_error}")

    async def _migrate(self) -> None:
        script = files("acmg").joinpath("schema.sql").read_text(encoding="utf-8")
        async with self.pool.acquire() as conn:
            for statement in _split_sql(script):
                await conn.execute(statement)

    async def close(self) -> None:
        await self.pool.close()

    async def seed(self) -> None:
        async with self.pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM products")
            if count:
                return
            async with conn.transaction():
                for product in PRODUCTS:
                    await conn.execute(
                        """
                        INSERT INTO products (
                          sku, name, brand, category, description, price_cents,
                          currency, stock, rating, attributes, synonyms
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
                        """,
                        product["sku"],
                        product["name"],
                        product["brand"],
                        product["category"],
                        product["description"],
                        product["price_cents"],
                        product["currency"],
                        product["stock"],
                        product["rating"],
                        json.dumps(product["attributes"]),
                        product["synonyms"],
                    )

    async def search_products(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM products ORDER BY sku")
        return [_product(row) for row in rows]

    async def get_product(self, sku: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM products WHERE sku = $1", sku)
        return _product(row) if row else None

    async def create_cart(self, customer_id: str) -> dict[str, Any]:
        cart_id = new_id("C-")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO carts (id, customer_id) VALUES ($1, $2)",
                cart_id,
                customer_id,
            )
        return await self.get_cart(cart_id)

    async def get_cart(self, cart_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            cart = await conn.fetchrow("SELECT * FROM carts WHERE id = $1", cart_id)
            if cart is None:
                return None
            items = await conn.fetch(
                "SELECT sku, qty, unit_price_cents, name FROM cart_items WHERE cart_id = $1 ORDER BY sku",
                cart_id,
            )
        return {
            "id": cart["id"],
            "customer_id": cart["customer_id"],
            "currency": cart["currency"],
            "items": [dict(item) for item in items],
            "created_at": cart["created_at"],
            "updated_at": cart["updated_at"],
        }

    async def save_cart_item(
        self,
        cart_id: str,
        sku: str,
        qty: int,
        unit_price_cents: int,
        name: str,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            if qty <= 0:
                await conn.execute(
                    "DELETE FROM cart_items WHERE cart_id = $1 AND sku = $2",
                    cart_id,
                    sku,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO cart_items (cart_id, sku, qty, unit_price_cents, name)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (cart_id, sku) DO UPDATE
                    SET qty = EXCLUDED.qty, unit_price_cents = EXCLUDED.unit_price_cents, name = EXCLUDED.name
                    """,
                    cart_id,
                    sku,
                    qty,
                    unit_price_cents,
                    name,
                )
            await conn.execute("UPDATE carts SET updated_at = now() WHERE id = $1", cart_id)
        cart = await self.get_cart(cart_id)
        assert cart is not None
        return cart

    async def save_quote(self, quote: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO quotes (
                  id, cart_id, customer_id, currency, subtotal_cents, shipping_cents,
                  total_cents, items, status, expires_at, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11)
                """,
                quote["id"],
                quote["cart_id"],
                quote["customer_id"],
                quote["currency"],
                quote["subtotal_cents"],
                quote["shipping_cents"],
                quote["total_cents"],
                json.dumps(quote["items"]),
                quote["status"],
                quote["expires_at"],
                quote["created_at"],
            )
        return await self._must_quote(quote["id"])

    async def get_quote(self, quote_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM quotes WHERE id = $1", quote_id)
        return _quote(row) if row else None

    async def _must_quote(self, quote_id: str) -> dict[str, Any]:
        quote = await self.get_quote(quote_id)
        assert quote is not None
        return quote

    async def claim_quote_and_reserve(self, quote_id: str, items: list[dict[str, Any]]) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                claimed = await conn.fetchrow(
                    """
                    UPDATE quotes
                    SET status = 'consumed'
                    WHERE id = $1 AND status = 'open' AND expires_at > now()
                    RETURNING id
                    """,
                    quote_id,
                )
                if claimed is None:
                    raise StoreError("quote_unavailable", "That checkout is no longer open.")
                for item in items:
                    updated = await conn.fetchrow(
                        """
                        UPDATE products
                        SET stock = stock - $2
                        WHERE sku = $1 AND stock >= $2
                        RETURNING sku
                        """,
                        item["sku"],
                        item["qty"],
                    )
                    if updated is None:
                        raise InsufficientStock(item["sku"])

    async def release_reservation(self, quote_id: str, items: list[dict[str, Any]]) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE quotes SET status = 'open'
                    WHERE id = $1 AND status = 'consumed'
                    """,
                    quote_id,
                )
                for item in items:
                    await conn.execute(
                        "UPDATE products SET stock = stock + $2 WHERE sku = $1",
                        item["sku"],
                        item["qty"],
                    )

    async def create_order(self, order: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orders (
                  id, customer_id, quote_id, status, currency, subtotal_cents, shipping_cents,
                  total_cents, items, payment_authorization_id, payment_capture_id, payment_method,
                  tracking_number, shipment_status, idempotency_key
                ) VALUES (
                  $1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14,$15
                )
                """,
                order["id"],
                order["customer_id"],
                order["quote_id"],
                order["status"],
                order["currency"],
                order["subtotal_cents"],
                order["shipping_cents"],
                order["total_cents"],
                json.dumps(order["items"]),
                order.get("payment_authorization_id"),
                order.get("payment_capture_id"),
                order.get("payment_method"),
                order.get("tracking_number"),
                order.get("shipment_status"),
                order.get("idempotency_key"),
            )
        created = await self.get_order(order["id"])
        assert created is not None
        return created

    async def update_order(self, order_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "payment_authorization_id",
            "payment_capture_id",
            "tracking_number",
            "shipment_status",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise StoreError("bad_field", f"Cannot update {key}.")
            values.append(value)
            sets.append(f"{key} = ${len(values)}")
        values.append(order_id)
        sql = f"UPDATE orders SET {', '.join(sets)}, updated_at = now() WHERE id = ${len(values)}"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, *values)
        order = await self.get_order(order_id)
        assert order is not None
        return order

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        return _order(row) if row else None

    async def latest_order(self, customer_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT * FROM orders
                WHERE customer_id = $1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                customer_id,
            )
        return _order(row) if row else None

    async def insert_payment(self, payment: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO payments (
                  authorization_id, capture_id, idempotency_key, customer_id, amount_cents,
                  currency, status, payment_method, provider
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                payment["authorization_id"],
                payment.get("capture_id"),
                payment["idempotency_key"],
                payment["customer_id"],
                payment["amount_cents"],
                payment["currency"],
                payment["status"],
                payment["payment_method"],
                payment["provider"],
            )
        stored = await self.get_payment(payment["authorization_id"])
        assert stored is not None
        return stored

    async def get_payment(self, authorization_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM payments WHERE authorization_id = $1",
                authorization_id,
            )
        return _payment(row) if row else None

    async def get_payment_by_capture(self, capture_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM payments WHERE capture_id = $1", capture_id)
        return _payment(row) if row else None

    async def get_payment_by_idempotency(self, key: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM payments WHERE idempotency_key = $1", key)
        return _payment(row) if row else None

    async def save_payment(self, payment: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE payments
                SET capture_id = $2, status = $3, updated_at = now()
                WHERE authorization_id = $1
                """,
                payment["authorization_id"],
                payment.get("capture_id"),
                payment["status"],
            )
        stored = await self.get_payment(payment["authorization_id"])
        assert stored is not None
        return stored

    async def idempotency_begin(self, scope: str, key: str, request_hash: str) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchrow(
                    "SELECT * FROM idempotency_keys WHERE scope = $1 AND key = $2 FOR UPDATE",
                    scope,
                    key,
                )
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise IdempotencyConflict()
                    return {"state": "exists", "record": _idem(existing)}
                await conn.execute(
                    """
                    INSERT INTO idempotency_keys (scope, key, request_hash, status)
                    VALUES ($1, $2, $3, 'in_progress')
                    """,
                    scope,
                    key,
                    request_hash,
                )
                created = await conn.fetchrow(
                    "SELECT * FROM idempotency_keys WHERE scope = $1 AND key = $2",
                    scope,
                    key,
                )
        return {"state": "started", "record": _idem(created)}

    async def idempotency_finish(
        self,
        scope: str,
        key: str,
        status: str,
        response: dict[str, Any],
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE idempotency_keys
                SET status = $3, response = $4::jsonb, updated_at = now()
                WHERE scope = $1 AND key = $2
                """,
                scope,
                key,
                status,
                json.dumps(jsonable(response)),
            )

    async def append_audit(self, event: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO audit_events (
                  event_id, correlation_id, causation_id, saga_id, session_id,
                  actor_type, actor_id, customer_id, tool_name, action, reason,
                  status, input, output, error
                ) VALUES (
                  $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb,$15
                )
                RETURNING *
                """,
                event["event_id"],
                event["correlation_id"],
                event.get("causation_id"),
                event.get("saga_id"),
                event.get("session_id"),
                event["actor_type"],
                event["actor_id"],
                event.get("customer_id"),
                event.get("tool_name"),
                event["action"],
                event.get("reason"),
                event["status"],
                json.dumps(jsonable(event.get("input"))),
                json.dumps(jsonable(event.get("output"))),
                event.get("error"),
            )
        return _audit(row)

    async def list_audit(
        self,
        *,
        session_id: str | None = None,
        correlation_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["TRUE"]
        values: list[Any] = []
        if session_id:
            values.append(session_id)
            clauses.append(f"session_id = ${len(values)}")
        if correlation_id:
            values.append(correlation_id)
            clauses.append(f"correlation_id = ${len(values)}")
        values.append(limit)
        sql = f"""
            SELECT * FROM (
              SELECT * FROM audit_events
              WHERE {' AND '.join(clauses)}
              ORDER BY id DESC
              LIMIT ${len(values)}
            ) recent
            ORDER BY id ASC
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *values)
        return [_audit(row) for row in rows]


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _product(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "sku": row["sku"],
        "name": row["name"],
        "brand": row["brand"],
        "category": row["category"],
        "description": row["description"],
        "price_cents": row["price_cents"],
        "currency": row["currency"],
        "stock": row["stock"],
        "rating": float(row["rating"]),
        "attributes": _loads(row["attributes"]) or {},
        "synonyms": row["synonyms"],
    }


def _quote(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "cart_id": row["cart_id"],
        "customer_id": row["customer_id"],
        "currency": row["currency"],
        "subtotal_cents": row["subtotal_cents"],
        "shipping_cents": row["shipping_cents"],
        "total_cents": row["total_cents"],
        "items": _loads(row["items"]),
        "status": row["status"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
    }


def _order(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "customer_id": row["customer_id"],
        "quote_id": row["quote_id"],
        "status": row["status"],
        "currency": row["currency"],
        "subtotal_cents": row["subtotal_cents"],
        "shipping_cents": row["shipping_cents"],
        "total_cents": row["total_cents"],
        "items": _loads(row["items"]),
        "payment_authorization_id": row["payment_authorization_id"],
        "payment_capture_id": row["payment_capture_id"],
        "payment_method": row["payment_method"],
        "tracking_number": row["tracking_number"],
        "shipment_status": row["shipment_status"],
        "idempotency_key": row["idempotency_key"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _payment(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "authorization_id": row["authorization_id"],
        "capture_id": row["capture_id"],
        "idempotency_key": row["idempotency_key"],
        "customer_id": row["customer_id"],
        "amount_cents": row["amount_cents"],
        "currency": row["currency"],
        "status": row["status"],
        "payment_method": row["payment_method"],
        "provider": row["provider"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _idem(row: asyncpg.Record | None) -> dict[str, Any]:
    assert row is not None
    response = row["response"]
    if isinstance(response, str):
        response = json.loads(response)
    return {
        "scope": row["scope"],
        "key": row["key"],
        "request_hash": row["request_hash"],
        "status": row["status"],
        "response": response,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _audit(row: asyncpg.Record) -> dict[str, Any]:
    created: datetime = row["created_at"]
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "correlation_id": row["correlation_id"],
        "causation_id": row["causation_id"],
        "saga_id": row["saga_id"],
        "session_id": row["session_id"],
        "actor_type": row["actor_type"],
        "actor_id": row["actor_id"],
        "customer_id": row["customer_id"],
        "tool_name": row["tool_name"],
        "action": row["action"],
        "reason": row["reason"],
        "status": row["status"],
        "input": _loads(row["input"]),
        "output": _loads(row["output"]),
        "error": row["error"],
        "created_at": created,
    }
