"""The thread record: what reaches the bucket, and what a failure costs the stream."""

import io
import json
import time

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
from botocore.exceptions import ClientError
from guppi_agent import agent as agent_module
from guppi_agent import conversation_log
from guppi_agent.app import app
from httpx import ASGITransport, AsyncClient

TOKEN = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1c2VyLTEifQ."  # header.{"sub":"user-1"}.
HEADERS = {
    "authorization": f"Bearer {TOKEN}",
    "x-amzn-bedrock-agentcore-runtime-session-id": "session-1",
}
KEY = b"test-key"
BUCKET = "conversation-log"


def run_body(*contents: str, run_id: str = "r1") -> dict:
    roles = ["user", "assistant"]
    messages = [
        {"id": f"m{index}", "role": roles[index % 2], "content": text}
        for index, text in enumerate(contents)
    ]
    return {
        "threadId": "t1",
        "runId": run_id,
        "messages": messages,
        "tools": [],
        "context": [],
        "state": {},
        "forwardedProps": {},
    }


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "S3")


class FakeS3:
    """The two calls the sink makes, with ETags and the conditional headers they take."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.puts: list[dict] = []
        self.version = 0
        self.get_delay = 0.0
        self.get_error: Exception | None = None
        self.conflicts = 0

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 (boto3 spelling)
        if self.get_delay:
            time.sleep(self.get_delay)
        if self.get_error is not None:
            raise self.get_error
        if Key not in self.objects:
            raise client_error("NoSuchKey")
        body, etag = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": etag}

    def put_object(self, **kwargs) -> dict:
        self.puts.append(kwargs)
        if self.conflicts:
            self.conflicts -= 1
            raise client_error("PreconditionFailed")
        self.version += 1
        etag = f'"v{self.version}"'
        self.objects[kwargs["Key"]] = (kwargs["Body"], etag)
        return {"ETag": etag}

    def record(self, thread: str = "t1") -> dict:
        body, _ = self.objects[f"threads/{thread}.json"]
        return json.loads(body)


class FakeRun:
    """Stands in for agent.StrandsRun: a retrieval followed by a two-delta reply."""

    def __init__(self, token: str) -> None:
        self.token = token

    async def run(self, run_input: RunAgentInput):
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
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(agent_module, "build_strands_agent", FakeRun)
    monkeypatch.setattr(conversation_log, "_s3", fake)
    monkeypatch.setattr(conversation_log, "_key", KEY)
    monkeypatch.setenv("CONVERSATION_LOG_ENABLED", "true")
    monkeypatch.setenv("CONVERSATION_LOG_BUCKET", BUCKET)
    yield fake
    conversation_log.reset_caches()


async def post(body: dict, headers: dict = HEADERS):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post("/invocations", json=body, headers=headers)
    await conversation_log.drain()
    return response


def log_records(caplog) -> list[dict]:
    return [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]


async def test_the_first_run_of_a_thread_writes_the_record(fake_s3, caplog):
    caplog.set_level("INFO", logger="guppi_agent")
    response = await post(run_body("hello there"))
    assert response.status_code == 200

    (put,) = fake_s3.puts
    assert put["Key"] == "threads/t1.json"
    assert put["IfNoneMatch"] == "*" and "IfMatch" not in put
    assert put["ServerSideEncryption"] == "aws:kms"

    body = fake_s3.record()
    assert body["schema_version"] == 1
    assert body["thread"] == "t1" and body["session"] == "session-1"
    assert body["created_at"] == body["runs"][0]["started_at"]
    assert body["messages"] == [
        {"id": "m0", "role": "user", "content": "hello there"},
        {"id": "a1", "role": "assistant", "content": "Hello there"},
    ]
    (entry,) = body["runs"]
    assert entry["run"] == "r1" and entry["outcome"] == "finished"
    assert entry["model"] == agent_module.DEFAULT_MODEL_ID
    assert entry["input_tokens"] == 12 and entry["output_tokens"] == 3
    assert entry["tool_calls"] == 1
    assert entry["total_ms"] >= 0 and entry["first_delta_ms"] >= 0


async def test_the_subject_is_the_keyed_pseudonym_and_the_sub_appears_nowhere(fake_s3, caplog):
    caplog.set_level("INFO", logger="guppi_agent")
    await post(run_body("hello there"))
    body = fake_s3.record()
    expected = conversation_log.subject_from_token(TOKEN, KEY)
    assert body["subject"] == expected and len(expected) == 32
    assert body["subject_key"] == 1
    (line,) = log_records(caplog)
    assert line["sub"] == expected  # the line and the record carry one pseudonym
    assert "user-1" not in json.dumps(body) and "user-1" not in caplog.text
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(body)
    assert "Hello there" not in caplog.text  # the reply belongs to the bucket only


async def test_a_second_run_merges_into_the_thread(fake_s3):
    await post(run_body("hello there"))
    first = fake_s3.record()
    await post(
        run_body("hello there", "Hello there", "and again", run_id="r2"),
    )
    _, second_put = fake_s3.puts
    assert second_put["IfMatch"] == '"v1"' and "IfNoneMatch" not in second_put

    body = fake_s3.record()
    assert body["created_at"] == first["created_at"]
    assert body["updated_at"] >= first["updated_at"]
    assert [message["content"] for message in body["messages"]] == [
        "hello there",
        "Hello there",
        "and again",
        "Hello there",
    ]
    assert [entry["run"] for entry in body["runs"]] == ["r1", "r2"]


async def test_a_conflicting_write_is_retried_once_and_then_given_up(fake_s3, caplog):
    caplog.set_level("INFO", logger="guppi_agent")
    fake_s3.conflicts = 1
    await post(run_body("hello there"))
    assert len(fake_s3.puts) == 2 and fake_s3.record()["runs"][0]["run"] == "r1"

    fake_s3.conflicts = 2
    caplog.clear()
    response = await post(run_body("hello there", "Hello there", "again", run_id="r2"))
    assert response.status_code == 200
    assert len(fake_s3.puts) == 4  # two attempts, then the task gives up
    assert [entry["run"] for entry in fake_s3.record()["runs"]] == ["r1"]
    assert "conversation log write failed" in caplog.text


async def test_a_failing_read_writes_nothing_and_logs_one_warning(fake_s3, caplog):
    caplog.set_level("INFO", logger="guppi_agent")
    fake_s3.get_error = client_error("AccessDenied")
    response = await post(run_body("hello there"))
    assert [event for event in response.text.splitlines() if "RUN_FINISHED" in event]
    assert fake_s3.puts == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "thread=t1" in warnings[0].getMessage() and "run=r1" in warnings[0].getMessage()


async def test_a_slow_write_times_out_without_writing(fake_s3, caplog, monkeypatch):
    caplog.set_level("INFO", logger="guppi_agent")
    monkeypatch.setattr(conversation_log, "WRITE_TIMEOUT_SECONDS", 0.05)
    fake_s3.get_delay = 0.4
    response = await post(run_body("hello there"))
    assert response.status_code == 200
    assert fake_s3.puts == []
    assert "conversation log write failed" in caplog.text
    assert "TimeoutError" in caplog.text


async def test_the_switch_off_writes_nothing_and_keeps_the_old_hash(monkeypatch, caplog):
    fake = FakeS3()
    monkeypatch.setattr(agent_module, "build_strands_agent", FakeRun)
    monkeypatch.setattr(conversation_log, "_s3", fake)
    monkeypatch.setattr(conversation_log, "_key", KEY)
    monkeypatch.delenv("CONVERSATION_LOG_ENABLED", raising=False)
    caplog.set_level("INFO", logger="guppi_agent")
    await post(run_body("hello there"))
    assert fake.puts == [] and fake.objects == {}
    (line,) = log_records(caplog)
    assert len(line["sub"]) == 12  # the truncated sha256, as before the switch existed
    assert "_reply" not in line


async def test_a_client_disconnect_is_logged_and_not_merged(fake_s3, caplog):
    caplog.set_level("INFO", logger="guppi_agent")
    run = RunAgentInput.model_validate(run_body("hello there"))
    record = {"thread": "t1", "run": "r1", "outcome": "client_disconnected", "session": "s"}
    conversation_log.schedule_write(run, record, TOKEN)
    await conversation_log.drain()
    assert fake_s3.puts == []
    (line,) = log_records(caplog)
    assert line["outcome"] == "client_disconnected"


def test_the_key_is_read_once_per_container(monkeypatch):
    calls = []

    class FakeSecrets:
        def get_secret_value(self, SecretId: str) -> dict:  # noqa: N803 (boto3 spelling)
            calls.append(SecretId)
            return {"SecretString": "secret-value"}

    conversation_log.reset_caches()
    monkeypatch.setattr(conversation_log, "_secrets", FakeSecrets())
    monkeypatch.setenv("CONVERSATION_LOG_KEY_SECRET_ARN", "arn:aws:secretsmanager:::secret/x")
    assert conversation_log.load_key() == b"secret-value"
    assert conversation_log.load_key() == b"secret-value"
    assert calls == ["arn:aws:secretsmanager:::secret/x"]
    conversation_log.reset_caches()


def test_a_token_without_a_sub_is_unknown():
    assert conversation_log.subject_from_token("not-a-jwt", KEY) == "unknown"
    assert conversation_log.subject_from_token(TOKEN, KEY) != conversation_log.subject_from_token(
        TOKEN, b"other-key"
    )


def test_the_prompt_names_the_logging_only_when_the_switch_is_on(monkeypatch):
    monkeypatch.delenv("CONVERSATION_LOG_ENABLED", raising=False)
    assert "Conversations are logged" not in agent_module.system_prompt()
    monkeypatch.setenv("CONVERSATION_LOG_ENABLED", "true")
    prompt = agent_module.system_prompt()
    assert "Conversations are logged for troubleshooting; nothing is saved between page" in prompt
