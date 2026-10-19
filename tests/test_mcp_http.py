import socket
from uuid import uuid4

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from acmg.config import Settings
from acmg.webapp import create_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def gateway():
    port = _free_port()
    settings = Settings(
        database_url="memory",
        public_base_url=f"http://127.0.0.1:{port}",
        mcp_internal_url=f"http://127.0.0.1:{port}/mcp",
        allowed_hosts="127.0.0.1,127.0.0.1:*,localhost,localhost:*",
        allowed_origins="http://127.0.0.1:*,http://localhost:*",
        llm_provider="mock",
        payment_provider="mock",
        auth_mode="demo",
    )
    application = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning"))
    task = __import__("asyncio").create_task(server.serve())
    async with httpx.AsyncClient() as client:
        for _ in range(50):
            try:
                response = await client.get(f"http://127.0.0.1:{port}/health")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await __import__("asyncio").sleep(0.05)
        else:
            server.should_exit = True
            raise RuntimeError("gateway did not start")
    yield port
    server.should_exit = True
    await task


async def test_unauthenticated_mcp_is_401_without_www_authenticate(gateway):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"http://127.0.0.1:{gateway}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"accept": "application/json, text/event-stream", "content-type": "application/json"},
        )
    assert response.status_code == 401
    assert "www-authenticate" not in {key.lower() for key in response.headers}
    async with httpx.AsyncClient() as client:
        metadata = await client.get(f"http://127.0.0.1:{gateway}/.well-known/oauth-authorization-server")
    assert "S256" in metadata.json()["code_challenge_methods_supported"]


async def test_streamable_http_search_and_simulator_checkout(gateway):
    url = f"http://127.0.0.1:{gateway}/mcp"
    async with httpx.AsyncClient(headers={"Authorization": "Bearer demo-shopper-token"}, timeout=20) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.protocolVersion == "2025-11-25"
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert {"search_catalog", "place_order", "refund_order"} <= names
                found = await session.call_tool(
                    "search_catalog",
                    {
                        "query": "desk lamp",
                        "reason": "HTTP contract test",
                        "session_id": "mcp-test",
                        "correlation_id": str(uuid4()),
                    },
                )
                assert found.isError is False

    session_id = "sim-" + uuid4().hex[:8]
    script = [
        "Find trail running shoes under 120 dollars",
        "Compare the two cheapest",
        "Add the cheaper pair to my cart",
        "Check out",
        "Yes, place the order",
    ]
    async with httpx.AsyncClient(timeout=30) as client:
        last = None
        for line in script:
            response = await client.post(
                f"http://127.0.0.1:{gateway}/api/chat",
                json={"session_id": session_id, "message": line},
            )
            assert response.status_code == 200, response.text
            last = response.json()
        assert "confirmed" in last["reply"].lower() or last["reply"].startswith("Order ")
        actions = [event["action"] for event in last["ledger"]]
        assert "saga.capture_payment.succeeded" in actions
        assert any(event["actor_type"] == "customer" for event in last["ledger"])

        fail_session = "fail-" + uuid4().hex[:8]
        for line in [
            "Find a desk lamp",
            "Add the lamp to my cart",
            "Check out",
            "Yes, place the order, but make the card capture fail",
        ]:
            response = await client.post(
                f"http://127.0.0.1:{gateway}/api/chat",
                json={"session_id": fail_session, "message": line},
            )
            assert response.status_code == 200, response.text
            failed = response.json()
        assert "rolled" in failed["reply"].lower() or "not" in failed["reply"].lower()
        fail_actions = [event["action"] for event in failed["ledger"]]
        assert "saga.capture_payment.failed" in fail_actions
        assert "saga.reserve_inventory.compensated" in fail_actions
