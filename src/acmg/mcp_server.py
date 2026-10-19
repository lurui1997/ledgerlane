"""MCP tools exposed over Streamable HTTP. Each tool calls the commerce service."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from typing import Annotated

from pydantic import Field

from acmg.config import Settings
from acmg.store import IdempotencyConflict, StoreError

current_customer: ContextVar[dict[str, str] | None] = ContextVar("current_customer", default=None)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
COMMIT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


class Runtime:
    def __init__(self) -> None:
        self.commerce: Any = None


def build_mcp(settings: Settings, runtime: Runtime) -> FastMCP:
    security = None
    if settings.allowed_hosts.strip().lower() != "off":
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.host_list(),
            allowed_origins=settings.origin_list(),
        )
    mcp = FastMCP(
        "Ledgerlane Commerce",
        instructions=(
            "Ledgerlane is a shop concierge. Search the catalog before adding anything to a cart. "
            "Read the checkout total aloud and wait for an explicit yes before place_order. "
            "Refunds also need an explicit yes. Never invent prices, SKUs, or order ids. "
            "If a tool says the checkout was rolled back, tell the shopper the card was not captured."
        ),
        host="0.0.0.0",
        port=settings.port,
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )

    def actor(tool_name: str, reason: str, session_id: str, correlation_id: str):
        from acmg.commerce import Actor

        customer = current_customer.get()
        if customer is None:
            raise StoreError("unauthorized", "A bearer token is required before shopping.")
        return Actor(
            actor_type="agent",
            actor_id=customer.get("actor_id", "ledgerlane-concierge"),
            customer_id=customer["customer_id"],
            session_id=session_id,
            reason=reason,
            correlation_id=correlation_id,
            tool_name=tool_name,
        )

    async def run(tool_name: str, reason: str, session_id: str, correlation_id: str, method: str, **kwargs: Any):
        if runtime.commerce is None:
            return fail("The shop is still starting. Try again in a moment.")
        try:
            payload = await getattr(runtime.commerce, method)(
                actor(tool_name, reason, session_id, correlation_id),
                **kwargs,
            )
        except (StoreError, IdempotencyConflict) as exc:
            return fail(exc.message, code=exc.code)
        except Exception as exc:  # noqa: BLE001 - tools must not crash the server
            return fail("Something went wrong before the shop could finish. Nothing else was changed.")
        if payload.get("ok") is False:
            extra = {key: value for key, value in payload.items() if key not in {"ok", "message"}}
            return fail(payload.get("message") or "That did not complete.", **extra)
        return ok(payload)

    @mcp.tool(
        name="search_catalog",
        description=(
            "Search the Ledgerlane catalog. Use for find, show, look up, or shop requests. "
            "Synonyms are in the catalog: sneakers and trainers mean footwear, a desk lamp is a lamp. "
            "Returns a search_id and products with stable sku values for get_product, compare_products, and add_to_cart. "
            "Sort is one of relevance, price_asc, price_desc, rating. Prices are integer cents."
        ),
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def search_catalog(
        query: Annotated[str, Field(description="What the shopper wants, in their words.")],
        reason: Annotated[str, Field(description="Why you are searching, in one sentence.")],
        session_id: Annotated[str, Field(description="Conversation id from the client.")],
        correlation_id: Annotated[str, Field(description="Id that groups every action in this turn.")],
        category: Annotated[str | None, Field(description="Optional: footwear, apparel, home, kitchen, accessories.")] = None,
        max_price_cents: Annotated[int | None, Field(description="Optional maximum unit price in cents. 120 dollars is 12000.")] = None,
        sort: Annotated[str, Field(description="relevance, price_asc, price_desc, or rating.")] = "relevance",
        limit: Annotated[int, Field(description="How many products to return, 1 to 8.")] = 5,
    ) -> CallToolResult:
        return await run(
            "search_catalog",
            reason,
            session_id,
            correlation_id,
            "search_catalog",
            query=query,
            category=category,
            max_price_cents=max_price_cents,
            sort=sort,
            limit=limit,
        )

    @mcp.tool(
        name="get_product",
        description="Fetch one product by the sku returned from search_catalog.",
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def get_product(
        sku: Annotated[str, Field(description="Stable product sku, for example NL-RIDGE-7.")],
        reason: Annotated[str, Field(description="Why you need this product.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
    ) -> CallToolResult:
        return await run("get_product", reason, session_id, correlation_id, "get_product", sku=sku)

    @mcp.tool(
        name="compare_products",
        description=(
            "Compare two to four products by sku. Returns the cheapest sku and a recommended sku "
            "(higher rating, then lower price) so the next call can add_to_cart."
        ),
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def compare_products(
        skus: Annotated[list[str], Field(description="Product skus from an earlier search.")],
        reason: Annotated[str, Field(description="Why you are comparing these.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
    ) -> CallToolResult:
        return await run("compare_products", reason, session_id, correlation_id, "compare_products", skus=skus)

    @mcp.tool(
        name="add_to_cart",
        description="Add a sku to the shopper's cart. Omit cart_id to start a cart. Returns cart_id for later calls.",
        annotations=MUTATING,
        structured_output=False,
    )
    async def add_to_cart(
        sku: Annotated[str, Field(description="Product sku to add.")],
        reason: Annotated[str, Field(description="Why this item is being added.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
        qty: Annotated[int, Field(description="How many to add, 1 to 10.")] = 1,
        cart_id: Annotated[str | None, Field(description="Existing cart id. Omit to create one.")] = None,
    ) -> CallToolResult:
        return await run(
            "add_to_cart",
            reason,
            session_id,
            correlation_id,
            "add_to_cart",
            sku=sku,
            qty=qty,
            cart_id=cart_id,
        )

    @mcp.tool(
        name="update_cart",
        description="Change a cart line. Quantity 0 removes the sku. Other lines stay as they are.",
        annotations=MUTATING,
        structured_output=False,
    )
    async def update_cart(
        cart_id: Annotated[str, Field(description="Cart id from add_to_cart.")],
        sku: Annotated[str, Field(description="Sku to change.")],
        qty: Annotated[int, Field(description="New quantity, 0 to 10.")],
        reason: Annotated[str, Field(description="Why the cart is changing.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
    ) -> CallToolResult:
        return await run(
            "update_cart",
            reason,
            session_id,
            correlation_id,
            "update_cart",
            cart_id=cart_id,
            sku=sku,
            qty=qty,
        )

    @mcp.tool(
        name="view_cart",
        description="Read the current cart by cart_id.",
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def view_cart(
        cart_id: Annotated[str, Field(description="Cart id.")],
        reason: Annotated[str, Field(description="Why you are reading the cart.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
    ) -> CallToolResult:
        return await run("view_cart", reason, session_id, correlation_id, "view_cart", cart_id=cart_id)

    @mcp.tool(
        name="prepare_checkout",
        description=(
            "Price a cart. Returns quote_id, shipping, and the total to read back. "
            "Does not charge. The quote expires in 15 minutes. Pass quote_id to place_order after the shopper says yes."
        ),
        annotations=MUTATING,
        structured_output=False,
    )
    async def prepare_checkout(
        cart_id: Annotated[str, Field(description="Cart to price.")],
        reason: Annotated[str, Field(description="Why you are preparing checkout.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
    ) -> CallToolResult:
        return await run("prepare_checkout", reason, session_id, correlation_id, "prepare_checkout", cart_id=cart_id)

    @mcp.tool(
        name="place_order",
        description=(
            "Place an order for a quote_id. Requires confirm=true only after the shopper explicitly agrees to the total. "
            "If confirm is false, nothing is charged and the tool asks you to confirm. "
            "Reuse the same idempotency_key when retrying the identical request so the card cannot be captured twice. "
            "simulate_failure is a demo hook: omit it unless the shopper asked to simulate a failure. "
            "Values: reserve_inventory, authorize_payment, create_order, capture_payment, open_shipment."
        ),
        annotations=COMMIT,
        structured_output=False,
    )
    async def place_order(
        quote_id: Annotated[str, Field(description="Quote id from prepare_checkout.")],
        confirm: Annotated[bool, Field(description="True only after an explicit yes.")],
        idempotency_key: Annotated[str, Field(description="Stable key for this exact checkout attempt.")],
        reason: Annotated[str, Field(description="Why the order is being placed.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
        payment_method: Annotated[str, Field(description="mock_card or declined_card.")] = "mock_card",
        simulate_failure: Annotated[str | None, Field(description="Demo-only step name to fail, or null.")] = None,
    ) -> CallToolResult:
        return await run(
            "place_order",
            reason,
            session_id,
            correlation_id,
            "place_order",
            quote_id=quote_id,
            confirm=confirm,
            idempotency_key=idempotency_key,
            payment_method=payment_method,
            simulate_failure=simulate_failure,
        )

    @mcp.tool(
        name="get_order",
        description="Read one order by order_id, including status and tracking.",
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def get_order(
        order_id: Annotated[str, Field(description="Order id such as LL-ABC123.")],
        reason: Annotated[str, Field(description="Why you are reading the order.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
    ) -> CallToolResult:
        return await run("get_order", reason, session_id, correlation_id, "get_order", order_id=order_id)

    @mcp.tool(
        name="track_order",
        description="Track an order. Pass order_id, or omit it to track the shopper's latest order.",
        annotations=READ_ONLY,
        structured_output=False,
    )
    async def track_order(
        reason: Annotated[str, Field(description="Why you are tracking.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
        order_id: Annotated[str | None, Field(description="Order id. Omit for the latest order.")] = None,
    ) -> CallToolResult:
        return await run("track_order", reason, session_id, correlation_id, "track_order", order_id=order_id)

    @mcp.tool(
        name="refund_order",
        description=(
            "Refund a confirmed order and restock the items. Requires confirm=true only after the shopper agrees. "
            "Reuse idempotency_key for an identical retry. "
            "simulate_failure demo values: refund_payment, restock_items, mark_refunded."
        ),
        annotations=COMMIT,
        structured_output=False,
    )
    async def refund_order(
        order_id: Annotated[str, Field(description="Confirmed order id.")],
        confirm: Annotated[bool, Field(description="True only after an explicit yes.")],
        idempotency_key: Annotated[str, Field(description="Stable key for this refund attempt.")],
        reason: Annotated[str, Field(description="Why the shopper wants the refund.")],
        session_id: Annotated[str, Field(description="Conversation id.")],
        correlation_id: Annotated[str, Field(description="Turn id.")],
        simulate_failure: Annotated[str | None, Field(description="Demo-only step name to fail, or null.")] = None,
    ) -> CallToolResult:
        return await run(
            "refund_order",
            reason,
            session_id,
            correlation_id,
            "refund_order",
            order_id=order_id,
            confirm=confirm,
            idempotency_key=idempotency_key,
            simulate_failure=simulate_failure,
        )

    return mcp


def ok(payload: dict[str, Any]) -> CallToolResult:
    body = {"ok": True, **payload}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(body))],
        structuredContent=body,
        isError=False,
    )


def fail(message: str, **extra: Any) -> CallToolResult:
    body = {"ok": False, "message": message, **extra}
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structuredContent=body,
        isError=True,
    )
