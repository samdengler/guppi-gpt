# guppi-agent

FastAPI application implementing the AgentCore Runtime AG-UI contract:
`POST /invocations` streams AG-UI events as server-sent events, `GET /ping` reports health.
See the repository AGENTS.md for how it fits the whole system.

A project agent on the platform (`docs/proposals/platform.md`) installs this package by
git URL, pinned to a tag:

    uv add "guppi-agent @ git+https://github.com/samdengler/guppi-gpt@<tag>#subdirectory=agent"

A project's whole HTTP surface is the factory. `build_agent(token)` returns an object
whose `run(run_input)` is an async iterator of AG-UI events (a Strands agent behind the
`ag_ui_strands` adapter, or anything else that yields them), and optionally `usage()` with
fields for the run log:

```python
# my_project/app.py
from guppi_agent import create_app

from my_project.agent import build_agent

app = create_app(build_agent)
```

and in the project's Dockerfile, the same server line as this repository's:

```dockerfile
CMD ["opentelemetry-instrument", "uvicorn", "my_project.app:app", "--host", "0.0.0.0", "--port", "8080"]
```

`guppi_agent` also exports `with_keepalive`, `trim_messages`, `validate_run`,
`conversation_log`, `app_resource` and `with_app_resources` for a project that needs the
same pieces outside the factory.

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
| `RETRIEVE_TOOL` | The gateway tool every run is given; a run whose `forwardedProps.project` names a project also gets that project's `<project>___*` tools | `docs___Retrieve` |
| `AWS_REGION` | Bedrock region | `us-east-1` |
| `LOG_LEVEL` | Python logging level | `INFO` |

The agent forwards the request's bearer token to the tools gateway unchanged; it decodes
the token only to hash the `sub` claim for the per-run log record.

## Tests

`uv run -- pytest agent/tests` replaces `agent.build_strands_agent` with a fake, so the
tests need no network and no AWS credentials.
