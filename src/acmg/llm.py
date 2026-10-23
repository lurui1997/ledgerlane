"""Swappable planner. Mock needs no key. OpenAI-compatible covers OpenAI now and Gemini later."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx

from acmg.config import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Decision:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class MockLLM:
    """Deterministic concierge so the demo runs with the network unplugged."""

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], shop: dict[str, Any]) -> Decision:
        last = messages[-1]
        if last["role"] == "tool":
            return self._after_tool(last, shop)
        text = last.get("content") or ""
        return self._after_user(text, shop)

    def _after_user(self, text: str, shop: dict[str, Any]) -> Decision:
        lowered = text.lower().strip()
        shop["last_user"] = text
        if "capture fail" in lowered or "card capture fail" in lowered:
            shop["fail_capture"] = True
        if re.search(r"\bstart over\b|\bforget that\b", lowered):
            for key in ("products", "cart_id", "quote_id", "order_id", "awaiting", "fail_capture", "seats", "place_attempt", "last_place_args"):
                shop.pop(key, None)
            return Decision("Cleared. Tell me what you want to find.")
        seats = self._seats_turn(text, lowered, shop)
        if seats is not None:
            return seats
        if re.search(r"what can you do|help|capabilities", lowered):
            return Decision(
                "I can find something in the catalog, compare a couple of options, add one to a cart, "
                "read you the total, place the order, track it, or refund it. "
                "If a card capture fails, I undo the reservation instead of leaving a charge."
            )
        if "refund" in lowered or "return" in lowered:
            if not shop.get("order_id"):
                return Decision("There isn't a confirmed order to refund yet.")
            if re.search(r"\byes\b|\bconfirm\b|\bdo it\b", lowered):
                return self._call(
                    "refund_order",
                    {
                        "order_id": shop["order_id"],
                        "confirm": True,
                        "idempotency_key": f"refund:{shop['session_id']}:{shop['order_id']}",
                        "reason": f"Shopper confirmed a refund: {text[:180]}",
                    },
                )
            shop["awaiting"] = "refund"
            return self._call(
                "refund_order",
                {
                    "order_id": shop["order_id"],
                    "confirm": False,
                    "idempotency_key": f"refund:{shop['session_id']}:{shop['order_id']}:ask",
                    "reason": f"Shopper asked about a refund: {text[:180]}",
                },
            )
        if re.search(r"\bwhere\b|\btrack\b|\bstatus\b", lowered):
            args: dict[str, Any] = {"reason": "Shopper asked where the order is."}
            if shop.get("order_id"):
                args["order_id"] = shop["order_id"]
            return self._call("track_order", args)
        if re.search(r"\byes\b|\bplace the order\b|\bconfirm\b", lowered) and shop.get("awaiting") == "refund":
            return self._call(
                "refund_order",
                {
                    "order_id": shop["order_id"],
                    "confirm": True,
                    "idempotency_key": f"refund:{shop['session_id']}:{shop['order_id']}",
                    "reason": "Shopper confirmed the refund.",
                },
            )
        if re.search(r"\byes\b|\bplace the order\b|\bconfirm the order\b|\bplace it\b", lowered):
            if not shop.get("quote_id"):
                if shop.get("cart_id"):
                    return self._call("prepare_checkout", {"cart_id": shop["cart_id"], "reason": "Shopper agreed to buy, so price the cart first."})
                return Decision("I don't have a checkout ready. Add something, then say check out.")
            simulate = "capture_payment" if shop.get("fail_capture") or "capture fail" in lowered else None
            return self._call(
                "place_order",
                {
                    "quote_id": shop["quote_id"],
                    "confirm": True,
                    "idempotency_key": f"place:{shop['session_id']}:{shop['quote_id']}",
                    "payment_method": "mock_card",
                    "simulate_failure": simulate,
                    "reason": "Shopper explicitly confirmed the quoted total.",
                },
            )
        if "check out" in lowered or "checkout" in lowered:
            if not shop.get("cart_id"):
                return Decision("Your cart is empty. Tell me what to find first.")
            return self._call("prepare_checkout", {"cart_id": shop["cart_id"], "reason": "Shopper asked to check out."})
        if "compare" in lowered:
            products = shop.get("products") or []
            if len(products) < 2:
                return Decision("Search first, then I can compare two of the results.")
            ranked = sorted(products, key=lambda item: item["price_cents"])
            skus = [ranked[0]["sku"], ranked[1]["sku"]]
            if "first two" in lowered:
                skus = [products[0]["sku"], products[1]["sku"]]
            return self._call("compare_products", {"skus": skus, "reason": "Shopper asked to compare options."})
        if "add" in lowered and ("cart" in lowered or "pair" in lowered or "lamp" in lowered or "one" in lowered):
            sku = _pick_sku(lowered, shop.get("products") or [])
            if not sku:
                return Decision("Tell me what to find, and I'll add it after that.")
            args = {"sku": sku, "qty": 1, "reason": f"Shopper asked to add {sku} to the cart."}
            if shop.get("cart_id"):
                args["cart_id"] = shop["cart_id"]
            return self._call("add_to_cart", args)
        if re.search(r"\bfind\b|\bsearch\b|\bshow\b|\blooking\b|\blamp\b|\bshoe", lowered):
            query, max_price = _query_and_price(text)
            args = {
                "query": query or "catalog",
                "sort": "price_asc" if "cheap" in lowered else "relevance",
                "limit": 5,
                "reason": f"Shopper asked: {text[:180]}",
            }
            if max_price is not None:
                args["max_price_cents"] = max_price
            return self._call("search_catalog", args)
        return Decision("Try asking me to find trail running shoes under 120 dollars.")

    def _seats_turn(self, text: str, lowered: str, shop: dict[str, Any]) -> Decision | None:
        """Concert-seat flow. Seats are a catalog SKU, so holding them is the saga's reserve_inventory step."""
        if re.search(r"\bseats?\b|harbor lights", lowered) and not re.search(r"\bbook\b|\bsame\b", lowered):
            shop["seats"] = True
            return self._call(
                "search_catalog",
                {"query": "harbor lights seats", "limit": 3, "reason": f"Shopper asked: {text[:180]}"},
            )
        if not shop.get("seats"):
            return None
        if re.search(r"\b(send|resend)\b.*\bsame\b|\bsame (booking )?request\b", lowered):
            last = shop.get("last_place_args")
            if not last:
                return Decision("There isn't an earlier booking request to resend.")
            # Byte-for-byte the previous request, including its idempotency key.
            args = dict(last)
            args["reason"] = "Shopper resent the identical booking request; same idempotency key."
            return self._call("place_order", args)
        if re.search(r"\byes\b.*\bbook\b|\bconfirm\b", lowered):
            if not shop.get("quote_id"):
                return Decision("I don't have a checkout for the seats yet. Say book them first.")
            fail = "capture fail" in lowered
            attempt = shop.get("place_attempt", 0)
            key = f"place:{shop['session_id']}:{shop['quote_id']}" + (f":retry{attempt}" if attempt else "")
            args = {
                "quote_id": shop["quote_id"],
                "confirm": True,
                "idempotency_key": key,
                "payment_method": "mock_card",
                "simulate_failure": "capture_payment" if fail else None,
                "reason": "Shopper explicitly confirmed the quoted total for the seats.",
            }
            shop["last_place_args"] = dict(args)
            return self._call("place_order", args)
        if re.search(r"\bbook\b|check ?out", lowered):
            if shop.get("cart_id"):
                return self._call("prepare_checkout", {"cart_id": shop["cart_id"], "reason": "Shopper asked to book the seats."})
            products = shop.get("products") or []
            sku = next((p["sku"] for p in products if p.get("category") == "tickets"), products[0]["sku"] if products else None)
            if not sku:
                return Decision("Ask for the seats first.")
            shop["checkout_after_add"] = True
            return self._call("add_to_cart", {"sku": sku, "qty": 1, "reason": "Shopper asked to book the two seats."})
        return None

    def _after_tool(self, message: dict[str, Any], shop: dict[str, Any]) -> Decision:
        payload = _payload(message.get("content") or "")
        name = message.get("name") or shop.get("last_tool")
        if name == "search_catalog":
            shop["products"] = payload.get("products") or []
            return Decision(payload.get("message") or "Here's what I found.")
        if name == "compare_products":
            shop["cheapest_sku"] = payload.get("cheapest_sku")
            return Decision(payload.get("message") or "Here's the comparison.")
        if name == "add_to_cart":
            shop["cart_id"] = payload.get("cart_id")
            if shop.pop("checkout_after_add", False) and shop["cart_id"]:
                return self._call("prepare_checkout", {"cart_id": shop["cart_id"], "reason": "Seats held in cart; price them before any charge."})
            return Decision(payload.get("message") or "Added to the cart.")
        if name == "prepare_checkout":
            shop["quote_id"] = payload.get("quote_id")
            shop["awaiting"] = "place"
            return Decision(payload.get("message") or "Here is the total. Shall I place it?")
        if name == "place_order" and shop.get("seats"):
            return Decision(_seats_place_message(payload, shop))
        if name == "place_order":
            if payload.get("order_id") and payload.get("status") == "confirmed":
                shop["order_id"] = payload["order_id"]
                shop["awaiting"] = None
                shop["fail_capture"] = False
            return Decision(payload.get("message") or "The checkout finished.")
        if name == "refund_order":
            if payload.get("confirmation_required"):
                shop["awaiting"] = "refund"
            if payload.get("status") == "refunded":
                shop["awaiting"] = None
            return Decision(payload.get("message") or "Here's the refund status.")
        if name in {"track_order", "get_order"}:
            if payload.get("order_id"):
                shop["order_id"] = payload["order_id"]
            return Decision(payload.get("message") or "Here's the order.")
        if payload.get("message"):
            return Decision(payload["message"])
        return Decision("Done.")

    def _call(self, name: str, arguments: dict[str, Any]) -> Decision:
        return Decision(tool_calls=[ToolCall(id="call_" + uuid4().hex[:8], name=name, arguments=arguments)])


