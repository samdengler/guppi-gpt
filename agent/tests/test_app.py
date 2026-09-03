import asyncio
import json

import pytest
from ag_ui.core import (
    EventType,
    RunAgentInput,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from guppi_agent import agent as agent_module
from guppi_agent import app as app_module
from guppi_agent.app import app
from guppi_agent.keepalive import with_keepalive
from guppi_agent.validation import trim_messages, validate_run
from httpx import ASGITransport, AsyncClient

TOKEN = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1c2VyLTEifQ."  # header.{"sub":"user-1"}.
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
HEADERS = {
    "authorization": f"Bearer {TOKEN}",
    "x-amzn-bedrock-agentcore-runtime-session-id": "session-1",
    "traceparent": f"00-{TRACE_ID}-00f067aa0ba902b7-01",
    "x-amzn-requestid": "req-1",
}


def message(role: str, content, index: int) -> dict:
    return {"id": f"m{index}", "role": role, "content": content}


def run_body(*contents: str) -> dict:
    roles = ["user", "assistant"]
    messages = [message(roles[i % 2], text, i) for i, text in enumerate(contents)]
    return {
        "threadId": "t1",
        "runId": "r1",
        "messages": messages,
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": {},
    }


def parse_sse(raw: str) -> list[dict]:
    return [
        json.loads(line[len("data:") :].strip())
        for line in raw.splitlines()
        if line.startswith("data:")
    ]


class FakeRun:
    """Stands in for agent.StrandsRun: yields a retrieval followed by a short reply."""

    seen: list[RunAgentInput] = []
    tokens: list[str] = []

    def __init__(self, token: str) -> None:
        FakeRun.tokens.append(token)

    async def run(self, run_input: RunAgentInput):
        FakeRun.seen.append(run_input)
        thread, run = run_input.thread_id, run_input.run_id
        yield RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread, run_id=run)
        yield ToolCallStartEvent(
            type=EventType.TOOL_CALL_START, tool_call_id="c1", tool_call_name="docs___Retrieve"
        )
        yield ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id="c1")
        yield TextMessageStartEvent(
            type=EventType.TEXT_MESSAGE_START, message_id="a1", role="assistant"
        )
        for delta in ("Hello", " there"):
            yield TextMessageContentEvent(
                type=EventType.TEXT_MESSAGE_CONTENT, message_id="a1", delta=delta
            )
        yield TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id="a1")
        yield RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=thread, run_id=run)

    def usage(self) -> dict[str, int]:
        return {"input_tokens": 12, "output_tokens": 3}


@pytest.fixture
def fake_agent(monkeypatch):
    FakeRun.seen = []
    FakeRun.tokens = []
    monkeypatch.setattr(agent_module, "build_strands_agent", FakeRun)
    return FakeRun


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def post(client, body, headers=HEADERS):
    async with client:
        return await client.post("/invocations", json=body, headers=headers)


async def test_ping(client):
    async with client:
        response = await client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "Healthy"}


