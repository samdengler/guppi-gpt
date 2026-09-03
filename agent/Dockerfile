# AgentCore Runtime expects an arm64 image listening on 0.0.0.0:8080.
FROM --platform=linux/arm64 python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .

EXPOSE 8080
CMD ["uvicorn", "guppi_agent.app:app", "--host", "0.0.0.0", "--port", "8080"]
