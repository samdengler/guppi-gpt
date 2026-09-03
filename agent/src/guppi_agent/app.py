"""GuppiGPT agent on the AgentCore Runtime AG-UI contract.

POST /invocations takes an AG-UI run input and streams AG-UI events as server-sent events;
GET /ping reports health. Per run: read the caller's bearer token, validate and trim the
thread, hand it to the Strands agent (agent.py) with the token forwarded to the tools
gateway, and keep the stream alive with ping events while the model or a tool is silent.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator

from ag_ui.core import (
    BaseEvent,
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunStartedEvent,
)
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace as otel_trace

from guppi_agent import agent as agent_module
from guppi_agent.keepalive import DEFAULT_PING_INTERVAL, with_keepalive
from guppi_agent.validation import trim_messages, validate_run

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("guppi_agent")

app = FastAPI(title="guppi-agent")

SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"
# W3C trace context, minted by the page per run and passed through by the edge gateway
# target and the runtime allowlists. The runtime's own request id header, when it
# forwards one, is recorded beside it (docs/proposals/traceability.md).
TRACE_HEADER = "traceparent"
REQUEST_ID_HEADER = "x-amzn-requestid"
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


def trace_id(request: Request) -> str:
    """The 32 hex character trace id for this run, or "-" when there is none.

    The traceparent header is the source of truth, since it is what the page minted and
    what every hop before this one logged. When the container runs under
    opentelemetry-instrument the active server span carries the same id, and that span
    is the fallback for a request that arrived without the header.
    """
    match = TRACEPARENT.match(request.headers.get(TRACE_HEADER, "").strip().lower())
    if match:
        return match.group(1)
    context = otel_trace.get_current_span().get_span_context()
    if context.is_valid:
        return f"{context.trace_id:032x}"
    return "-"


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def subject_hash(token: str) -> str:
    """First 12 hex characters of the hashed sub claim; the token is not verified here.

    The runtime validated the token before the request reached the container.
    """
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        sub = str(claims.get("sub", ""))
    except Exception:
        sub = ""
    if not sub:
        return "unknown"
    return hashlib.sha256(sub.encode()).hexdigest()[:12]


def started_then_error(run: RunAgentInput, message: str, code: str) -> list[BaseEvent]:
    return [
        RunStartedEvent(type=EventType.RUN_STARTED, thread_id=run.thread_id, run_id=run.run_id),
        RunErrorEvent(type=EventType.RUN_ERROR, message=message, code=code),
    ]


async def run_agent(run: RunAgentInput, token: str, record: dict) -> AsyncIterator[BaseEvent]:
    """Produce the AG-UI events for one run and fill in the log record as they pass."""
    reason = validate_run(run)
    if reason is not None:
        record["outcome"] = "error"
        for event in started_then_error(run, reason, "BAD_INPUT"):
            yield event
        return

    messages = trim_messages(run.messages)
    record["messages"] = len(messages)
    run = run.model_copy(update={"messages": messages})

    runner = agent_module.build_strands_agent(token)
    started = False
    try:
        async for event in runner.run(run):
            if event.type == EventType.RUN_STARTED:
                started = True
            elif event.type == EventType.TOOL_CALL_START:
                record["tool_calls"] = record.get("tool_calls", 0) + 1
            elif event.type == EventType.TEXT_MESSAGE_CONTENT and "first_delta_ms" not in record:
                record["first_delta_ms"] = elapsed_ms(record)
            elif event.type == EventType.RUN_ERROR:
                record["outcome"] = "error"
            yield event
    except Exception:
        log.exception("run failed thread=%s run=%s", run.thread_id, run.run_id)
        record["outcome"] = "error"
        if not started:
            yield RunStartedEvent(
                type=EventType.RUN_STARTED, thread_id=run.thread_id, run_id=run.run_id
            )
        yield RunErrorEvent(
            type=EventType.RUN_ERROR, message="agent run failed", code="AGENT_ERROR"
        )
    finally:
        usage = getattr(runner, "usage", None)
        if callable(usage):
            record.update(usage())


def elapsed_ms(record: dict) -> int:
    return int((time.monotonic() - record["_t0"]) * 1000)


async def event_stream(
    run: RunAgentInput, token: str, encoder: EventEncoder, record: dict
) -> AsyncIterator[str]:
    record["_t0"] = time.monotonic()
    record.setdefault("outcome", "finished")
    try:
        async for event in with_keepalive(run_agent(run, token, record), DEFAULT_PING_INTERVAL):
            yield encoder.encode(event)
    except asyncio.CancelledError:
        record["outcome"] = "client_disconnected"
        raise
    finally:
        record["total_ms"] = elapsed_ms(record)
        record.pop("_t0", None)
        log.info(json.dumps(record, sort_keys=True))


@app.post("/invocations")
async def invocations(request: Request) -> StreamingResponse:
    body = await request.json()
    encoder = EventEncoder(accept=request.headers.get("accept"))
    media_type = encoder.get_content_type()
    try:
        run = RunAgentInput.model_validate(body)
    except Exception as exc:
        # The contract wants errors on the stream, so a bad body still answers with SSE.
        reason = str(exc)

        async def bad_body() -> AsyncIterator[str]:
            yield encoder.encode(
                RunErrorEvent(type=EventType.RUN_ERROR, message=reason, code="BAD_INPUT")
            )

        return StreamingResponse(bad_body(), media_type=media_type)

    token = bearer_token(request)
    if token is None:
        # Header names only: the runtime forwards Authorization solely when its request
        # header allowlist names it, and this line is what shows that it did not.
        log.warning(
            "no bearer token; headers present: %s", sorted(set(request.headers.keys()))
        )

        async def unauthorized() -> AsyncIterator[str]:
            for event in started_then_error(run, "bearer token required", "UNAUTHORIZED"):
                yield encoder.encode(event)

        return StreamingResponse(unauthorized(), media_type=media_type)

    record = {
        "sub": subject_hash(token),
        "session": request.headers.get(SESSION_HEADER, "-"),
        "thread": run.thread_id,
        "run": run.run_id,
        "trace_id": trace_id(request),
        "request_id": request.headers.get(REQUEST_ID_HEADER, "-"),
        "messages": len(run.messages),
        "tool_calls": 0,
    }
    return StreamingResponse(
        event_stream(run, token, encoder, record),
        media_type=media_type,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/ping")
async def ping() -> JSONResponse:
    return JSONResponse({"status": "Healthy"})
