"""The thread record: one JSON object per conversation in the conversation log bucket.

A run's data is complete only when its stream has ended, so `schedule_write` runs after the
last event: it reads the HMAC key once per container, replaces the log record's subject with
the pseudonym, writes the CloudWatch line, and then reads, merges, and writes
`threads/<threadId>.json` under a ten second timeout. Every failure is one warning line and
nothing else; the stream is finished by the time any of this runs.

CONVERSATION_LOG_ENABLED decides whether any of it happens. With the switch off the module
does nothing and `app.py` logs the run line as it always has.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from ag_ui.core import RunAgentInput

log = logging.getLogger("guppi_agent")

SCHEMA_VERSION = 1
SUBJECT_KEY_VERSION = 1
SUBJECT_HEX_CHARS = 32  # 128 bits of the HMAC, enough that collisions are not a concern
THREADS_PREFIX = "threads/"
WRITE_TIMEOUT_SECONDS = 10
MISSING_KEY_CODES = {"NoSuchKey", "NoSuchBucket", "404"}
CONFLICT_CODES = {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}

_key: bytes | None = None
_s3: Any = None
_secrets: Any = None
_tasks: set[asyncio.Task] = set()


def enabled() -> bool:
    return os.environ.get("CONVERSATION_LOG_ENABLED", "").lower() == "true"


def bucket_name() -> str:
    return os.environ.get("CONVERSATION_LOG_BUCKET", "")


def reset_caches() -> None:
    """Drop the cached key and clients. Used by tests; the container calls it never."""
    global _key, _s3, _secrets
    _key, _s3, _secrets = None, None, None


def s3_client() -> Any:
    global _s3
    if _s3 is None:
        import boto3

        _s3 = boto3.client("s3")
    return _s3


def secrets_client() -> Any:
    global _secrets
    if _secrets is None:
        import boto3

        _secrets = boto3.client("secretsmanager")
    return _secrets


def load_key() -> bytes:
    """The HMAC key, read from Secrets Manager once per container and held in memory."""
    global _key
    if _key is None:
        arn = os.environ.get("CONVERSATION_LOG_KEY_SECRET_ARN", "")
        if not arn:
            raise RuntimeError("CONVERSATION_LOG_KEY_SECRET_ARN is not set")
        response = secrets_client().get_secret_value(SecretId=arn)
        _key = response["SecretString"].encode()
    return _key


def claims(token: str) -> dict:
    """The unverified claims of a bearer token; the runtime validated it already."""
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


def subject_from_token(token: str, key: bytes) -> str:
    """The pseudonym for a token's sub claim: the HMAC of the sub, truncated to 128 bits."""
    found = claims(token)
    # Okta access tokens carry the stable user id in uid; their sub is the login (email).
    sub = str(found.get("uid") or found.get("sub", ""))
    if not sub:
        return "unknown"
    return hmac.new(key, sub.encode(), hashlib.sha256).hexdigest()[:SUBJECT_HEX_CHARS]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def loggable(record: dict) -> dict:
    """The record without the keys the CloudWatch line must not carry, such as the reply."""
    return {key: value for key, value in record.items() if not key.startswith("_")}


def run_entry(record: dict) -> dict:
    """One entry of the thread's runs list: what the run cost and how it ended."""
    return {
        "run": record.get("run"),
        "started_at": record.get("started_at"),
        "model": record.get("model"),
        "input_tokens": record.get("input_tokens", 0),
        "output_tokens": record.get("output_tokens", 0),
        "first_delta_ms": record.get("first_delta_ms"),
        "total_ms": record.get("total_ms"),
        "tool_calls": record.get("tool_calls", 0),
        "outcome": record.get("outcome"),
    }


def merge(existing: dict | None, run_input: RunAgentInput, record: dict, subject: str) -> dict:
    """The thread record after this run: the thread as the page sent it plus the reply."""
    messages = [
        {"id": message.id, "role": message.role, "content": str(message.content)}
        for message in run_input.messages
    ]
    reply = record.get("_reply", "")
    if reply:
        messages.append(
            {"id": record.get("_reply_id") or "reply", "role": "assistant", "content": reply}
        )
    updated_at = now_iso()
    previous = existing or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "thread": run_input.thread_id,
        "session": record.get("session", "-"),
        "subject": subject,
        "subject_key": SUBJECT_KEY_VERSION,
        "created_at": previous.get("created_at") or record.get("started_at") or updated_at,
        "updated_at": updated_at,
        "messages": messages,
        "runs": list(previous.get("runs", [])) + [run_entry(record)],
    }


def error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def get_record(bucket: str, key: str) -> tuple[dict | None, str | None]:
    """The thread's current record and its ETag, or (None, None) when it does not exist."""
    try:
        response = s3_client().get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if error_code(exc) in MISSING_KEY_CODES:
            return None, None
        raise
    body = response["Body"].read()
    return json.loads(body), response.get("ETag")


def put_record(bucket: str, key: str, body: dict, etag: str | None) -> None:
    """Write the record, refusing to overwrite a version this task did not read."""
    condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
    s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=(json.dumps(body, sort_keys=True) + "\n").encode(),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
        **condition,
    )


def write_record(bucket: str, run_input: RunAgentInput, record: dict, subject: str) -> None:
    """Read, merge, and write the thread record, retrying once when another writer won.

    A subject that differs from the stored one is not merged: the thread would then hold two
    people's messages, and the page never shares a thread id between sign-ins.
    """
    key = f"{THREADS_PREFIX}{run_input.thread_id}.json"
    for attempt in (1, 2):
        existing, etag = get_record(bucket, key)
        if existing and existing.get("subject") not in (None, subject):
            log.warning(
                "conversation log subject mismatch thread=%s run=%s",
                run_input.thread_id,
                run_input.run_id,
            )
            return
        try:
            put_record(bucket, key, merge(existing, run_input, record, subject), etag)
            return
        except Exception as exc:
            if attempt == 2 or error_code(exc) not in CONFLICT_CODES:
                raise
            log.warning(
                "conversation log conflict, retrying thread=%s run=%s",
                run_input.thread_id,
                run_input.run_id,
            )


async def write(run_input: RunAgentInput, record: dict, token: str) -> None:
    key = await asyncio.to_thread(load_key)
    record["sub"] = subject_from_token(token, key)
    log.info(json.dumps(loggable(record), sort_keys=True))
    if record.get("outcome") == "client_disconnected":
        # The run line already records the disconnect; the thread keeps the runs it served.
        return
    bucket = bucket_name()
    if not bucket:
        raise RuntimeError("CONVERSATION_LOG_BUCKET is not set")
    await asyncio.to_thread(write_record, bucket, run_input, record, record["sub"])


async def guarded_write(run_input: RunAgentInput, record: dict, token: str) -> None:
    try:
        await asyncio.wait_for(write(run_input, record, token), timeout=WRITE_TIMEOUT_SECONDS)
    except Exception as exc:
        log.warning(
            "conversation log write failed thread=%s run=%s: %s: %s",
            run_input.thread_id,
            run_input.run_id,
            type(exc).__name__,
            exc,
        )


def schedule_write(run_input: RunAgentInput, record: dict, token: str) -> None:
    """Start the write as a task of its own, so the stream's end waits for nothing.

    The task is created in the `finally` of the stream, where a client disconnect has already
    cancelled the request's task; a task created there is independent of that cancellation.
    The set holds a reference so the loop cannot collect a running task.
    """
    task = asyncio.create_task(guarded_write(run_input, record, token))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain() -> None:
    """Wait for the pending writes. Used by tests; the container calls it never."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
