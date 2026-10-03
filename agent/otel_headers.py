"""Puts the Dynatrace trace export header in place from Secrets Manager, then runs the command.

The runtime's environment carries only `DYNATRACE_TOKEN_SECRET_ARN`; the API token stays in
Secrets Manager and enters this process's environment just before `opentelemetry-instrument`
reads `OTEL_EXPORTER_OTLP_TRACES_HEADERS` (docs/proposals/dynatrace.md, "The token in Secrets
Manager"). Without the ARN the command runs with the environment unchanged. A token that
cannot be read is reported on stderr and the command still starts: trace export then fails,
the agent does not.

This is a standalone file, not part of the guppi_agent package: importing the package loads
the whole kit, and this runs before every start.

    python /app/otel_headers.py opentelemetry-instrument uvicorn guppi_agent.app:app ...
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping

SECRET_ENV = "DYNATRACE_TOKEN_SECRET_ARN"
HEADERS_ENV = "OTEL_EXPORTER_OTLP_TRACES_HEADERS"


def headers(token: str) -> str:
    """The OTLP header value Dynatrace's ingest expects for an API token."""
    return f"Authorization=Api-Token {token.strip()}"


def environment(env: Mapping[str, str], fetch: Callable[[str], str]) -> dict[str, str]:
    """`env` with the trace export header from the secret `env` names, if any. The header
    replaces any value already set: AgentCore injects OTEL_* settings of its own, and the
    runtime variable this replaces used to override them the same way."""
    arn = env.get(SECRET_ENV)
    if not arn:
        return dict(env)
    try:
        token = fetch(arn)
    except Exception as exc:  # noqa: BLE001 - any failure leaves the agent starting
        print(
            f"otel_headers: could not read {SECRET_ENV} ({type(exc).__name__}); "
            "trace export starts without its header",
            file=sys.stderr,
        )
        return dict(env)
    return {**env, HEADERS_ENV: headers(token)}


def fetch_secret(arn: str) -> str:
    import boto3

    return boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]


def main(argv: list[str] | None = None) -> None:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        raise SystemExit("usage: python otel_headers.py <command> [args...]")
    os.execvpe(command[0], command, environment(os.environ, fetch_secret))


if __name__ == "__main__":
    main()
