# GuppiGPT Agent Instructions

## Project Overview

GuppiGPT is a minimal claude.ai style chat: one page, stateless plain text conversation,
no history, no attachments, Google sign-in, and answers grounded in a Bedrock Knowledge
Base. The design document (`docs/guppigpt-design.html`) and the decision log
(`docs/guppigpt-decision-log.html`) are the source of truth for architecture; this
repository implements them. `docs/guppigpt-architecture.html` holds the diagrams. When code and design disagree,
fix one of them in the same change.

Guppi is Bob's ship AI from *We Are Legion (We Are Bob)*.

## Tech Stack

- Infrastructure: AWS CDK v2 in Python, one stack `GuppiGpt`, region `us-east-1`
- Agent: Python 3.12, FastAPI, AG-UI over SSE, Strands Agents with the `ag-ui-strands` adapter, arm64 container on AgentCore Runtime
- Model: Claude Haiku 4.5 through the `us.` cross-region inference profile (`MODEL_ID` in the stack)
- Edge: CloudFront in front of an AgentCore Gateway runtime target; Cognito user pool federated to Google
- Page: static HTML and vanilla JavaScript in `web/src/`, bundled once by esbuild into `web/dist/` with `@ag-ui/client` as the stream reader, served from S3 through CloudFront
- Package manager: uv workspace (`infra` and `agent` are members)
- Secrets: 1Password CLI at deploy time for the Google OAuth client; the origin header value is generated in Secrets Manager by the stack; nothing secret is checked in

## Project Structure

```
docs/                     # design document, decision log, architecture diagrams (self-contained HTML)
  proposals/              # backlog proposals in Markdown; traceability.md covers run and trace ids
infra/
  app.py                  # CDK app entry
  guppi_gpt_infra/stack.py
  tests/                  # assertions against the synthesized template
agent/
  src/guppi_agent/app.py         # FastAPI app: POST /invocations (SSE), GET /ping, per-run log record with trace id
  src/guppi_agent/agent.py       # per-run MCP client with the user token, Strands agent, AG-UI adapter
  src/guppi_agent/validation.py  # run input validation and front trimming
  src/guppi_agent/keepalive.py   # CUSTOM ping event after 15 silent seconds
  Dockerfile                     # arm64, uvicorn on 8080; built from the repo root so uv.lock is in context
  tests/
web/
  package.json            # esbuild, @ag-ui/client, @openfeature/web-sdk; `npm run build` writes dist/
  src/index.html          # the page; no inline script or style (CSP is default-src 'self')
  src/app.js              # PKCE sign-in by hand, HttpAgent subscriber, plain text rendering
  src/app.css
  src/features.js         # OpenFeature static provider; initFeatures() and isEnabled()
  src/flags-core.js       # pure override parsing and default/override overlay, tested without a DOM
  features.json           # committed feature flag defaults, merged into config.json at deploy time
  test/features.test.mjs  # node:test coverage for flags-core.js, run by `npm test`
  dist/                   # build output plus config.json written by deploy.sh; not committed
scripts/
  deploy.sh
  seed-content.sh         # clone the docs repositories at pinned revisions, sync Markdown to S3
  ingest.sh               # StartIngestionJob and wait
```

## Rules

- No Lambda functions anywhere in the request path or the content sync path.
- Secrets never enter files, `cdk.context.json`, or `-c` context values. Values the stack
  cannot produce (the Google OAuth client) are CloudFormation parameters with `no_echo`
  supplied by `scripts/deploy.sh` from 1Password; values it can produce (the
  `X-Origin-Verify` header) live in Secrets Manager and reach the template only as
  dynamic references.
- The agent image installs dependencies from `uv.lock` in a layer before the source is
  copied, so a code change rebuilds only the last layer; `.dockerignore` at the repo root
  limits the build context (and the CDK asset hash) to the agent files and the lockfile.
- Every change to the stack must keep `uv run -- pytest` green and
  `uv run -- cdk synth -c image_uri=<any ecr uri>` working without Docker.
- Prose in docs and comments: no em-dashes or en-dashes, no second person.
- The page renders plain text only: no Markdown parser, no `innerHTML` with model or user text.
- Tests replace `guppi_agent.agent.build_strands_agent`; nothing in `agent/tests` reaches
  Bedrock or the gateway.
