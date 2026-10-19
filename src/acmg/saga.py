"""Small saga runner. Steps commit forward; on failure they compensate in reverse."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import uuid4


class SagaFailure(Exception):
    """A step refused to commit. The message is safe to show a shopper."""


Execute = Callable[["SagaContext"], Awaitable[dict[str, Any]]]
Compensate = Callable[["SagaContext", dict[str, Any]], Awaitable[None]]


@dataclass
class SagaStep:
    name: str
    execute: Execute
    compensate: Compensate | None = None


@dataclass
class SagaContext:
    name: str
    saga_id: str
    correlation_id: str
    session_id: str
    actor_type: str
    actor_id: str
    customer_id: str
    reason: str
    tool_name: str
    bag: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepOutcome:
    name: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "output": self.output,
            "error": self.error,
        }


@dataclass
class SagaResult:
    saga_id: str
    name: str
    status: str
    steps: list[StepOutcome]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "saga_id": self.saga_id,
            "name": self.name,
            "status": self.status,
            "error": self.error,
            "steps": [step.as_dict() for step in self.steps],
        }


class AuditLog:
    def __init__(self, store: object):
        self.store = store

    async def record(
        self,
        ctx: SagaContext | None,
        *,
        action: str,
        status: str,
        reason: str | None = None,
        tool_name: str | None = None,
        input: Any = None,
        output: Any = None,
        error: str | None = None,
        correlation_id: str | None = None,
        session_id: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        customer_id: str | None = None,
        saga_id: str | None = None,
    ) -> None:
        await self.store.append_audit(  # type: ignore[attr-defined]
            {
                "event_id": str(uuid4()),
                "correlation_id": correlation_id or (ctx.correlation_id if ctx else "none"),
                "causation_id": None,
                "saga_id": saga_id if saga_id is not None else (ctx.saga_id if ctx else None),
                "session_id": session_id or (ctx.session_id if ctx else None),
                "actor_type": actor_type or (ctx.actor_type if ctx else "system"),
                "actor_id": actor_id or (ctx.actor_id if ctx else "ledgerlane"),
                "customer_id": customer_id or (ctx.customer_id if ctx else None),
                "tool_name": tool_name if tool_name is not None else (ctx.tool_name if ctx else None),
                "action": action,
                "reason": reason if reason is not None else (ctx.reason if ctx else None),
                "status": status,
                "input": input,
                "output": output,
                "error": error,
            }
        )


class SagaEngine:
    def __init__(self, audit: AuditLog):
        self.audit = audit

    async def run(self, ctx: SagaContext, steps: list[SagaStep]) -> SagaResult:
        completed: list[tuple[SagaStep, dict[str, Any]]] = []
        outcomes: list[StepOutcome] = []
        await self.audit.record(ctx, action=f"saga.{ctx.name}.started", status="started", input={"steps": [step.name for step in steps]})
        for step in steps:
            await self.audit.record(ctx, action=f"saga.{step.name}.started", status="started")
            try:
                output = await step.execute(ctx)
            except Exception as exc:  # noqa: BLE001 - every step failure is compensable
                message = getattr(exc, "message", None) or str(exc)
                outcomes.append(StepOutcome(step.name, "failed", error=message))
                await self.audit.record(
                    ctx,
                    action=f"saga.{step.name}.failed",
                    status="failed",
                    error=message,
                )
                compensation_failed = False
                for done, done_output in reversed(completed):
                    if done.compensate is None:
                        outcomes.append(StepOutcome(done.name, "compensated", output={"note": "nothing to undo"}))
                        await self.audit.record(
                            ctx,
                            action=f"saga.{done.name}.compensated",
                            status="compensated",
                            output={"note": "nothing to undo"},
                        )
                        continue
                    try:
                        await done.compensate(ctx, done_output)
                    except Exception as comp_exc:  # noqa: BLE001
                        comp_message = getattr(comp_exc, "message", None) or str(comp_exc)
                        outcomes.append(StepOutcome(done.name, "compensation_failed", error=comp_message))
                        await self.audit.record(
                            ctx,
                            action=f"saga.{done.name}.compensation_failed",
                            status="compensation_failed",
                            error=comp_message,
                        )
                        compensation_failed = True
                        break
                    outcomes.append(StepOutcome(done.name, "compensated", output=done_output))
                    await self.audit.record(
                        ctx,
                        action=f"saga.{done.name}.compensated",
                        status="compensated",
                        output=done_output,
                    )
                status = "compensation_failed" if compensation_failed else ("compensated" if completed else "aborted")
                await self.audit.record(ctx, action=f"saga.{ctx.name}.{status}", status=status, error=message)
                return SagaResult(ctx.saga_id, ctx.name, status, outcomes, error=message)
            completed.append((step, output))
            outcomes.append(StepOutcome(step.name, "succeeded", output=output))
            await self.audit.record(ctx, action=f"saga.{step.name}.succeeded", status="succeeded", output=output)
        await self.audit.record(ctx, action=f"saga.{ctx.name}.completed", status="completed")
        return SagaResult(ctx.saga_id, ctx.name, "completed", outcomes)
