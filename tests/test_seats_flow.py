from uuid import uuid4

import httpx

from test_mcp_http import gateway  # noqa: F401  (reuse the live-gateway fixture)


async def test_harbor_lights_fail_rollback_then_retry_and_idempotent_replay(gateway):  # noqa: F811
    base = f"http://127.0.0.1:{gateway}"
    session = f"seats-{uuid4().hex[:8]}"

    async with httpx.AsyncClient(timeout=30) as client:
        async def say(text):
            r = await client.post(f"{base}/api/chat", json={"session_id": session, "message": text})
            assert r.status_code == 200, r.text
            return r.json()

        found = await say("Two seats for Harbor Lights tonight")
        assert found["trace"][0]["result"]["products"][0]["price"] == "$148.00"

        quote = await say("Book them")
        assert "Nothing is charged until you confirm" in quote["reply"]
        assert quote["trace"][-1]["result"]["total"] == "$148.00"

        failed = await say("Yes, book them, but make the card capture fail")
        result = failed["trace"][-1]["result"]
        steps = {s["name"]: s["status"] for s in result["saga"]["steps"]}
        assert steps["capture_payment"] == "failed"
        assert steps["reserve_inventory"] == steps["authorize_payment"] == steps["create_order"] == "compensated"
        assert result["charges"]["captured"] == 0
        assert "seats C6–C7 released" in failed["reply"]

        booked = await say("Yes, book them")
        first = booked["trace"][-1]["result"]
        assert first["status"] == "confirmed" and first["charges"]["captured"] == 1

        replay = await say("Send the same booking request again")
        call = replay["trace"][-1]
        assert call["arguments"]["idempotency_key"] == first["idempotency_key"]
        assert call["result"]["replayed"] is True
        assert call["result"]["order_id"] == first["order_id"]
        assert call["result"]["charges"]["captured"] == 1
        assert not any(row["action"].startswith("saga.") for row in replay["ledger"])

        audit = await client.get(f"{base}/api/audit", params={"session_id": session})
        assert any(e["action"] == "saga.capture_payment.failed" for e in audit.json()["events"])
