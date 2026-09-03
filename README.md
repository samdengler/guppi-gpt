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
| `scripts/` | `deploy.sh` and helpers |

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
```

## Status

Spike stage. The stack deploys the streaming path only: DNS, certificates, Cognito with
Google federation, the runtime with a hello-world AG-UI agent, the edge gateway, and
CloudFront. The knowledge base, tools gateway, WAF, and alarms come after the spike passes.
