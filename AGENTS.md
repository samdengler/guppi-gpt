# guppi-mcp-app Agent Instructions

## Project Overview

guppi-mcp-app is a tools-only project on the GuppiGPT platform (`chat.dengler.io`). It
deploys one MCP server to AgentCore Runtime and registers it as a target on the platform's
tools gateway, so the platform's Guppi agent offers its tools on the project page
`https://chat.dengler.io/p/mcp-app/`. The page, sign-in, CloudFront, the gateways and the
agent belong to the guppi-gpt repository; this repository never changes them. The contract
between the two is the set of SSM parameters under `/guppi/platform/` and the project
manifest, both described in guppi-gpt's `docs/proposals/platform.md`.

The server's one tool, `show_card`, returns an MCP Apps UI resource (`ui://mcp-app/card`),
the first experiment toward MCP Apps in the platform page.

The gateway target is named `mcp-app`, so the tool reaches the agent as
`mcp-app___show_card`; the platform agent offers `mcp-app___*` (or `mcp_app___*`) tools on
a run whose `forwardedProps.project` is `mcp-app`. The tools gateway checks the user's JWT
and then signs its call to the Runtime with its own role (`GATEWAY_IAM_ROLE`), because it
refuses `JWT_PASSTHROUGH` on an MCP server target; the Runtime therefore has no JWT
authorizer and accepts only callers allowed `InvokeAgentRuntime`, which this stack grants
to the tools gateway role. `-c target_credentials=JWT_PASSTHROUGH` synthesizes the
passthrough design for when the service accepts it. The target lists in `DEFAULT` mode
(tools cached at the control plane) so its tools share the first `tools/list` page with
the `docs` target; the platform agent reads only the first page. `docs/decision-log.md`
has the reasons.

## Tech Stack

- Infrastructure: AWS CDK v2 in Python, one stack `GuppiMcpApp`, region `us-east-1`
- Server: Python 3.12, the official `mcp` SDK (FastMCP), streamable HTTP, stateless, on
  `0.0.0.0:8000` at `/mcp`, an arm64 container on AgentCore Runtime with protocol `MCP`
- Page: none of its own; `web/manifest.json` is published to the platform's site bucket
  under `projects/mcp-app/`
- Package manager: uv workspace (`infra` and `server` are members)
- Secrets: none; the stack has no secret parameters

## Project Structure

```
docs/
  phase-2.md              # the phase 2 brief
  decision-log.md         # decisions taken where a brief did not settle the question
  phase-2-report.md       # what phase 2 landed, its checks, and what is left for Sam
infra/
  app.py                  # CDK app entry
  guppi_mcp_app_infra/stack.py  # the Runtime, the tools gateway target, the invoke grant
  tests/                  # assertions against the synthesized template
server/
  src/mcp_app_server/     # the FastMCP server, show_card, the card resource and its HTML
  Dockerfile              # arm64, the MCP server on 8000; built from the repo root so uv.lock is in context
  tests/                  # show_card and the resource through an in-process MCP client
web/
  manifest.json           # the project manifest the platform page loads
scripts/
  deploy.sh               # cdk deploy, then publish web/ to projects/mcp-app/
  probe.py                # a small MCP client: tools/list, tools/call, resources/list, resources/read
```

## Rules

- No Lambda functions by default. Lambda is a preference, not a ban: when a function is
  the right tool, propose it and get Sam's approval before building it.
- Secrets never enter files, `cdk.context.json`, or `-c` context values. A value the stack
  cannot produce becomes a CloudFormation parameter with `no_echo`; a value it can produce
  lives in Secrets Manager and reaches the template only as a dynamic reference.
- Tokens from guppi-gpt's `scripts/test-token.sh` never appear in logs, reports, commits
  or test fixtures.
- The server image installs dependencies from `uv.lock` in a layer before the source is
  copied; `.dockerignore` at the repo root limits the build context (and the CDK asset
  hash) to the server files and the lockfile.
- The tools gateway pages `tools/list` and `resources/list`; any client here, the probe
  included, follows `nextCursor`.
- Every change to the stack must keep `uv run -- pytest` green and
  `uv run -- cdk synth -c image_uri=<any ecr uri>` working without Docker.
- Only the `GuppiMcpApp` stack is deployed from here. Platform resources (the gateways,
  the site bucket, the distribution) are read from SSM and never edited, and nothing is
  written to the site bucket outside `projects/mcp-app/`.
- Prose in docs and comments: no em-dashes or en-dashes, no second person.
- The page renders plain text only. The card HTML is served to an MCP Apps host, which
  puts it in a sandboxed iframe; it has no external scripts and makes no network calls.

## Dependency Management

```sh
uv sync --all-packages --dev             # install all workspace members and dev tools
uv add --package mcp-app-server httpx    # add a dependency to one member
uv run -- pytest                         # run all tests
uv run -- ruff check .
```

- Always use `uv add --package <member>` for dependencies, not manual pyproject edits.

## Local Development

```sh
uv run --package mcp-app-server -- python -m mcp_app_server   # serves http://localhost:8000/mcp
uv run -- python scripts/probe.py http://localhost:8000/mcp
```

Through the platform's tools gateway, with a test token that never reaches a command
line:

```sh
url="$(aws ssm get-parameter --name /guppi/platform/tools-gateway-url --query Parameter.Value --output text)"
../guppi-gpt/scripts/test-token.sh | uv run -- python scripts/probe.py "$url" --token -
```

The container runs the same way:

```sh
docker build --platform linux/arm64 -f server/Dockerfile -t mcp-app-server .
docker run --rm -p 8000:8000 mcp-app-server
```

## Deploying

`scripts/deploy.sh` runs `cdk deploy GuppiMcpApp` (Docker builds the arm64 server image on
Sam's Mac), then syncs `web/` to `s3://<site-bucket>/projects/mcp-app/` and invalidates
`/projects/mcp-app/*`, with the bucket and distribution read from SSM
(`/guppi/platform/site-bucket-name`, `/guppi/platform/distribution-id`).
`scripts/deploy.sh --site-only` skips `cdk deploy`. Every run is also written to
`.deploy/deploy-<timestamp>.log` with `.deploy/latest.log` pointing at the newest and a
final `deploy exit=<code>` line.
