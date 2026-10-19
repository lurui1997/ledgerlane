"""Payment provider port. Mock is live. PayPal sandbox is a typed seam for November."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Protocol

from acmg.store import StoreError, utcnow


class PaymentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ProviderNotConfigured(PaymentError):
    def __init__(self, message: str):
        super().__init__("provider_not_configured", message)


@dataclass
class AuthorizeRequest:
    amount_cents: int
    currency: str
    customer_id: str
    order_ref: str
    idempotency_key: str
    payment_method: str


@dataclass
class AuthorizeResult:
    authorization_id: str
    status: str


@dataclass
class CaptureResult:
    authorization_id: str
    capture_id: str
    status: str


@dataclass
class RefundResult:
    capture_id: str
    refund_id: str
    status: str


class PaymentProvider(Protocol):
    name: str

    async def authorize(self, request: AuthorizeRequest) -> AuthorizeResult: ...

    async def capture(self, authorization_id: str, idempotency_key: str) -> CaptureResult: ...

    async def void(self, authorization_id: str, idempotency_key: str) -> None: ...

    async def refund(
        self,
        capture_id: str,
        amount_cents: int,
        idempotency_key: str,
        reason: str,
    ) -> RefundResult: ...


class MockPaymentProvider:
    """In-ledger card stand-in. State lives in the same store as the order, so a restart can still refund."""

    name = "mock"

    def __init__(self, store: object):
        self.store = store
        self.authorize_calls = 0
        self.capture_calls = 0
        self.void_calls = 0
        self.refund_calls = 0

    async def authorize(self, request: AuthorizeRequest) -> AuthorizeResult:
        self.authorize_calls += 1
        if request.payment_method == "declined_card":
            raise PaymentError("declined", "The card was declined. Nothing was charged.")
        existing = await self.store.get_payment_by_idempotency(request.idempotency_key)  # type: ignore[attr-defined]
        if existing:
            return AuthorizeResult(existing["authorization_id"], existing["status"])
        authorization_id = "auth_" + secrets.token_hex(6)
        await self.store.insert_payment(  # type: ignore[attr-defined]
            {
                "authorization_id": authorization_id,
                "capture_id": None,
                "idempotency_key": request.idempotency_key,
                "customer_id": request.customer_id,
                "amount_cents": request.amount_cents,
                "currency": request.currency,
                "status": "authorized",
                "payment_method": request.payment_method,
                "provider": self.name,
                "created_at": utcnow(),
                "updated_at": utcnow(),
            }
        )
        return AuthorizeResult(authorization_id, "authorized")

    async def capture(self, authorization_id: str, idempotency_key: str) -> CaptureResult:
        self.capture_calls += 1
        payment = await self._require(authorization_id)
        if payment["status"] == "captured":
            return CaptureResult(authorization_id, payment["capture_id"], "captured")
        if payment["status"] != "authorized":
            raise PaymentError("capture_conflict", "That authorization cannot be captured.")
        payment["status"] = "captured"
        payment["capture_id"] = "cap_" + secrets.token_hex(6)
        await self.store.save_payment(payment)  # type: ignore[attr-defined]
        return CaptureResult(authorization_id, payment["capture_id"], "captured")

    async def void(self, authorization_id: str, idempotency_key: str) -> None:
        self.void_calls += 1
        payment = await self._require(authorization_id)
        if payment["status"] in {"captured", "refunded", "voided"}:
            return
        if payment["status"] != "authorized":
            raise PaymentError("void_conflict", "That authorization cannot be voided.")
        payment["status"] = "voided"
        await self.store.save_payment(payment)  # type: ignore[attr-defined]

    async def refund(
        self,
        capture_id: str,
        amount_cents: int,
        idempotency_key: str,
        reason: str,
    ) -> RefundResult:
        self.refund_calls += 1
        payment = await self.store.get_payment_by_capture(capture_id)  # type: ignore[attr-defined]
        if payment is None:
            raise PaymentError("missing_capture", "I can't find that charge to refund.")
        if payment["status"] == "refunded":
            return RefundResult(capture_id, "refund_" + payment["authorization_id"], "refunded")
        if payment["status"] != "captured":
            raise PaymentError("refund_conflict", "That charge is not in a refundable state.")
        if amount_cents != payment["amount_cents"]:
            raise PaymentError("partial_refund", "This demo refunds the full charge only.")
        payment["status"] = "refunded"
        await self.store.save_payment(payment)  # type: ignore[attr-defined]
        return RefundResult(capture_id, "refund_" + payment["authorization_id"], "refunded")

    async def _require(self, authorization_id: str) -> dict:
        payment = await self.store.get_payment(authorization_id)  # type: ignore[attr-defined]
        if payment is None:
            raise PaymentError("missing_authorization", "I can't find that card authorization.")
        return payment


class PayPalSandboxProvider:
    """Seam for the PayPal AI Hackathon. Not selected unless PAYMENT_PROVIDER=paypal.

    Planned Orders v2 mapping, left unimplemented on purpose so that hackathon
    can show a real delta during its own submission window:

    - authorize: POST {api}/v2/checkout/orders intent=AUTHORIZE, then
      POST /v2/checkout/orders/{id}/authorize
    - capture: POST /v2/payments/authorizations/{id}/capture
    - void: POST /v2/payments/authorizations/{id}/void
    - refund: POST /v2/payments/captures/{id}/refund

    OAuth: POST {api}/v1/oauth2/token grant_type=client_credentials
    using PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.
    """

    name = "paypal_sandbox"

    def __init__(self, client_id: str, client_secret: str, api_base: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base = api_base.rstrip("/")
        if not client_id or not client_secret:
            raise ProviderNotConfigured(
                "PayPal sandbox needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET. "
                "The adapter itself is still a seam; use PAYMENT_PROVIDER=mock for this build."
            )

    async def authorize(self, request: AuthorizeRequest) -> AuthorizeResult:
        raise ProviderNotConfigured(
            "PayPal sandbox calls are not wired yet. Keep PAYMENT_PROVIDER=mock until the November update."
        )

    async def capture(self, authorization_id: str, idempotency_key: str) -> CaptureResult:
        raise ProviderNotConfigured("PayPal capture is not wired yet.")

    async def void(self, authorization_id: str, idempotency_key: str) -> None:
        raise ProviderNotConfigured("PayPal void is not wired yet.")

    async def refund(
        self,
        capture_id: str,
        amount_cents: int,
        idempotency_key: str,
        reason: str,
    ) -> RefundResult:
        raise ProviderNotConfigured("PayPal refund is not wired yet.")


def build_payment_provider(provider_name: str, store: object, paypal_client_id: str, paypal_client_secret: str, paypal_api_base: str) -> PaymentProvider:
    if provider_name == "mock":
        return MockPaymentProvider(store)
    if provider_name == "paypal":
        return PayPalSandboxProvider(paypal_client_id, paypal_client_secret, paypal_api_base)
    raise StoreError("unknown_provider", f"Unknown payment provider {provider_name}.")
