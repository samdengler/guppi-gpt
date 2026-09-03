# AgentCore Runtime expects an arm64 image listening on 0.0.0.0:8080.
# Build context is the repository root (see the CDK stack and .dockerignore) so the
# workspace lockfile is visible: dependencies install from it in their own layer, which
# Docker reuses until uv.lock or a pyproject changes, and the source lands in a small
# layer of its own afterwards.
FROM --platform=linux/arm64 python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

# Workspace metadata only: the root project, the lockfile, and each member's pyproject
# (uv refuses to resolve a workspace with a member missing).
COPY pyproject.toml uv.lock ./
COPY agent/pyproject.toml agent/README.md agent/
COPY infra/pyproject.toml infra/
RUN uv sync --frozen --package guppi-agent --no-dev --no-editable --no-install-workspace

# The agent itself. Only this layer and the sync below rerun on a source change.
COPY agent/src agent/src
RUN uv sync --frozen --package guppi-agent --no-dev --no-editable

EXPOSE 8080
# opentelemetry-instrument (from aws-opentelemetry-distro, a guppi-agent dependency) is
# the documented AgentCore entrypoint wrapper: it installs the ADOT tracer provider and
# the FastAPI, httpx, MCP, and botocore instrumentation before uvicorn imports the app,
# so one trace covers the request, the Strands run, the tools gateway call, and the
# Bedrock call. The runtime supplies the exporter settings as environment variables
# (AGENT_OBSERVABILITY_ENABLED and OTEL_*). Local development starts uvicorn directly
# (AGENTS.md) and needs none of this. See docs/proposals/traceability.md.
CMD ["opentelemetry-instrument", "uvicorn", "guppi_agent.app:app", "--host", "0.0.0.0", "--port", "8080"]