- Observability code is never behind a feature flag. `web/features.json` and the
  OpenFeature provider in `web/src/features.js` gate product features only
  (`docs/proposals/feature-flags.md`).

## Dependency Management

```sh
uv sync --all-packages --dev        # install all workspace members and dev tools
uv add --package guppi-agent httpx  # add a dependency to one member
uv run -- pytest                    # run all tests
uv run -- pytest agent/tests -v
uv run -- ruff check .
cd web && npm ci && npm run build   # bundle the page into web/dist/
cd web && npm test                  # node:test coverage for the feature flag overlay
```

Flipping a feature flag: edit `web/features.json`, then run `scripts/deploy.sh --site-only`
to rebuild the page and republish `web/dist/config.json` with the new defaults; no `cdk
deploy` and no CloudFormation parameter (`docs/proposals/feature-flags.md`).

- Always use `uv add --package <member>` for dependencies, not manual pyproject edits.
- Run `uv sync --all-packages --dev` after pulling changes.

## Deploying

`scripts/deploy.sh` reads the Google OAuth client id and secret from 1Password
(`op://Personal/GuppiGPT Google OAuth/...`, an API Credential item whose `username` is the client id and `credential` is the client secret), runs `cdk deploy` with them as parameters,
builds the page (`npm ci`, `npm run build` in `web/`), writes `web/dist/config.json` from the stack outputs, syncs `web/dist/` to the site bucket, and
invalidates CloudFront. When `op whoami` fails the script omits both parameters and
CloudFormation reuses the stack's existing values. `GUPPI_ALARM_EMAIL`, when set, becomes
the `AlarmEmail` parameter and subscribes that address to the alarm topic. `scripts/deploy.sh --site-only` skips `cdk deploy` (so no image build or push) and
publishes the page from the last outputs file. Every run is
also written to `.deploy/deploy-<timestamp>.log` with `.deploy/latest.log` pointing at the
newest and a final `deploy exit=<code>` line, so a Claude session can watch a deploy
started from any terminal. Deploys run on Sam's Mac; the Docker image is built there for
arm64.

The runtime's request header allowlist names `Authorization` and `traceparent`; without
the first the runtime validates the bearer and drops it, and the agent has no token for
the tools gateway; without the second the trace the page started ends at the runtime.
The edge gateway target's allowed request headers name the session id header and
`traceparent` for the same reason. The runtime container receives `TOOLS_GATEWAY_URL`,
`MODEL_ID`, `RETRIEVE_TOOL`, `LOG_LEVEL`, and `OTEL_PYTHON_EXCLUDED_URLS` from the stack;
the container starts under `opentelemetry-instrument` (`agent/Dockerfile`), and the
runtime supplies the ADOT exporter settings itself. `docs/proposals/traceability.md`
describes the identifiers and where each one is logged. The web ACL on the edge gateway keeps all three rules in COUNT
until `WAF_BLOCK` in `stack.py` is flipped after real traffic has been watched. The billing
alarm reads `AWS/Billing EstimatedCharges`, which exists only after billing alerts are
enabled in the account's billing preferences (a console setting, not in the stack).

The knowledge base corpus is refreshed by `scripts/seed-content.sh` (three docs repositories
at revisions pinned in the script, Markdown only, `aws s3 sync --delete` to `docs/<source>/`
in the content bucket) followed by `scripts/ingest.sh`. A scheduler runs the same ingestion
nightly. The tools gateway target is named `docs`, so the MCP tools are `docs___Retrieve` and
`docs___AgenticRetrieveStream`.

Two context keys exist for experiments and default off: `-c bind_runtime_to_gateway=true`
adds `allowedWorkloadConfiguration` to the runtime authorizer, and
`-c target_credentials=GATEWAY_IAM_ROLE` makes the gateway sign requests to the runtime
instead of passing the user token through. Neither works against the deployed JWT runtime
today; the decision log records why.

## Local Development

```sh
cd agent && uv run -- uvicorn guppi_agent.app:app --port 8080 --reload
curl -N -X POST localhost:8080/invocations -H 'content-type: application/json' \
  -d '{"threadId":"t","runId":"r","messages":[{"id":"1","role":"user","content":"hello"}]}'
```