class OpenAICompatibleLLM:
    def __init__(self, *, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], shop: dict[str, Any]) -> Decision:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"The model provider returned {response.status_code}.")
        message = response.json()["choices"][0]["message"]
        calls = []
        for call in message.get("tool_calls") or []:
            raw = call["function"]["arguments"] or "{}"
            calls.append(ToolCall(id=call["id"], name=call["function"]["name"], arguments=json.loads(raw)))
        return Decision(content=message.get("content") or "", tool_calls=calls)


def build_llm(settings: Settings) -> MockLLM | OpenAICompatibleLLM:
    if settings.llm_provider == "mock":
        return MockLLM()
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is empty. Set it, or use LLM_PROVIDER=mock.")
        return OpenAICompatibleLLM(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.openai_model,
        )
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is empty. Set it, or use LLM_PROVIDER=mock.")
        return OpenAICompatibleLLM(
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            model=settings.gemini_model,
        )
    raise RuntimeError(f"Unknown LLM_PROVIDER {settings.llm_provider}.")


def _query_and_price(text: str) -> tuple[str, int | None]:
    match = re.search(r"(?:under|below|less than)\s+\$?(\d+)", text.lower())
    max_price = int(match.group(1)) * 100 if match else None
    query = re.sub(r"(?:under|below|less than)\s+\$?\d+\s*(dollars|bucks|usd)?", " ", text.lower())
    query = re.sub(r"\b(find|search|show|me|looking for|i want|i need|please|a|an|the|some)\b", " ", query)
    query = re.sub(r"\s+", " ", query).strip(" .")
    return query, max_price


