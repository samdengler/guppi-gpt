"""GuppiGPT agent on the AgentCore Runtime AG-UI contract.

Spike stage: no model call yet. The run echoes the last user message word by word so the
streaming path (CloudFront, edge gateway, runtime) can be exercised end to end. Two
`forwardedProps` keys drive the timing tests:

* ``silentSeconds``: stay silent for this long after RUN_STARTED, so the keepalive ping is
  the only traffic. Used to prove that a silent stretch longer than the CloudFront origin
  timeout survives.
* ``wordDelay``: seconds between words (default 0.05).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from ag_ui.core import (
    BaseEvent,
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from guppi_agent.keepalive import DEFAULT_PING_INTERVAL, with_keepalive

log = logging.getLogger("guppi_agent")

app = FastAPI(title="guppi-agent")

SESSION_HEADER = "x-amzn-bedrock-agentcore-runtime-session-id"


def last_user_text(run: RunAgentInput) -> str:
    for message in reversed(run.messages):
        if message.role == "user" and isinstance(message.content, str):
            return message.content
    return ""


async def run_agent(run: RunAgentInput) -> AsyncIterator[BaseEvent]:
    """Produce the AG-UI events for one run. RUN_STARTED is first, before any work."""
    yield RunStartedEvent(type=EventType.RUN_STARTED, thread_id=run.thread_id, run_id=run.run_id)

    props = run.forwarded_props if isinstance(run.forwarded_props, dict) else {}
    silent = float(props.get("silentSeconds", 0) or 0)
    word_delay = float(props.get("wordDelay", 0.05) or 0)

    if silent > 0:
        await asyncio.sleep(silent)

    message_id = str(uuid.uuid4())
    yield TextMessageStartEvent(
        type=EventType.TEXT_MESSAGE_START, message_id=message_id, role="assistant"
    )
    reply = f"Guppi here. You said: {last_user_text(run) or '(nothing)'}"
    for index, word in enumerate(reply.split(" ")):
        delta = word if index == 0 else f" {word}"
        yield TextMessageContentEvent(
            type=EventType.TEXT_MESSAGE_CONTENT, message_id=message_id, delta=delta
        )
        if word_delay:
            await asyncio.sleep(word_delay)
    yield TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=message_id)
    yield RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=run.thread_id, run_id=run.run_id)


async def event_stream(run: RunAgentInput, encoder: EventEncoder) -> AsyncIterator[str]:
    try:
        async for event in with_keepalive(run_agent(run), DEFAULT_PING_INTERVAL):
            yield encoder.encode(event)
    except Exception:
        log.exception("run failed thread=%s run=%s", run.thread_id, run.run_id)
        yield encoder.encode(
            RunErrorEvent(type=EventType.RUN_ERROR, message="agent run failed", code="AGENT_ERROR")
        )


@app.post("/invocations")
async def invocations(request: Request) -> StreamingResponse:
    body = await request.json()
    encoder = EventEncoder(accept=request.headers.get("accept"))
    try:
        run = RunAgentInput.model_validate(body)
    except Exception as exc:
        # The contract wants errors on the stream, so a bad body still answers with SSE.
        reason = str(exc)

        async def bad_input() -> AsyncIterator[str]:
            yield encoder.encode(
                RunErrorEvent(type=EventType.RUN_ERROR, message=reason, code="BAD_INPUT")
            )

        return StreamingResponse(bad_input(), media_type=encoder.get_content_type())

    log.info(
        "run thread=%s run=%s session=%s",
        run.thread_id,
        run.run_id,
        request.headers.get(SESSION_HEADER, "-"),
    )
    return StreamingResponse(
        event_stream(run, encoder),
        media_type=encoder.get_content_type(),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/ping")
async def ping() -> JSONResponse:
    return JSONResponse({"status": "Healthy"})
