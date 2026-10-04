# GuppiGPT

A one page, plain text chat behind Google sign-in. The page is served from CloudFront,
the stream runs through an AgentCore Gateway to a Strands agent on AgentCore Runtime, and
the agent reads a Bedrock Knowledge Base through a second gateway.

![GuppiGPT runtime architecture](docs/guppigpt-architecture-runtime.png)

The stack is [Amazon Bedrock](https://aws.amazon.com/bedrock/) end to end, with the page
and its state kept deliberately small. In brief:

**Agent and model**

* [AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html)
  hosts the agent container, written with [Strands Agents](https://strandsagents.com/).
* Two [AgentCore Gateways](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html):
  the edge gateway holds the runtime as its target and checks the caller's JWT; the tools
  gateway exposes the knowledge base as an [MCP](https://modelcontextprotocol.io/) tool.
* [Bedrock Knowledge Bases](https://aws.amazon.com/bedrock/knowledge-bases/) holds the
  documentation the agent searches, synced nightly from S3.
* A Claude model answers through a
  [cross-region inference profile](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html).
* The wire format is [AG-UI](https://docs.ag-ui.com/): one HTTP request per turn, a
  server-sent event stream of text deltas, tool events, and run boundaries back, read by
  [`@ag-ui/client`](https://www.npmjs.com/package/@ag-ui/client). See the design's
  [wire format section](docs/guppigpt-design.html#wire-format).
* No Lambda function in the request path; the only ones in the account belong to
  Dynatrace's own AWS integration stack.

**Sign-in and state**

* Google sign-in through an [Amazon Cognito](https://aws.amazon.com/cognito/) user pool,
  with [PKCE](https://datatracker.ietf.org/doc/html/rfc7636) in the browser. The user's
  token travels every hop up to the tools gateway.
* The session persists across reloads through a rotated refresh token in IndexedDB
  (design section [request flow](docs/guppigpt-design.html#request-flow)).
* Chat history is browser-local and behind a flag
  ([proposal](docs/proposals/local-history.md)).
* Feature flags are an [OpenFeature](https://openfeature.dev/) provider over a committed
  JSON file, [`web/features.json`](web/features.json), with browser-wide overrides from a
  URL-only settings page at `/flags.html` ([proposal](docs/proposals/feature-flags.md)).
* Each conversation is written to a private S3 bucket for thirty days under a keyed
  pseudonym rather than the account id, readable only through a dedicated investigator
  role ([proposal](docs/proposals/conversation-logging.md)).

**Observability and guards**

* Never behind a flag. The container exports [OpenTelemetry](https://opentelemetry.io/)
  spans, the gateways and runtime ship vended logs, and a thumbs up or down on a reply
  becomes an [EventBridge](https://aws.amazon.com/eventbridge/) event through a REST API.
* All of it lands in a [Dynatrace](https://www.dynatrace.com/) tenant alongside RUM from
  the page, with one dashboard ([`docs/dynatrace/dashboard.json`](docs/dynatrace/dashboard.json)).
  The trace path, the log path, and the correlation story are in the design's
  [observability section](docs/guppigpt-design.html#observability).
* CloudWatch alarms on the gateways, the runtime, the model, billing, and the feedback
  queue ([proposal](docs/proposals/operations.md)), and [AWS WAF](https://aws.amazon.com/waf/)
  in front of the edge gateway.

The same host also serves other projects, each from its own repository and stack: a
project page lives at `chat.dengler.io/p/<name>/`, reads a manifest the project publishes
to the site bucket, and talks to the platform agent or to the project's own agent through
the edge gateway. This stack publishes what a project needs as `/guppi/platform/...` SSM
parameters. The contract, the routing, the manifest, and the extension API are in
[`docs/proposals/platform.md`](docs/proposals/platform.md).

## Projects on the platform

| Project | Repository | Page | What it is |
| --- | --- | --- | --- |
| GuppiGPT | this one | `https://chat.dengler.io/` | The platform's own agent: Strands on AgentCore, answering from the documentation knowledge base |
| HR Assistant | [guppi-hr](https://github.com/samdengler/guppi-hr), `connect/` | `https://chat.dengler.io/p/hr/` | An agent project: Amazon Connect's Agentic CX designer as the super-agent, over the Profile, Pay and Travel sub-agents (A2A) and the HR tools (MCP). Until 3 Oct 2026 this was `/p/hr-connect/`, which now redirects here |
| HR Assistant (DIY) | [guppi-hr](https://github.com/samdengler/guppi-hr) | `https://chat.dengler.io/p/hr-diy/` | The same HR assistant with a Strands orchestrator as the super-agent, with every change confirmed before it commits; it was `/p/hr/` until 3 Oct 2026 |
| MCP App Lab | this one, [`examples/mcp-app/`](examples/mcp-app/) | `https://chat.dengler.io/p/mcp-app/` | A tools-only project: an MCP server whose tools return MCP Apps UI, offered by the platform agent |

The two HR projects are two choices of super-agent over one set of sub-agents and tools,
both in guppi-hr. In its main stack the super-agent is code: a Strands orchestrator on
AgentCore Runtime, where one model call routes each turn and the routing policy, the
clarifying question and the confirmation step are Python. In `connect/` it is an Agentic
CX designer canvas in Amazon Connect Customer, built from code with the designer SDK,
where routing and confirmation are canvas nodes and Connect adds voice, messaging
channels and escalation to a person. Both pages run the same four scenarios
(guppi-hr's `docs/demo.md`), so they can be compared turn by turn.

MCP App Lab lives here as an example: it touches nothing outside its folder and deploys its
own stack, the way a project in another repository would.

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
| `examples/mcp-app/` | MCP App Lab, a tools-only project with its own workspace, stack and `AGENTS.md` |
| `scripts/` | `deploy.sh`, `seed-content.sh` (refresh the knowledge base corpus), `ingest.sh` (index it), `test-token.sh` (an access token for scripted checks) |

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
   posts the vote to `/api/feedback`, an HTTP API with a JWT authorizer on the Okta issuer
   (since 4 Oct 2026; a REST API with a Cognito authorizer before) integrates directly with
   EventBridge, and an API destination turns it into a Dynatrace business
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
8. An allow-list for sign-in. Done: invite only, built 3 Oct 2026 with a Cognito pre
   sign-up Lambda, and on Okta since 4 Oct 2026, where the `chat-users` group is the list
   and no Lambda is needed (`docs/proposals/invites.md`).
9. TODO: cut the cold start of the Lambda functions in the request path. Today that is
   `guppi-gpt-obo-issuer` (Python 3.12, arm64, 1024 MB), which every hop of guppi-hr's
   on-behalf-of tokens calls. After a quiet spell its first requests take 1.0 to 1.5 s
   each against about 10 ms warm (4 Oct 2026, 05:46 UTC: four cold instances at once,
   init 118 to 162 ms); that adds a second or more to the first answer of the day
   (guppi-hr L24, L25). Most of it is the first request's `_load`: importing boto3,
   three clients, then eight calls in parallel (SSM, KMS `GetPublicKey`, five Secrets
   Manager reads, Okta's keys) before the KMS `Sign`. Steps, measuring each with the
   Lambda's REPORT lines:
   - time each step of `_load` in the log, to see what the second goes to;
   - make fewer calls: the five client secrets in one secret, the issuer URL and the
     public key as environment values set at deploy, Okta's keys fetched only when a
     token names an unknown key;
   - load at init rather than on the first request, where Lambda gives the full CPU;
   - Lambda SnapStart for Python, which restores a snapshot taken after init;
   - a Rust rewrite (cargo-lambda, the AWS SDK for Rust), which starts in tens of
     milliseconds and imports nothing, but still makes the same network calls. Worth it
     if the import and client setup turn out to be most of the time.
   Keeping instances warm (provisioned concurrency or a schedule) was set aside (Sam,
   4 Oct): the aim is a function that starts fast.
