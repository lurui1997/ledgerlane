"""HTTP surface: MCP transport, demo OAuth, and the Alexa+ simulator."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib.resources import files
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from pydantic import BaseModel, Field

from acmg.config import Settings
from acmg.mcp_server import build_mcp, current_customer
from acmg.oauth_demo import DemoAuth, authorize_redirect, basic_credentials, token_matches
from acmg.payments import build_payment_provider
from acmg.postgres import PostgresStore
from acmg.saga import AuditLog
from acmg.store import MemoryStore
from acmg.commerce import build_commerce
from acmg.agent import AgentLoop

logger = logging.getLogger("ledgerlane")
STATIC_DIR = files("acmg").joinpath("static")


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None


class BearerGate:
    """401 without a WWW-Authenticate header, which Alexa+ account linking requires."""

    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.settings.auth_mode == "off":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "").rstrip("/") or "/"
        if path != "/mcp":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope.get("headers", [])}
        presented = ""
        authorization = headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
        if not token_matches(presented, self.settings.demo_token):
            body = b'{"error":"unauthorized","message":"Send Authorization: Bearer with the demo token."}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        token = current_customer.set({"customer_id": "demo-shopper", "actor_id": "ledgerlane-concierge"})
        try:
            await self.app(scope, receive, send)
        finally:
            current_customer.reset(token)


def create_app(settings: Settings | None = None) -> BearerGate:
    settings = settings or Settings()
    runtime_mcp_holder: dict = {}
    from acmg.mcp_server import Runtime

    runtime = Runtime()
    mcp = build_mcp(settings, runtime)
    mcp.streamable_http_app()
    mcp_asgi = StreamableHTTPASGIApp(mcp.session_manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.store_kind == "memory":
            store = MemoryStore()
        else:
            store = await PostgresStore.connect(settings.database_url)
        await store.seed()
        payments = build_payment_provider(
            settings.payment_provider,
            store,
            settings.paypal_client_id,
            settings.paypal_client_secret,
            settings.paypal_api_base,
        )
        commerce = build_commerce(store, payments)
        runtime.commerce = commerce
        app.state.settings = settings
        app.state.store = store
        app.state.commerce = commerce
        app.state.payments = payments
        app.state.agent = AgentLoop(settings)
        app.state.auth = DemoAuth(settings)
        logger.info("ledgerlane store=%s llm=%s payments=%s", settings.store_kind, settings.llm_provider, payments.name)
        async with mcp.session_manager.run():
            yield
        await store.close()

    app = FastAPI(title="Ledgerlane", version="0.1.0", lifespan=lifespan)
    app.mount("/mcp", mcp_asgi)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR.joinpath("index.html")))

    @app.get("/audit")
    async def audit_page() -> FileResponse:
        return FileResponse(str(STATIC_DIR.joinpath("audit.html")))

    @app.get("/health")
    async def health(request: Request) -> dict:
        commerce = getattr(request.app.state, "commerce", None)
        return {
            "status": "ok" if commerce is not None else "starting",
            "store": settings.store_kind,
            "payment_provider": settings.payment_provider,
            "llm_provider": settings.llm_provider,
            "auth": settings.auth_mode,
            "mcp": {
                "name": "Ledgerlane Commerce",
                "protocol": "2025-11-25",
                "transport": "streamable-http",
                "path": "/mcp",
                "resource": settings.mcp_resource,
            },
        }

    @app.post("/api/chat")
    async def chat(body: ChatIn, request: Request) -> dict:
        session_id = (body.session_id or str(uuid4())).strip()[:80]
        commerce = request.app.state.commerce

        async def audit_note(**kwargs):
            await AuditLog(request.app.state.store).record(None, tool_name="conversation", **kwargs)

        try:
            result = await request.app.state.agent.chat(session_id, body.message.strip(), audit_note)
        except Exception as exc:  # noqa: BLE001 - the simulator surfaces a shopper-safe line
            logger.exception("chat failed")
            raise HTTPException(status_code=502, detail="The concierge could not finish that turn.") from exc
        result["ledger"] = await commerce.list_audit(session_id=session_id, correlation_id=result["correlation_id"])
        return result

    @app.get("/api/audit")
    async def audit(request: Request, session_id: str | None = None, correlation_id: str | None = None) -> dict:
        events = await request.app.state.commerce.list_audit(session_id=session_id, correlation_id=correlation_id)
        return {"events": events}

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def protected_resource(request: Request) -> dict:
        return request.app.state.auth.protected_resource_metadata()

    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_server(request: Request) -> dict:
        return request.app.state.auth.authorization_server_metadata()

    @app.get("/oauth/authorize")
    async def authorize(request: Request):
        auth: DemoAuth = request.app.state.auth
        params = request.query_params
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        if client_id != settings.demo_client_id or not auth.redirect_allowed(redirect_uri):
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if params.get("code_challenge_method") not in {None, "", "S256"} or not params.get("code_challenge"):
            return JSONResponse({"error": "invalid_request", "error_description": "PKCE S256 is required."}, status_code=400)
        if not auth.resource_ok(params.get("resource")):
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        code = auth.issue_code(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=params["code_challenge"],
            scope=params.get("scope") or "commerce.shop",
            resource=params.get("resource") or settings.mcp_resource,
        )
        return RedirectResponse(authorize_redirect(redirect_uri, code, params.get("state")), status_code=302)

    @app.post("/oauth/token")
    async def token(request: Request):
        auth: DemoAuth = request.app.state.auth
        form = await _form(request)
        client_id = form.get("client_id", "")
        client_secret = form.get("client_secret", "")
        basic = basic_credentials(request.headers.get("authorization", ""))
        if basic:
            client_id, client_secret = basic
        if not auth.client_ok(client_id, client_secret):
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        grant = form.get("grant_type", "")
        resource = form.get("resource") or ""
        if resource and not auth.resource_ok(resource):
            return JSONResponse({"error": "invalid_target"}, status_code=400)
        try:
            if grant == "client_credentials":
                return auth.token_response(form.get("scope") or "commerce.shop commerce.refund")
            if grant == "authorization_code":
                return auth.exchange_code(
                    code=form.get("code", ""),
                    client_id=client_id,
                    redirect_uri=form.get("redirect_uri", ""),
                    code_verifier=form.get("code_verifier", ""),
                    resource=resource,
                )
        except ValueError as exc:
            return JSONResponse({"error": "invalid_grant", "error_description": str(exc)}, status_code=400)
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    runtime_mcp_holder["mcp"] = mcp
    return BearerGate(app, settings)


async def _form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        return {str(key): "" if value is None else str(value) for key, value in body.items()}
    raw = (await request.body()).decode()
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


app = create_app()
