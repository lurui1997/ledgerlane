"""Drive the simulator the way the demo video does. Exits non-zero on failure."""

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4317"


def chat(client: httpx.Client, session_id: str, message: str) -> dict:
    response = client.post(f"{BASE}/api/chat", json={"session_id": session_id, "message": message}, timeout=30)
    response.raise_for_status()
    return response.json()


def main() -> None:
    with httpx.Client() as client:
        health = client.get(f"{BASE}/health", timeout=10)
        health.raise_for_status()
        body = health.json()
        if body["llm_provider"] != "mock" or body["payment_provider"] != "mock":
            raise SystemExit(f"expected mock mode, got {body}")
        if body["store"] != "postgres":
            raise SystemExit(f"expected postgres, got {body['store']}")
        bare = client.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={"accept": "application/json, text/event-stream", "content-type": "application/json"},
            timeout=10,
        )
        if bare.status_code != 401 or "www-authenticate" in {key.lower() for key in bare.headers}:
            raise SystemExit(f"expected a bare 401, got {bare.status_code} {bare.headers}")

        last = None
        for line in [
            "Find trail running shoes under 120 dollars",
            "Compare the two cheapest",
            "Add the cheaper pair to my cart",
            "Check out",
            "Yes, place the order",
            "Where is my order?",
            "Refund it, they don't fit",
            "Yes, refund it",
        ]:
            last = chat(client, "e2e-happy", line)
            print(f"> {line}\n  {last['reply']}")
        if "refunded" not in last["reply"].lower():
            raise SystemExit("happy path did not refund")

        failed = None
        for line in [
            "Find a desk lamp",
            "Add the lamp to my cart",
            "Check out",
            "Yes, place the order, but make the card capture fail",
        ]:
            failed = chat(client, "e2e-fail", line)
            print(f"> {line}\n  {failed['reply']}")
        actions = [event["action"] for event in failed["ledger"]]
        if "saga.capture_payment.failed" not in actions or "saga.reserve_inventory.compensated" not in actions:
            raise SystemExit(f"compensation missing: {actions}")
    print("e2e ok")


if __name__ == "__main__":
    main()
