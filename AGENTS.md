# GuppiGPT Agent Instructions

## Project Overview

GuppiGPT is a minimal claude.ai style chat: one page, stateless plain text conversation,
no history, no attachments, Google sign-in, and answers grounded in a Bedrock Knowledge
Base. The design document (`guppigpt-design.html`) and the decision log
(`guppigpt-decision-log.html`) in `~/Documents/Claude/Projects/GuppiGPT` are the source of
truth for architecture; this repository implements them. When code and design disagree,
fix one of them in the same change.

Guppi is Bob's ship AI from *We Are Legion (We Are Bob)*.

## Tech Stack

- Infrastructure: AWS CDK v2 in Python, one stack `GuppiGpt`, region `us-east-1`
- Agent: Python 3.12, FastAPI, AG-UI over SSE, Strands Agents (after the spike), arm64 container on AgentCore Runtime
- Edge: CloudFront in front of an AgentCore Gateway runtime target; Cognito user pool federated to Google
- Page: static HTML and vanilla JavaScript in `web/`, served from S3 through CloudFront
- Package manager: uv workspace (`infra` and `agent` are members)
- Secrets: 1Password CLI at deploy time; nothing secret is checked in

## Project Structure

```
infra/
  app.py                  # CDK app entry
  guppi_gpt_infra/stack.py
  tests/                  # assertions against the synthesized template
agent/
  src/guppi_agent/app.py  # FastAPI app: POST /invocations (SSE), GET /ping
  Dockerfile              # arm64, uvicorn on 8080
  tests/
web/
  index.html              # the page; reads config.json written by deploy.sh
scripts/
  deploy.sh
  seed-content.sh         # clone the docs repositories at pinned revisions, sync Markdown to S3
  ingest.sh               # StartIngestionJob and wait
```

## Rules

- No Lambda functions anywhere in the request path or the content sync path.
- Secrets never enter files, `cdk.context.json`, or `-c` context values; they are
  CloudFormation parameters with `no_echo` supplied by `scripts/deploy.sh` from 1Password.
- Every change to the stack must keep `uv run -- pytest` green and
  `uv run -- cdk synth -c image_uri=<any ecr uri>` working without Docker.
- Prose in docs and comments: no em-dashes or en-dashes, no second person.

## Dependency Management

```sh
uv sync --all-packages --dev        # install all workspace members and dev tools
uv add --package guppi-agent httpx  # add a dependency to one member
uv run -- pytest                    # run all tests
uv run -- pytest agent/tests -v
uv run -- ruff check .
```

- Always use `uv add --package <member>` for dependencies, not manual pyproject edits.
- Run `uv sync --all-packages --dev` after pulling changes.

## Deploying

`scripts/deploy.sh` reads the Google OAuth client id and secret from 1Password
(`op://Personal/GuppiGPT Google OAuth/...`, an API Credential item whose `username` is the client id and `credential` is the client secret), runs `cdk deploy` with them as parameters,
writes `web/config.json` from the stack outputs, syncs `web/` to the site bucket, and
invalidates CloudFront. Deploys run on Sam's Mac; the Docker image is built there for arm64.

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