def _pick_sku(text: str, products: list[dict[str, Any]]) -> str | None:
    if not products:
        return None
    if "cheap" in text:
        return min(products, key=lambda item: item["price_cents"])["sku"]
    skip = {"add", "the", "pair", "cart", "item", "please", "this", "that", "into", "my"}
    for product in products:
        name = product["name"].lower()
        for token in text.split():
            if token in skip or len(token) < 3:
                continue
            if token in name:
                return product["sku"]
    return products[0]["sku"]


def _payload(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {"message": content}
    return parsed if isinstance(parsed, dict) else {"message": content}


def _seats_place_message(payload: dict[str, Any], shop: dict[str, Any]) -> str:
    """Spoken reply built only from the tool result: saga steps, replay flag, and payment row counts."""
    charges = payload.get("charges") or {}
    captured = charges.get("captured")
    if payload.get("replayed"):
        return (
            f"Same request key, so the gateway replayed the first result instead of running checkout again: "
            f"order {payload.get('order_id')} is {payload.get('status')}. No new authorization or capture. "
            f"Charges on this checkout: {captured}."
        )
    if payload.get("status") == "confirmed":
        shop["order_id"] = payload.get("order_id")
        shop["awaiting"] = None
        return (
            f"Booked. Order {payload.get('order_id')} for {payload.get('total')}, seats C6\u2013C7 in Hall B. "
            f"Charges on this checkout: {captured}."
        )
    steps = (payload.get("saga") or {}).get("steps") or []
    failed = next((s["name"] for s in steps if s.get("status") == "failed"), None)
    undone = [s["name"] for s in steps if s.get("status") == "compensated"]
    shop["place_attempt"] = shop.get("place_attempt", 0) + 1
    labels = {"create_order": "order cancelled", "authorize_payment": "authorization voided", "reserve_inventory": "seats C6\u2013C7 released"}
    done = ", ".join(labels.get(n, n) for n in undone) or "nothing to undo"
    return (
        f"The card capture failed{' at ' + failed if failed else ''}, so I rolled back: {done}. "
        f"Nothing was charged (captured payments: {captured})."
    )
