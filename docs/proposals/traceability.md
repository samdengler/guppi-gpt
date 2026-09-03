# One identifier for a whole turn

Backlog item 3. A support question about GuppiGPT arrives as "the answer to my
second question came back empty around 14:10". Today the only way to follow that turn
is by timestamp: the agent's log record, the runtime's own logs, and whatever the
gateways kept are each searched separately, and none of them share a key with the
page. The aim is that one identifier, read off the page or off any single log line,
finds the same turn at every hop: browser, CloudFront, the edge gateway, the AgentCore
runtime, the agent, the tools gateway, and the Bedrock call.

The design stays inside the repository's rules: AWS managed services only, nothing
serverful, and no Lambda function anywhere in the request path.

## Identifiers that exist today

The request path is CloudFront, the edge gateway (an AgentCore Gateway with a runtime
target and token passthrough), the AgentCore runtime, the agent container, the tools
gateway (an MCP gateway in front of the knowledge base), and Bedrock. Each hop already
mints or carries something.

The page (`web/src/app.js`) creates a `threadId` per New chat, a `runId` per send (an
AG-UI run input field), and a runtime session id per page load, sent as the
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header. The `runId` comes back on the
stream in `RUN_STARTED` and `RUN_FINISHED`. None of these were shown on the page or
kept anywhere the page's user could read them.

CloudFront adds `X-Amz-Cf-Id` to every response and forwards all viewer headers to the
gateway origin (the `AllViewerExceptHostHeader` origin request policy). Standard
logging for the distribution is off, so `X-Amz-Cf-Id` has nowhere to be looked up.

The edge gateway answers with `x-amzn-requestid`. AgentCore Gateway vends logs and
spans once a log delivery is configured for the gateway; each log record carries
`request_id`, `trace_id`, and `span_id`, and the `kind:SERVER` span carries
`aws.request.id` and the tool name ([gateway observability
data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-gateway-metrics.html)).
No delivery is configured for either gateway in the stack, so nothing is kept.

The runtime answers with its own `x-amzn-requestid` (shown in the AG-UI contract's
error examples, [AG-UI protocol
contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-agui-protocol-contract.html)).
It creates a log group `/aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>`
with a `runtime-logs` stream for the container's stdout, an `otel-rt-logs` stream for
ADOT structured logs, and a `spans` stream for traces when the agent is instrumented
([view observability
data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-view.html)).
With observability turned on it also emits an `InvokeAgentRuntime` span with
`aws.request_id` and `session.id`, and `APPLICATION_LOGS` records with `request_id`,
`session_id`, `trace_id`, `span_id`, and the request and response payloads ([runtime
observability
data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html)).
Before this change the runtime forwarded only `Authorization` to the container; every
other request header, trace context included, stopped at the runtime.

The agent (`agent/src/guppi_agent/app.py`) writes one JSON record per run to stdout:
hashed subject, session id, thread id, run id, message count, tool calls, token counts,
first delta latency, total latency, outcome. It lands in the `runtime-logs` stream. The
run id is the only key it shares with the page, and the page did not show it.

The tools gateway is reached from the agent over MCP with the user's token. It has the
same vended logs and spans as the edge gateway and, like the edge gateway, no delivery
configured. Its `Retrieve` call to the knowledge base is a Bedrock API call under the
gateway's role, recorded by CloudTrail with the gateway's request id.

The Bedrock model call (`InvokeModelWithResponseStream` from the Strands Bedrock
provider) returns a request id in `ResponseMetadata`. Nothing in the agent logged it,
and model invocation logging is not enabled on the account.

## What was missing

Three gaps. First, no identifier crossed more than one hop: the page's run id reached
the agent's log record and nothing else, the gateways' request ids reached only their
own (unconfigured) logs, and the runtime's request id reached the browser but not the
container. Second, the container emitted no spans and received no trace context, so the
runtime's trace id, the gateways' trace ids, and the Bedrock call were unrelated
records. Third, the account-level and resource-level switches that make the managed
telemetry visible (CloudWatch Transaction Search, tracing on the runtime and gateways,
log deliveries for the gateways) were never turned on; design section 15 lists this as
remaining work.

## The design: a W3C trace context minted by the page

