"""The launcher that takes the Dynatrace token from Secrets Manager (agent/otel_headers.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "otel_headers", Path(__file__).resolve().parents[1] / "otel_headers.py"
)
otel_headers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(otel_headers)
HEADERS_ENV = otel_headers.HEADERS_ENV
SECRET_ENV = otel_headers.SECRET_ENV
environment = otel_headers.environment


def never(arn: str) -> str:
    raise AssertionError("the secret must not be read")


def test_the_header_comes_from_the_secret_the_runtime_names():
    seen = []

    def fetch(arn: str) -> str:
        seen.append(arn)
        return "dt0c01.example\n"

    env = environment({SECRET_ENV: "arn:secret", "KEEP": "1"}, fetch)
    assert seen == ["arn:secret"]
    assert env[HEADERS_ENV] == "Authorization=Api-Token dt0c01.example"
    assert env["KEEP"] == "1"


def test_without_the_arn_the_environment_is_unchanged():
    assert environment({"KEEP": "1"}, never) == {"KEEP": "1"}


def test_the_secret_replaces_a_header_the_platform_set():
    # AgentCore injects its own OTEL_* settings into the container; with the secret named,
    # Dynatrace's Authorization header has to win, as the runtime variable did before.
    env = environment(
        {SECRET_ENV: "arn:secret", HEADERS_ENV: "x-aws-something=1"}, lambda arn: "tok"
    )
    assert env[HEADERS_ENV] == "Authorization=Api-Token tok"


def test_a_secret_that_cannot_be_read_still_starts_the_command(capsys):
    def fetch(arn: str) -> str:
        raise RuntimeError("AccessDenied dt0c01.never-printed")

    env = environment({SECRET_ENV: "arn:secret"}, fetch)
    assert HEADERS_ENV not in env
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "dt0c01" not in err


def test_main_runs_the_command_with_the_new_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(otel_headers, "fetch_secret", lambda arn: "tok")
    monkeypatch.setattr(otel_headers.os, "environ", {SECRET_ENV: "arn:secret"})
    monkeypatch.setattr(
        otel_headers.os, "execvpe", lambda file, args, env: calls.append((file, args, env))
    )
    otel_headers.main(["opentelemetry-instrument", "uvicorn", "guppi_agent.app:app"])
    file, args, env = calls[0]
    assert file == "opentelemetry-instrument"
    assert args == ["opentelemetry-instrument", "uvicorn", "guppi_agent.app:app"]
    assert env[HEADERS_ENV] == "Authorization=Api-Token tok"


def test_main_needs_a_command():
    with pytest.raises(SystemExit):
        otel_headers.main([])
