# guppi-mcp-app Agent Instructions

## Project Overview

guppi-mcp-app is a tools-only project on the GuppiGPT platform (`chat.dengler.io`), kept
in guppi-gpt as `examples/mcp-app/` (it was its own repository until 2 October 2026). It
deploys one MCP server to AgentCore Runtime and registers it as a target on the platform's
tools gateway, so the platform's Guppi agent offers its tools on the project page
`https://chat.dengler.io/p/mcp-app/`. The page, sign-in, CloudFront, the gateways and the
agent belong to the rest of guppi-gpt; work in this folder never changes them, so it stays
an honest example of a project in its own repository. The contract between the two is the
set of SSM parameters under `/guppi/platform/` and the project manifest, both described in
`../../docs/proposals/platform.md`. Commands below run from this folder.

`show_card` returns an MCP Apps UI resource (`ui://mcp-app/card`), which the platform
page renders in a sandboxed frame (guppi-gpt phase 3). Phase 4 added one tool per way an
app can reach and talk to the page: `show_chart` (resource by reference), `card_clicked`
(called by the card's button, app-only), `update_card` (a later result for the same card),
`show_static_page` (a `text/uri-list` resource naming `web/app/`) and `ask_preferences` (a
form that sends `ui/update-model-context`). `docs/experiments.md` compares them.

The gateway target is named `mcp-app`, so the tools reach the agent as
`mcp-app___<tool>`; the platform agent offers `mcp-app___*` (or `mcp_app___*`) tools on
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
  phase-4.md              # the phase 4 brief: the MCP Apps experiments
  experiments.md          # the comparison: one row per experiment, E1 to E6
  phase-4-report.md       # what phase 4 landed, its checks, and what is left for Sam
infra/
  app.py                  # CDK app entry
  guppi_mcp_app_infra/stack.py  # the Runtime, the tools gateway target, the invoke grant
  tests/                  # assertions against the synthesized template
server/
  src/mcp_app_server/     # the FastMCP server and its tools; bridge.py (the app half of the
                          # MCP Apps bridge, shared by every page), card.py, chart.py,
                          # preferences.py, static_page.py (one UI resource each)
  Dockerfile              # arm64, the MCP server on 8000; built from the repo root so uv.lock is in context
  tests/                  # the tools and resources through an in-process MCP client
web/
  manifest.json           # the project manifest the platform page loads
  app/                    # E5's static MCP App page, published beside the manifest
scripts/
  deploy.sh               # cdk deploy, sync the gateway target, publish web/ to projects/mcp-app/
  probe.py                # a small MCP client: tools/list, tools/call, resources/list, resources/read
  browser-check.mjs       # the signed-in headless check of each experiment on the live page
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
- The page renders plain text only. The app pages are served to an MCP Apps host, which
  puts them in a sandboxed iframe; they have no external scripts, make no network calls,
  write values only through `textContent`, and never rely on form submission (the host's
  sandbox has no `allow-forms`). `web/app/` is the exception by design: a static page
  under the site CSP, so its script and style are files.

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
../../scripts/test-token.sh | uv run -- python scripts/probe.py "$url" --token -
```

The container runs the same way:

```sh
docker build --platform linux/arm64 -f server/Dockerfile -t mcp-app-server .
docker run --rm -p 8000:8000 mcp-app-server
```

## Deploying

`scripts/deploy.sh` runs `cdk deploy GuppiMcpApp` (Docker builds the arm64 server image on
Sam's Mac), synchronizes the `mcp-app` gateway target (a `DEFAULT`-listing target serves
the tools of its last sync, so a new server changes nothing on the gateway without it),
then syncs `web/` to `s3://<site-bucket>/projects/mcp-app/` and invalidates
`/projects/mcp-app/*`, with the bucket and distribution read from SSM
(`/guppi/platform/site-bucket-name`, `/guppi/platform/distribution-id`).
`scripts/deploy.sh --site-only` skips `cdk deploy`. Every run is also written to
`.deploy/deploy-<timestamp>.log` with `.deploy/latest.log` pointing at the newest and a
final `deploy exit=<code>` line.

The browser check runs every experiment on the live page with the test session (the same
`$HOME/.config/guppi/test-session.json` guppi-gpt's `test-token.sh` uses), reading
Playwright from `../../web` (the guppi-gpt page):

```sh
node scripts/browser-check.mjs            # E1 to E6, screenshots in .deploy/phase-4-E<n>.png
node scripts/browser-check.mjs E3         # one experiment
```
