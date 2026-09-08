# GuppiGPT

A one page, plain text chat behind Google sign-in. The page is served from CloudFront,
the stream runs through an AgentCore Gateway to a Strands agent on AgentCore Runtime, and
the agent reads a Bedrock Knowledge Base through a second gateway.

![GuppiGPT runtime architecture](docs/guppigpt-architecture-runtime.png)

The stack is Amazon Bedrock end to end. AgentCore Runtime hosts the agent container, two
AgentCore Gateways front it (one holds the runtime as its target and checks the caller's
JWT, the other exposes the knowledge base as an MCP tool), Bedrock Knowledge Bases holds
the documentation the agent searches, and a Claude model answers through a cross-region
inference profile. The agent is written with Strands and speaks AG-UI: the page opens one
HTTP request per turn and reads a server-sent event stream of text deltas, tool events, and
run boundaries through `@ag-ui/client`, so the same wire format a future rich client would
use is already what the plain page consumes. Sign-in is Google through a Cognito user pool
with PKCE in the browser, and the user's token travels every hop up to the tools gateway.
There is no Lambda function in the request path; the only ones in the account belong to
Dynatrace's own AWS integration stack.

State stays small and mostly in the browser. The sign-in session persists across reloads
through a rotated refresh token in IndexedDB, chat history is a browser-local feature behind
a flag, and the page's feature flags are a committed JSON file with browser-wide overrides
from a URL-only settings page. On the server, each conversation is written to a private S3
bucket for thirty days under a keyed pseudonym rather than the account id, readable only
through a dedicated investigator role. Observability is never behind a flag: the container
exports OpenTelemetry spans, the gateways and runtime ship vended logs, a vote on a reply
becomes an EventBridge event, and all of it lands in a Dynatrace tenant alongside RUM from
the page, with CloudWatch alarms and AWS WAF in front of the gateway as the guards.

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
--site-only`. No `cdk deploy`. A visitor's own override, from `?ff=` or from the
settings page at `/flags.html` (reachable only by URL, linked from nowhere), applies to
every tab of their browser. Details in
[`docs/proposals/feature-flags.md`](docs/proposals/feature-flags.md).

## Status

Deployed and verified in the browser on 3 Sep 2026, reconciled against the account on
7 Sep 2026. Remaining work is listed in the design document, section 16. Operational
notes:

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
  SNS console (Subscriptions, Request confirmation), subscribe an address at another
  provider, or subscribe a phone number.
* Every conversation is written to the conversation log bucket as one versioned object per
  thread, under a keyed hash of the account, and expires 30 days after its last write. The
  investigator role is the only principal outside the runtime that can read it; the
  re-identification procedure is in `docs/proposals/conversation-logging.md`.
* The site also serves `/privacy.html` and `/terms.html`, added 7 Sep 2026, because Google's
  OAuth consent screen required public privacy and terms URLs before it could be published; with them in place it went to production on 7 Sep 2026, so any Google account can sign in.
  Both are plain static pages in `web/src/`, copied into `web/dist/` by the same build that
  bundles the page. Content changes to either page go out with `scripts/deploy.sh
  --site-only`, the same as any other page-only change.

## Sign-in session

Since 5 Sep 2026 the page keeps the refresh token in IndexedDB, restores the session with a
silent refresh on load, rotates the refresh token on every use (the app client has rotation
enabled with a 30 second grace period), and clears it on sign out. The access and id tokens
stay in memory. Before this, every reload redirected through Cognito and often to Google's
account chooser.

## Backlog

Preference for anything on the backend: AWS native services, serverless where possible
(scale to zero, pay per use, automatic scaling).

1. Dynatrace RUM on the page plus the Dynatrace AWS connection. Live since 5 Sep 2026 on
   environment wfd05358: RUM application GuppiGPT (self-hosted script, `rum` flag on),
   Firehose log forwarding from the vended log groups, and OTLP trace export from the
   runtime. The connection is the push-based one, deployed 7 Sep 2026 from Dynatrace's own
   activation stack `GuppiGPT-Dynatrace`, with `AWS/Bedrock-AgentCore` and `AWS/Bedrock`
   added as custom namespaces; the role-based model never worked. Setting the Dynatrace
   endpoint redirects trace export, so CloudWatch Transaction Search receives no spans
   (`docs/proposals/dynatrace.md`, design section 12).
2. A Dynatrace dashboard for operational metrics. The dashboard GuppiGPT operations exists
   in the tenant, created from `docs/dynatrace/dashboard.json`; edits are re-imported from
   that file.
3. Correlation ids and traceability. Done: `docs/proposals/traceability.md`.
4. Up/down feedback on each reply. Built and verified 5 Sep 2026; the `feedback` flag went off again on 8 Sep 2026 at Sam's request, so the thumbs are hidden unless a tab turns them on with `?ff=feedback`. The pipeline behind them stays deployed: the page
   posts the vote to `/api/feedback`, a REST API with a Cognito authorizer integrates
   directly with EventBridge, and an API destination turns it into a Dynatrace business
   event (`fetch bizevents | filter event.type == "guppigpt.reply-feedback"`). RUM custom
   actions were tried first and the tenant's new RUM does not ingest them:
   `docs/proposals/feedback.md`.
5. Chat history local to the browser. Built dark behind the `history` flag as Chats:
   `docs/proposals/local-history.md`.
6. Conversation logging to S3, pseudonymous with privileged re-identification. Both
   switches on since 5 Sep 2026, 30 day retention. Decisions in
   `docs/proposals/conversation-logging.md`.
7. Operational alarms, vended log delivery, and the per-user rate limit: done, thresholds
   in `docs/proposals/operations.md`.
8. Low priority: an allow-list for sign-in. Any Google account is admitted today. The
   only place Cognito can refuse a federated first sign-in is a pre sign-up trigger, so
   this is one small Lambda function reading an email list from an SSM parameter, plus a
   callback error branch on the page. Shape and caveats in design section 16; needs
   approval under the Lambda rule before building.
