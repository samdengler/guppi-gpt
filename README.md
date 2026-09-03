# GuppiGPT

A one page, stateless, plain text chat behind Google sign-in. The page is served from
CloudFront, the stream runs through an AgentCore Gateway to a Strands agent on AgentCore
Runtime, and the agent reads a Bedrock Knowledge Base through a second gateway.

The design document and decision log live in the Claude project folder
(`~/Documents/Claude/Projects/GuppiGPT`). This repository holds the implementation.

## Layout

| Path | Contents |
| --- | --- |
| `infra/` | AWS CDK app (Python), one stack named `GuppiGpt` |
| `agent/` | The agent container: FastAPI serving AG-UI over SSE on the AgentCore Runtime contract |
| `web/` | The static page: sources in `src/`, esbuild bundle in `dist/` |
| `scripts/` | `deploy.sh`, `seed-content.sh` (refresh the knowledge base corpus), `ingest.sh` (index it) |

## Prerequisites

* AWS credentials for the account that holds the `dengler.io` hosted zone, region `us-east-1`
* [uv](https://docs.astral.sh/uv/), Node 22 or later (for the CDK CLI via `npx` and the page build), a Docker daemon for the arm64 image build (Colima with the `docker` CLI works; `colima start` before deploying)
* [1Password CLI](https://developer.1password.com/docs/cli/) signed in, holding the item
  `GuppiGPT Google OAuth` in the `Personal` vault as an API Credential (`username` is the client id, `credential` is the client secret)
* `jq` and the AWS CLI

## Commands

```sh
uv sync --all-packages --dev # install everything
uv run -- pytest             # agent and stack tests
scripts/deploy.sh            # cdk deploy with secrets read from 1Password, build and sync the page
scripts/deploy.sh --hotswap  # any extra arguments go to cdk deploy
scripts/seed-content.sh      # clone the three docs repositories and sync Markdown to the content bucket
scripts/ingest.sh            # start one ingestion job and wait for it
```

## Status

Design steps 1 to 7 are implemented. The streaming path (DNS, certificates, Cognito with
Google federation, the runtime, the edge gateway, CloudFront) and the knowledge base
(content bucket, managed knowledge base, nightly ingestion, tools gateway) are deployed
and verified. The Strands agent, the final page, the web ACL, the alarms, and the
Content Security Policy are deployed and were verified in the browser on 3 Sep 2026: a
question about MCP transports ran one retrieval and streamed a plain text answer in about
six seconds, and a follow-up turn kept the context.

Billing alerts were enabled in the account's billing preferences on 3 Sep 2026, so the
estimated charges metric will exist. Flipping `WAF_BLOCK` in the stack stays manual, once
the rules have been watched in COUNT.

TODO: the alarm topic's email subscription for the address passed as `AlarmEmail` is stuck
in PendingConfirmation. SNS sent two confirmation requests on 3 Sep 2026 and neither
reached Gmail, spam included. Until one is confirmed no alarm delivers anywhere. Options:
confirm from the SNS console (Subscriptions, Request confirmation), or subscribe a
different address.