async def test_run_streams_the_agent_events_in_order(client, fake_agent, caplog):
    caplog.set_level("INFO", logger="guppi_agent")
    response = await post(client, run_body("hello there"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    types = [event["type"] for event in parse_sse(response.text)]
    assert types == [
        "RUN_STARTED",
        "TOOL_CALL_START",
        "TOOL_CALL_END",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert fake_agent.tokens == [TOKEN]
    # The run id the page minted comes back on the first event, so a reader can pair
    # the reply on the page with the agent's log record.
    assert parse_sse(response.text)[0] == {"type": "RUN_STARTED", "threadId": "t1", "runId": "r1"}

    records = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
    (record,) = records
    assert record["outcome"] == "finished"
    assert record["tool_calls"] == 1
    assert record["messages"] == 1
    assert record["session"] == "session-1"
    assert record["thread"] == "t1" and record["run"] == "r1"
    assert record["trace_id"] == TRACE_ID
    assert record["request_id"] == "req-1"
    assert record["input_tokens"] == 12 and record["output_tokens"] == 3
    assert "first_delta_ms" in record and "total_ms" in record
    assert record["sub"] != "user-1" and len(record["sub"]) == 12
    assert TOKEN not in caplog.text


async def test_missing_bearer_is_a_run_error(client, fake_agent):
    response = await post(client, run_body("hello"), headers={})
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[1]["code"] == "UNAUTHORIZED"
    assert fake_agent.seen == []


async def test_bad_body_is_a_run_error_on_the_stream(client, fake_agent):
    response = await post(client, {"nope": True})
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["RUN_ERROR"]
    assert events[0]["code"] == "BAD_INPUT"


@pytest.mark.parametrize(
    "body, fragment",
    [
        (run_body(), "1 to 40"),
        (run_body(*(["x"] * 41)), "1 to 40"),
        (run_body("a", "b"), "last message must be a user turn"),
        (run_body("x" * 4000), "under 4000"),
        (run_body(" "), "non-empty"),
        (
            {**run_body("a"), "messages": [message("user", [{"type": "text", "text": "a"}], 0)]},
            "non-empty string",
        ),
        ({**run_body("a"), "messages": [message("system", "a", 0)]}, "only user and assistant"),
        (
            {
                **run_body("a", "b", "c"),
                "messages": [message("user", "a", 0), message("user", "b", 1)],
            },
            "roles must alternate",
        ),
    ],
)
async def test_invalid_threads_are_run_errors(client, fake_agent, body, fragment):
    response = await post(client, body)
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[1]["code"] == "BAD_INPUT"
    assert fragment in events[1]["message"]
    assert fake_agent.seen == []


async def test_long_threads_are_trimmed_from_the_front(client, fake_agent):
    # 21 turns of 3,900 characters: about 975 tokens each, over the 16,000 budget.
    response = await post(client, run_body(*[f"{i}" + "x" * 3899 for i in range(21)]))
    assert parse_sse(response.text)[-1]["type"] == "RUN_FINISHED"
    (run_input,) = fake_agent.seen
    kept = run_input.messages
    assert len(kept) < 21 and len(kept) % 2 == 1
    assert kept[0].role == "user" and kept[-1].role == "user"
    assert kept[-1].content.startswith("20")


async def test_agent_failure_becomes_a_run_error(client, monkeypatch, caplog):
    class Failing:
        def __init__(self, token):
            pass

        async def run(self, run_input):
            raise RuntimeError("boom")
            yield  # pragma: no cover

    monkeypatch.setattr(agent_module, "build_strands_agent", Failing)
    caplog.set_level("INFO", logger="guppi_agent")
    response = await post(client, run_body("hello"))
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[1]["code"] == "AGENT_ERROR"
    assert "boom" not in events[1]["message"]
    records = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
    assert records[-1]["outcome"] == "error"


def test_validate_and_trim_directly():
    run = RunAgentInput.model_validate(run_body("a" * 40, "b" * 40, "c" * 40))
    assert validate_run(run) is None
    assert [m.role for m in trim_messages(run.messages, budget=0)] == ["user"]
    assert trim_messages(run.messages, budget=10**6) == run.messages


async def test_record_marks_missing_trace_and_request_ids(client, fake_agent, caplog):
    caplog.set_level("INFO", logger="guppi_agent")
    headers = {"authorization": f"Bearer {TOKEN}"}
    await post(client, run_body("hello"), headers=headers)
    (record,) = [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]
    # No traceparent and no tracer provider: nothing to report, and nothing invented.
    assert record["trace_id"] == "-"
    assert record["request_id"] == "-"
    assert record["session"] == "-"


@pytest.mark.parametrize(
    "header, expected",
    [
        (f"00-{TRACE_ID}-00f067aa0ba902b7-01", TRACE_ID),
        (f"  00-{TRACE_ID.upper()}-00F067AA0BA902B7-00  ", TRACE_ID),
        (f"00-{TRACE_ID}-00f067aa0ba902b7", "-"),  # flags missing
        (f"00-{TRACE_ID[:-1]}-00f067aa0ba902b7-01", "-"),  # trace id too short
        ("Root=1-5759e988-bd862e3fe1be46a994272793;Sampled=1", "-"),  # X-Ray form
        ("", "-"),
    ],
)
def test_trace_id_reads_only_a_well_formed_traceparent(header, expected):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/invocations",
        "headers": [(b"traceparent", header.encode())] if header else [],
    }
    assert app_module.trace_id(app_module.Request(scope)) == expected


def test_subject_hash_is_stable_and_never_the_sub():
    digest = app_module.subject_hash(TOKEN)
    assert digest == app_module.subject_hash(TOKEN)
    assert len(digest) == 12 and "user-1" not in digest
    assert app_module.subject_hash("not-a-jwt") == "unknown"


def test_settings_require_the_gateway_url(monkeypatch):
    monkeypatch.delenv("TOOLS_GATEWAY_URL", raising=False)
    with pytest.raises(RuntimeError, match="TOOLS_GATEWAY_URL"):
        agent_module.build_strands_agent("token")
    monkeypatch.setenv("TOOLS_GATEWAY_URL", "https://tools.example/mcp")
    monkeypatch.setenv("MODEL_ID", "test-model")
    runner = agent_module.build_strands_agent("token")
    assert runner._settings.model_id == "test-model"
    assert runner._settings.retrieve_tool == "docs___Retrieve"
    assert runner.usage() == {}


async def test_keepalive_inserts_ping_during_silence():
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
