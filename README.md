# GuppiGPT

A one page, stateless, plain text chat behind Google sign-in. The page is served from
CloudFront, the stream runs through an AgentCore Gateway to a Strands agent on AgentCore
Runtime, and the agent reads a Bedrock Knowledge Base through a second gateway.

![GuppiGPT runtime architecture](docs/guppigpt-architecture-runtime.png)

## Documentation

The architecture, requirements, wire format, security controls, and remaining work are in
the design document. This README covers only how to build and deploy.

| Document | Contents |
| --- | --- |
| [`docs/guppigpt-design.html`](docs/guppigpt-design.html) | The design as it stands: requirements, architecture, request flow, wire format, agent and page design, knowledge base sync, security and cost limits, decisions, next steps |
| [`docs/guppigpt-decision-log.html`](docs/guppigpt-decision-log.html) | Revision history, decisions that were reversed and why, review answers |
| [`docs/guppigpt-architecture.html`](docs/guppigpt-architecture.html) | The architecture diagrams with AWS icons; PNG exports sit beside it |

The HTML documents are self-contained; open them in a browser. When code and design
disagree, fix one of them in the same change.

## Layout

| Path | Contents |
| --- | --- |
| `docs/` | Design document, decision log, architecture diagrams |
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
scripts/deploy.sh --site-only # publish the page only, no cdk deploy or image push
scripts/seed-content.sh      # clone the three docs repositories and sync Markdown to the content bucket
scripts/ingest.sh            # start one ingestion job and wait for it
```

Flipping a feature flag: edit `web/features.json`, then run `scripts/deploy.sh
--site-only`. No `cdk deploy`. Details in
[`docs/proposals/feature-flags.md`](docs/proposals/feature-flags.md).

## Status

Deployed and verified in the browser on 3 Sep 2026. Remaining work is listed in the
design document, section 15. Operational notes:

* `WAF_BLOCK` in `infra/guppi_gpt_infra/stack.py` has been `True` since 4 Sep 2026, after a
  day in COUNT produced no counts on any rule. Set it back to `False` to return to
  watching; a direct call to the gateway hostname now gets a 403 from the WAF.
* The edge gateway's front door answers 403 from its load balancer, before any of the
  stack's WAF rules run, for any request body containing an http or https URL whose host
  is localhost, 127.0.0.1, or 169.254.169.254. Because the page resends the whole thread,
  one such URL in an earlier message ends the conversation. The system prompt asks the
  model for `<server-url>` placeholders, the page explains the 403 without offering Retry,
  and New chat is the way out.
* The alarm topic's email subscription for the address passed as `AlarmEmail` is stuck in
  PendingConfirmation: SNS sent two confirmation requests on 3 Sep 2026 and neither reached
  Gmail, spam included. Until one is confirmed no alarm delivers anywhere. Confirm from the
  SNS console (Subscriptions, Request confirmation) or subscribe a different address.

## Backlog

Preference for anything on the backend: AWS native services, serverless where possible
(scale to zero, pay per use, automatic scaling).

1. Dynatrace RUM on the page plus the Dynatrace AWS integration. Live since 5 Sep 2026 on
   environment wfd05358: RUM application GuppiGPT (self-hosted script, `rum` flag on), the
   monitoring role assumed by Dynatrace, Firehose log forwarding from the vended log
   groups, and OTLP trace export from the runtime. Whether CloudWatch still receives
   spans alongside Dynatrace is the open check (`docs/proposals/dynatrace.md`).
2. A Dynatrace dashboard for operational metrics. Draft in `docs/dynatrace/dashboard.json`;
   import and adjust its two flagged queries.
3. Correlation ids and traceability. Done: `docs/proposals/traceability.md`.
4. Up/down feedback on each reply. Built dark behind the `feedback` flag as a DOM event for
   the RUM hook: `docs/proposals/feedback.md`.
5. Chat history local to the browser. Built dark behind the `history` flag as Chats:
   `docs/proposals/local-history.md`.
6. Conversation logging to S3, anonymous with privileged re-identification. Runtime
   switch on since 5 Sep 2026 with 30 day retention (a proof of concept); the page
   `logging` flag follows once the first thread object is seen. Decisions in
   `docs/proposals/conversation-logging.md`.
7. Operational alarms, vended log delivery, and the per-user rate limit: done, thresholds
   in `docs/proposals/operations.md`.
