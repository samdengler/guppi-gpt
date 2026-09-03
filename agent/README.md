# guppi-agent

FastAPI application implementing the AgentCore Runtime AG-UI contract:
`POST /invocations` streams AG-UI events as server-sent events, `GET /ping` reports health.
See the repository AGENTS.md for how it fits the whole system.

## Modules

| Module | Role |
| --- | --- |
| `app.py` | The HTTP surface: bearer token, validation, the log record, the SSE response |
| `agent.py` | One Strands agent per request: Bedrock model, system prompt, the MCP client to the tools gateway with the caller's token |
| `validation.py` | Thread shape checks and front trimming to the token budget |
| `keepalive.py` | The `ping` custom event during silent stretches |

## Environment

| Variable | Meaning | Default |
| --- | --- | --- |
| `TOOLS_GATEWAY_URL` | MCP endpoint of the tools gateway (required) | none |
| `MODEL_ID` | Bedrock model or inference profile id | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `RETRIEVE_TOOL` | The one gateway tool the agent is given | `docs___Retrieve` |
| `AWS_REGION` | Bedrock region | `us-east-1` |
| `LOG_LEVEL` | Python logging level | `INFO` |

The agent forwards the request's bearer token to the tools gateway unchanged; it decodes
the token only to hash the `sub` claim for the per-run log record.

## Tests

`uv run -- pytest agent/tests` replaces `agent.build_strands_agent` with a fake, so the
tests need no network and no AWS credentials.