The page starts the trace. For every send (and every Retry, which is a new run) it
mints a `traceparent` header in the W3C form
`00-<32 hex trace id>-<16 hex parent id>-01` and sends it beside the bearer and the
session id. The trace id opens with the epoch seconds so it is also a well-formed X-Ray
trace id (`1-<8 hex seconds>-<24 hex random>`), which is the form CloudWatch shows in
Transaction Search. AgentCore documents `traceparent` as an accepted invocation header
for the runtime ([enhanced runtime observability with custom
headers](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html#observability-configure-invoke)).
The X-Ray form of the header, `X-Amzn-Trace-Id`, was set aside because the runtime's
header allowlist refuses every `x-amzn-` name other than its own custom prefix
([pass custom headers to AgentCore
Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-header-allowlist.html)).

CloudFront passes the header through under the existing origin request policy. The
edge gateway's runtime target names it in `metadata_configuration.allowed_request_headers`
beside the session header, and the runtime names it in `request_header_configuration`
beside `Authorization`. Both allowlists are enforced by the platform: a header missing
from either one is dropped silently, as the `Authorization` header was on the first
deploy (decision log, revision 10). The CloudFormation shapes are documented at
[GatewayTarget
MetadataConfiguration](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-bedrockagentcore-gatewaytarget-metadataconfiguration.html)
and [Runtime
RequestHeaderConfiguration](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-bedrockagentcore-runtime-requestheaderconfiguration.html).

Inside the container, `aws-opentelemetry-distro` (ADOT for Python) does the rest with
no code in the agent. The image starts uvicorn under `opentelemetry-instrument`, the
entrypoint wrapper AgentCore documents for containers ([enabling observability in agent
code](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html#observability-configure-custom)).
The runtime supplies the exporter settings as environment variables
(`AGENT_OBSERVABILITY_ENABLED`, the `OTEL_*` endpoint and resource values), which is
why `DISABLE_ADOT_OBSERVABILITY=true` exists to unset them; the stack adds only
`OTEL_PYTHON_EXCLUDED_URLS=/ping$` so the runtime's health checks produce no spans.
With the distro in place:

- The FastAPI instrumentation opens a server span for `POST /invocations` whose trace
  id is read from the incoming `traceparent`. The distro's propagator order is
  `baggage,xray,tracecontext`, so the same header would also be understood in the
  X-Ray form should a caller ever send that instead.
- With `AGENT_OBSERVABILITY_ENABLED=true` the distro exports every span (no sampling),
  sends the session id from baggage into span attributes as `session.id`, and marks
  the service as `gen_ai_agent` for the CloudWatch GenAI Observability dashboard.
- Strands Agents creates its own spans (agent invocation, model call, tool call)
  through the global tracer provider the wrapper installed, as children of the server
  span.
- The distro's MCP instrumentation spans the tools gateway session and tool calls and
  injects `traceparent` into the outbound MCP requests, so the tools gateway's own
  logs and spans carry the same trace id.
- The botocore instrumentation spans the Bedrock call with the model id and records
  the Bedrock request id in `aws.request_id`.

A local build of the image confirmed the chain as far as the container goes: with a
console exporter, a run sent with a chosen `traceparent` produced the server span, the
MCP session span, and the agent's log record, all carrying the trace id from the
header.

The agent's per-run record gains two fields. `trace_id` is the trace id from the
`traceparent` header (falling back to the active span when the header is absent, or
`-` when neither exists), and `request_id` is the runtime's `x-amzn-requestid` when
the runtime forwards one to the container. The record still names the thread, run,
and session, so a search on any one of them yields the others.

The page keeps the identifiers where support can find them without a screenshot of the
network tab. The reply element of each turn carries `data-run-id`, `data-trace-id`,
and, when the response had one, `data-request-id` (the `x-amzn-requestid` the gateway
answered with; the page is same-origin with the API, so the header is readable).
Nothing is rendered; the attributes are read from the browser inspector.

### Where the telemetry lands

- Spans from the container go to the X-Ray OTLP endpoint and are stored as structured
  logs. In supported Regions a newly created runtime delivers them to the `spans`
  stream of its own log group; older runtimes use the shared `aws/spans` group, and
  `UNIFIED_TRACES_DESTINATION_ENABLED=true` on the runtime opts one in ([span
  destination for agents in
  AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html#observability-configure-unified-traces)).
  The unified destination needs `logs:PutResourcePolicy` on the runtime's execution
  role, which the stack now grants for this runtime's log groups as the documented
  policy shows ([runtime
  permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)).
- CloudWatch Transaction Search is the query surface for spans; the GenAI Observability
  page in CloudWatch shows the same data grouped by session and trace, with the model
  and tool calls broken out.
- The runtime's `APPLICATION_LOGS` and the gateways' vended logs go wherever their log
  deliveries point; this proposal recommends CloudWatch Logs groups under
  `/aws/vendedlogs/bedrock-agentcore/`.
- The agent's own record stays in `runtime-logs`.

### Identifiers, hop by hop

| Identifier | Who mints it | Where it is logged | How to query it |
| --- | --- | --- | --- |
| Trace id (`traceparent`) | The page, one per run | Runtime `spans` stream (all container spans, with `session.id`); runtime `APPLICATION_LOGS` (`trace_id`); both gateways' vended logs and spans (`trace_id`); the agent's record (`trace_id`); the reply element (`data-trace-id`) | Transaction Search by trace id for the timeline; Logs Insights `filter trace_id = "<id>"` across the runtime and gateway log groups; `filter @message like "<id>"` on `runtime-logs` |
| Run id (`runId`) | The page, one per send or Retry | `RUN_STARTED` and `RUN_FINISHED` on the stream; the agent's record (`run`); the reply element (`data-run-id`) | Logs Insights on `runtime-logs`: `filter run = "<id>"`, then pivot to `trace_id` |
| Thread id (`threadId`) | The page, one per New chat | The agent's record (`thread`) | Groups every run of one conversation in `runtime-logs` |
| Runtime session id | The page, one per page load | Runtime `InvokeAgentRuntime` span and `APPLICATION_LOGS` (`session_id`); container spans (`session.id`); the agent's record (`session`); GenAI Observability sessions view | Session view in GenAI Observability; Logs Insights `filter session_id = "<id>"` |
| Gateway request id (`x-amzn-requestid` on the response) | The edge gateway | The gateway's vended logs and `kind:SERVER` span (`request_id`, `aws.request.id`); the reply element (`data-request-id`) | Logs Insights on the gateway's vended log group: `filter request_id = "<id>"`, then pivot to `trace_id` |
| Runtime request id | The runtime | `InvokeAgentRuntime` span (`aws.request_id`), `APPLICATION_LOGS` (`request_id`); the agent's record (`request_id`) when the runtime forwards the header | Logs Insights on the runtime's `APPLICATION_LOGS` delivery, then pivot to `trace_id` |
| Tools gateway request id | The tools gateway, per MCP call | Its vended logs and spans, beside the same `trace_id` | Same as the edge gateway; found from the trace id rather than searched for |
| Bedrock request id | Bedrock | The botocore span's `aws.request_id` in the container's spans; CloudTrail | Read from the trace; quoted to AWS Support when a model call itself is the problem |
| `X-Amz-Cf-Id` | CloudFront | Response header only | Not searchable until CloudFront standard logging is on; out of scope |

The trace id is the one that crosses every hop. The run id is what the page shows and
what the agent logs, and the agent's record joins the two. The support path is: read
`data-run-id` or `data-trace-id` off the reply (or ask for the approximate time and find
the run in `runtime-logs`), search Transaction Search by trace id, and open the gateway
and runtime log groups by the same value if the trace alone does not explain the turn.

### One-time settings outside the stack

Two switches are account-level or resource-level settings rather than stack resources
today.

CloudWatch Transaction Search must be on before spans can be stored or searched, and
before tracing can be turned on for the runtime and gateways. The console path is
Application Signals, Transaction search, Enable, with spans ingested as structured
logs. The API path is a CloudWatch Logs resource policy letting `xray.amazonaws.com`
write to `aws/spans` and `/aws/application-signals/data`, then
`aws xray update-trace-segment-destination --destination CloudWatchLogs` ([getting
started with
observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html)).
Spans take about ten minutes to appear after enabling. Both steps have CloudFormation
resources (`AWS::Logs::ResourcePolicy` and
[`AWS::XRay::TransactionSearchConfig`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-xray-transactionsearchconfig.html),
which sets the indexing percentage), and CDK 2.268 exposes both as L1 constructs, so
the setting moved into `stack.py` on 3 Sep 2026 after the first deploy showed the OTLP
exporter answering 400 for every span batch while the destination was still X-Ray. It
is an account-wide setting that other workloads in the account inherit.

Log delivery for the two gateways and the runtime's `APPLICATION_LOGS` is configured per
resource: a delivery source of type `APPLICATION_LOGS` (and `TRACES` for the gateways),
a CloudWatch Logs delivery destination, and a delivery joining them. The console does
this under each resource's Log delivery pane; the same three objects exist as
[`AWS::Logs::DeliverySource`, `AWS::Logs::DeliveryDestination`, and
`AWS::Logs::Delivery`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-logs-delivery.html).
The recommendation is to add them to the stack for both gateways and the runtime in a
follow-up, with a 30 day retention on the log groups, once the first deploy of this
change has shown what the container's spans look like. Tracing on the runtime and
gateways is a toggle on each resource in the AgentCore console (Tracing pane); the
runtime's toggle is worth confirming after the deploy.

### Dynatrace

Dynatrace is a separate backlog item; only the hand-off matters here. The trace id the
page mints is a W3C trace context, which Dynatrace OneAgent and its OpenTelemetry
ingest read unchanged, so a Dynatrace trace of the same turn would carry the same id
that appears in CloudWatch and on the reply element. Two routes exist: the container
can export spans a second time over OTLP to a Dynatrace endpoint (an
`OTEL_EXPORTER_OTLP_*` pair on the runtime, or, if CloudWatch is to be dropped,
`DISABLE_ADOT_OBSERVABILITY=true` and the OTLP settings in its place), or Dynatrace can
read the CloudWatch log groups and spans through its AWS integration. Either way the
identifiers in the table above are the join keys; nothing in this design would need to
change.

## Costs

The stack itself adds nothing that bills: two header allowlist entries, one environment
variable, one IAM statement, and a larger container image (the distro and its
instrumentation packages add roughly fifty Python packages to the dependency layer,
which Docker caches until the lockfile changes).

Telemetry volume is the cost. Each turn produces the runtime's own span and log record,
the agent's record, a few Strands spans, an MCP session span and a tool call span, a
Bedrock span, and one ASGI `send` span per SSE event the container writes. The last
item dominates: a two hundred token reply is a few hundred small spans. All of it is
stored as CloudWatch Logs and billed at CloudWatch Logs ingestion and storage rates;
the gateways' vended logs bill at the vended logs tier. Transaction Search indexes 1
percent of spans at no cost and charges for a higher indexing percentage. At the
traffic this app sees (tens of turns a day) the whole of it is well under the noise of
the billing alarm, and the WAF and billing alarms already in the stack bound the
surprise. If the `send` spans prove noisy the remedy is to instrument the app in code
with `exclude_spans=["receive", "send"]` on the FastAPI instrumentor, which the
auto-instrumentation has no environment switch for; that is a follow-up once a deploy
has shown real span counts.

There is one operational cost to note: the container exports spans over HTTPS to the
X-Ray and CloudWatch Logs OTLP endpoints on a background thread. Export failures are
logged and do not affect the stream.

## Out of scope

- Enabling Transaction Search, tracing on the runtime and gateways, and the log
  deliveries: recommended above, to be done (and then moved into the stack) after the
  account owner accepts the account-wide setting.
- Dynatrace, beyond the paragraph above.
- CloudFront standard logging and AWS WAF logging. Both would add `X-Amz-Cf-Id` and the
  WAF decision to the searchable set; neither is needed to follow a turn, and both bill
  per request.
- Bedrock model invocation logging, which records prompts and completions. The Bedrock
  request id is already in the trace.
- Alarms on gateway 4xx, runtime 5xx, and Bedrock throttling (design section 15, a
  separate item).
- Showing the identifiers on the page. They are data attributes for support to read,
  and stay out of the rendered text.

## Verification after the first deploy

1. Send one turn and read `data-trace-id` off the reply element.
2. Confirm the same value appears as `trace_id` in the agent's record in `runtime-logs`,
   which shows the header passed both allowlists.
3. With Transaction Search on, search that trace id and confirm the server span, the
   Strands spans, the MCP span, and the Bedrock span appear under one trace, and that
   the trace shows `session.id`.
4. With a log delivery on the tools gateway, confirm its records for the `Retrieve`
   call carry the same `trace_id`, which shows the MCP instrumentation injected the
   context across the thread the MCP client runs in. If it did not, the fix is in the
   agent: attach the request's context in `StrandsRun.run` before `client.start`.
5. Note which request id the browser's `data-request-id` matches: the edge gateway's
   or the runtime's. The proposal assumes the gateway's; the AG-UI contract shows the
   runtime setting one too, and the gateway may forward it.
6. Count spans per turn in the `spans` stream to decide whether the ASGI `send` spans
   should be excluded.
