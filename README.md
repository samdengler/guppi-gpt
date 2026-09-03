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
| `web/` | The static page |
| `scripts/` | `deploy.sh`, `seed-content.sh` (refresh the knowledge base corpus), `ingest.sh` (index it) |

## Prerequisites

* AWS credentials for the account that holds the `dengler.io` hosted zone, region `us-east-1`
* [uv](https://docs.astral.sh/uv/), Node 22 (for the CDK CLI via `npx`), a Docker daemon for the arm64 image build (Colima with the `docker` CLI works; `colima start` before deploying)
* [1Password CLI](https://developer.1password.com/docs/cli/) signed in, holding the item
  `GuppiGPT Google OAuth` in the `Personal` vault as an API Credential (`username` is the client id, `credential` is the client secret)
* `jq` and the AWS CLI

## Commands

```sh
uv sync --all-packages --dev # install everything
uv run -- pytest             # agent and stack tests
scripts/deploy.sh            # cdk deploy with secrets read from 1Password, then sync web/
scripts/deploy.sh --hotswap  # any extra arguments go to cdk deploy
scripts/seed-content.sh      # clone the three docs repositories and sync Markdown to the content bucket
scripts/ingest.sh            # start one ingestion job and wait for it
```

## Status

The streaming path is deployed and proven: DNS, certificates, Cognito with Google
federation, the runtime with a hello-world AG-UI agent, the edge gateway, and CloudFront.
The knowledge base is deployed: a content bucket, a managed knowledge base with an S3
connector, a nightly ingestion schedule, and the tools gateway exposing `Retrieve` and
`AgenticRetrieveStream` as MCP tools behind the same Cognito JWT. Still to come: the Strands
agent that calls the tools gateway, the final page, WAF, and the billing alarm.
