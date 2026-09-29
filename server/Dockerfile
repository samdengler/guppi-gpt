# AgentCore Runtime with protocol MCP expects an arm64 image serving streamable HTTP on
# 0.0.0.0:8000 at /mcp. The build context is the repository root (see the CDK stack and
# .dockerignore) so the workspace lockfile is visible: dependencies install from it in
# their own layer, and the source lands in a small layer of its own afterwards.
FROM --platform=linux/arm64 python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

# Workspace metadata only: the root project, the lockfile, and each member's pyproject
# (uv refuses to resolve a workspace with a member missing).
COPY pyproject.toml uv.lock ./
COPY server/pyproject.toml server/README.md server/
COPY infra/pyproject.toml infra/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package mcp-app-server --no-dev --no-editable --no-install-workspace

# The server itself. Only this layer and the sync below rerun on a source change.
COPY server/src server/src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package mcp-app-server --no-dev --no-editable

EXPOSE 8000
CMD ["python", "-m", "mcp_app_server"]
