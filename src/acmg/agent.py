"""Agent loop. The planner chooses tools; every tool call goes out over MCP."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from acmg.config import Settings
from acmg.llm import Decision, ToolCall, build_llm

SYSTEM_PROMPT = """You are Ledgerlane, the shopping concierge in an Alexa+ web simulation.
Use the commerce tools for every fact about products, prices, carts, and orders. Never invent them.
Before place_order or a confirmed refund_order, ask the shopper to confirm the amount in plain speech, then call the tool with confirm=true only after they agree.
Speak in short sentences a person could hear aloud. Do not mention tool names, JSON, saga steps, or internal ids except the order id and tracking number the tool returned.
If a tool fails or says the checkout was rolled back, apologize and offer one next step. Say clearly that the card was not captured when that is what the tool reported.
Always include reason, session_id, and correlation_id. The server rejects a missing bearer token; you do not handle auth yourself.
"""


class AgentLoop:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = build_llm(settings)
        self.sessions: dict[str, dict[str, Any]] = {}

    async def chat(self, session_id: str, user_text: str, audit_note) -> dict[str, Any]:
        state = self.sessions.setdefault(
            session_id,
            {"messages": [{"role": "system", "content": SYSTEM_PROMPT}], "shop": {"session_id": session_id}},
        )
        correlation_id = str(uuid4())
        state["shop"]["correlation_id"] = correlation_id
        state["messages"].append({"role": "user", "content": user_text})
        await audit_note(
            correlation_id=correlation_id,
            session_id=session_id,
            actor_type="customer",
            actor_id="demo-shopper",
            action="conversation.user_message",
            reason=user_text,
            status="succeeded",
        )
        trace: list[dict[str, Any]] = []
        reply = "I didn't catch that."
        headers = {"Authorization": f"Bearer {self.settings.demo_token}"}
        async with httpx.AsyncClient(headers=headers, timeout=30) as http_client:
            async with streamable_http_client(self.settings.mcp_internal_url, http_client=http_client) as (read, write, _session):
                async with ClientSession(read, write) as mcp:
                    await mcp.initialize()
                    listed = await mcp.list_tools()
                    openai_tools = [_openai_tool(tool) for tool in listed.tools]
                    for _ in range(6):
                        decision: Decision = await self.llm.complete(state["messages"], openai_tools, state["shop"])
                        if not decision.tool_calls:
                            reply = decision.content or "Okay."
                            state["messages"].append({"role": "assistant", "content": reply})
                            break
                        state["messages"].append(
                            {
                                "role": "assistant",
                                "content": decision.content or "",
                                "tool_calls": [
                                    {
                                        "id": call.id,
                                        "type": "function",
                                        "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                                    }
                                    for call in decision.tool_calls
                                ],
                            }
                        )
                        for call in decision.tool_calls:
                            arguments = _enrich(call, session_id, correlation_id, user_text)
                            result = await mcp.call_tool(call.name, arguments)
                            text = _result_text(result)
                            state["shop"]["last_tool"] = call.name
                            state["messages"].append(
                                {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": text}
                            )
                            trace.append(
                                {
                                    "tool": call.name,
                                    "arguments": {key: value for key, value in arguments.items() if key != "reason"},
                                    "reason": arguments.get("reason"),
                                    "is_error": bool(result.isError),
                                    "result": _result_json(text),
                                }
                            )
                    else:
                        reply = "I got stuck in the tools. Ask me again in a shorter way."
                        state["messages"].append({"role": "assistant", "content": reply})
        await audit_note(
            correlation_id=correlation_id,
            session_id=session_id,
            actor_type="agent",
            actor_id="ledgerlane-concierge",
            action="conversation.assistant_message",
            reason="Spoke the result of the tool calls for this turn.",
            status="succeeded",
            output={"reply": reply, "tools": [item["tool"] for item in trace]},
        )
        return {"session_id": session_id, "correlation_id": correlation_id, "reply": reply, "trace": trace}


def _enrich(call: ToolCall, session_id: str, correlation_id: str, user_text: str) -> dict[str, Any]:
    arguments = dict(call.arguments)
    arguments.setdefault("session_id", session_id)
    arguments.setdefault("correlation_id", correlation_id)
    arguments.setdefault("reason", f"Shopper said: {user_text[:240]}")
    return arguments


def _openai_tool(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


def _result_text(result: Any) -> str:
    if getattr(result, "structuredContent", None):
        return json.dumps(result.structuredContent)
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) or "{}"


def _result_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"message": text}
