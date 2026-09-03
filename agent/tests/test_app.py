import json

import pytest
from ag_ui.core import EventType, RunFinishedEvent
from guppi_agent.app import app
from guppi_agent.keepalive import with_keepalive
from httpx import ASGITransport, AsyncClient


def run_body(text: str, **props):
    return {
        "threadId": "t1",
        "runId": "r1",
        "messages": [{"id": "m1", "role": "user", "content": text}],
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": props,
    }


def parse_sse(raw: str):
    events = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:") :].strip()))
    return events


async def test_ping():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "Healthy"}


async def test_run_streams_expected_event_sequence():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/invocations", json=run_body("hello there", wordDelay=0))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    types = [event["type"] for event in events]
    assert types[0] == "RUN_STARTED"
    assert types[1] == "TEXT_MESSAGE_START"
    assert types[-2] == "TEXT_MESSAGE_END"
    assert types[-1] == "RUN_FINISHED"
    text = "".join(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert text == "Guppi here. You said: hello there"


async def test_bad_input_is_a_run_error_on_the_stream():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/invocations", json={"nope": True})
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert events == [dict(events[0])]
    assert events[0]["type"] == "RUN_ERROR"
    assert events[0]["code"] == "BAD_INPUT"


async def test_keepalive_inserts_ping_during_silence():
    import asyncio

    async def slow():
        await asyncio.sleep(0.25)
        yield RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id="t", run_id="r")

    seen = [event async for event in with_keepalive(slow(), interval=0.1)]
    names = [getattr(event, "name", None) for event in seen]
    assert names.count("ping") >= 2
    assert seen[-1].type == EventType.RUN_FINISHED


async def test_keepalive_propagates_errors():
    async def failing():
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        async for _ in with_keepalive(failing(), interval=0.1):
            pass
