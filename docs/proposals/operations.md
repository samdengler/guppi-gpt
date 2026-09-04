# Alarms, log delivery, and per-user rate limits

Design section 15 lists three remaining items: alarms on gateway 4xx rate, runtime 5xx,
and Bedrock throttling; log delivery for the two gateways and the runtime; and per-user
rate limits on the edge gateway keyed on the JWT `sub` claim. This change adds all three
to `infra/guppi_gpt_infra/stack.py`. None of it has been deployed; `infra/tests/test_stack.py`
checks the synthesized template only.

## Alarms added to the alarm topic

Every alarm below follows the pattern the existing WAF alarms already use: the
`AWS/Bedrock-AgentCore` namespace, a five minute period, one evaluation period, missing
data treated as not breaching, and the alarm topic as the only action. The gateway
devguide states that gateway invocation metrics carry a `Resource` dimension holding the
resource's ARN ([gateway observability
data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-gateway-metrics.html)).
The runtime devguide lists the same metric names (`Invocations`, `Throttles`,
`SystemErrors`, `UserErrors`, `Latency`) without a dimensions table
([runtime observability
data](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-runtime-metrics.html)),
so using `Resource` for the runtime alarms too is carried over by analogy rather than
confirmed in writing; it should be checked against real metric data after the first
deploy, the same way the WAF alarms' `GatewayId` dimension is flagged as an assumption in
`stack.py` already.

| Alarm | Metric | Dimension | Threshold | Why |
| --- | --- | --- | --- | --- |
| `EdgeGateway5xxAlarm` | `SystemErrors`, Sum | `Resource` = edge gateway ARN | at least 1 | Crossed-zero pattern, same as the existing WAF alarms; at the traffic this app sees, any 5xx from the gateway is worth a look |
| `ToolsGateway5xxAlarm` | `SystemErrors`, Sum | `Resource` = tools gateway ARN | at least 1 | Same reasoning as the edge gateway |
| `Runtime5xxAlarm` | `SystemErrors`, Sum | `Resource` = runtime ARN | at least 1 | Same reasoning; a runtime 5xx usually means the container failed rather than the caller |
| `EdgeGateway4xxRateAlarm` | Math expression `(UserErrors / Invocations) * 100` | `Resource` = edge gateway ARN, on both input metrics | at least 10 percent | WAF stays in `COUNT` (`WAF_BLOCK` in `stack.py`), so a 4xx here comes from the gateway's own JWT check or a malformed request, not the web ACL. Ten percent is a starting point picked before any real traffic has been watched; a raw count was rejected because occasional 4xx (an expired access token, thirty minutes into a session) is expected and only a rate says whether it is worth attention |
| `RuntimeLatencyP90Alarm` | `Latency`, p90 | `Resource` = runtime ARN | 30000 ms | The CloudFront behavior for `/api/*` gives the gateway origin sixty seconds before it gives up (`ORIGIN_RESPONSE_TIMEOUT` in `stack.py`). Half of that is the point past which a run is already close to the point CloudFront would cut it off, which gives warning before the origin timeout actually fires rather than after |
| `BedrockThrottlingAlarm` | `AWS/Bedrock` `InvocationThrottles`, Sum | `ModelId` = `MODEL_ID` (the inference profile id) | at least 1 | Crossed-zero pattern; a throttle on the one inference profile the runtime calls usually means its quota needs raising |

Runtime `Latency` is documented as the total elapsed time from receiving the request to
sending the final response token, the same quantity the gateway table calls `Duration`;
the runtime has no separate first-token latency metric to alarm on.

## Log delivery for the two gateways and the runtime

Each of the edge gateway, the tools gateway, and the runtime gets one CloudWatch Logs
group:

| Resource | Log group | Log types delivered |
| --- | --- | --- |
| Edge gateway | `/aws/vendedlogs/bedrock-agentcore/guppi-gpt-edge` | `APPLICATION_LOGS` |
| Tools gateway | `/aws/vendedlogs/bedrock-agentcore/guppi-gpt-tools` | `APPLICATION_LOGS` |
| Runtime | `/aws/vendedlogs/bedrock-agentcore/guppi_gpt` | `APPLICATION_LOGS` |

Each log group has 30 day retention. Both log types are supported delivery sources for
"Amazon Bedrock AgentCore Gateway" and "Amazon Bedrock AgentCore Runtime" in the
CloudWatch Logs vended log destinations table, and both can target a CloudWatch Logs
destination (the `CWL` delivery destination type), not only the `XRAY` destination type an
AWS sample script for enabling gateway observability happens to use for traces. That
combination (`TRACES` delivered to a `CWL` destination for a gateway) was tried on
4 Sep 2026 and CloudFormation rejected it: "Invalid destination type provided for this
resource and log type". The gateways' `TRACES` deliveries were removed; gateway traces
would need an `XRAY` delivery destination, which is a follow-up, and the runtime's spans
already reach Transaction Search.

Each resource, log type pair is wired as a CDK L1 triple: `AWS::Logs::DeliverySource`
(named after the resource and the log type, `ResourceArn` set to the resource's own ARN),
one `AWS::Logs::DeliveryDestination` per resource (`DeliveryDestinationType` `CWL`,
pointing at the log group), and one `AWS::Logs::Delivery` per source, pairing it with the
resource's destination
([`AWS::Logs::DeliverySource`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-logs-deliverysource.html),
[log delivery configuration
guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html#observability-configure-cloudwatch)).
The delivery source's `Name` is a plain string, not a CloudFormation reference, so CDK has
no token to infer a dependency from; `stack.py` adds an explicit dependency from each
`Delivery` to its `DeliverySource` and `DeliveryDestination` rather than relying on
implicit ordering.

A CloudWatch Logs resource policy is required for delivery to work: without it, only a
deploying principal that already holds `logs:PutResourcePolicy` on the log group gets one
created automatically the first time delivery starts, which the role `cdk deploy` runs as
is not guaranteed to have
([logs sent to CloudWatch Logs, V2
permissions](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/AWS-logs-infrastructure-V2-CloudWatchLogs.html)).
`stack.py` adds one `AWS::Logs::ResourcePolicy` granting the `delivery.logs.amazonaws.com`
service principal `logs:CreateLogStream` and `logs:PutLogEvents` on a
`/aws/vendedlogs/bedrock-agentcore/*` resource prefix, following the AWS-recommended
prefix pattern rather than one statement per log group, so a fourth vended log
destination under the same prefix needs no policy change.

## Querying each log group

Gateway log records carry `request_id`, `trace_id`, and `span_id`, with the request or
response body nested under `body` (`observability-gateway-metrics.html`, sample log). A
CloudWatch Logs Insights query against the edge gateway's log group, filtered to one trace:

```
fields @timestamp, request_id, span_id, body.log, body.isError
| filter trace_id = "<trace id>"
| sort @timestamp asc
```

The tools gateway's log group holds the same shape; the equivalent query there finds the
`Retrieve` calls a run made:

```
fields @timestamp, request_id, body.requestBody, body.responseBody
| filter trace_id = "<trace id>"
| sort @timestamp asc
```

The runtime's `APPLICATION_LOGS` records carry `request_id`, `session_id`, `trace_id`,
`span_id`, `operation`, and the request and response payloads
(`observability-runtime-metrics.html`, application log data). A query by session, which is
what the page's own per-run log record does not carry:

```
fields @timestamp, request_id, trace_id, operation, response_payload
| filter session_id = "<session id>"
| sort @timestamp asc
```

## Per-user rate limits: what exists and what was built

The investigation started from `CfnGateway`'s properties. One property looked like a
candidate, `policy_engine_configuration` (CloudFormation
`GatewayPolicyEngineConfigurationProperty`), but it takes only an `arn` and a `mode`; it
points at a separately provisioned Cedar-style policy engine resource and has nothing to
do with throughput. The boto3 service model for `bedrock-agentcore-control` told the
real story: `CreateGatewayRateLimit`, `GetGatewayRateLimit`, `UpdateGatewayRateLimit`,
`DeleteGatewayRateLimit`, `ListGatewayRateLimits`, and `BatchPutGatewayRateLimits` all
exist, alongside `LimitEntry` and `RateConfig` shapes that map directly onto "requests per
minute per user" and "concurrent connections per user." CloudFormation exposes the same
thing as a full resource type, `AWS::BedrockAgentCore::GatewayRateLimit`, and the
installed CDK has an L1 for it, `CfnGatewayRateLimit`. Rate limits are therefore
implemented directly in `stack.py`, with no post-deploy script needed.

A rate limit has an ordered list of dimension keys and a list of entries mapping
dimension values to rates. The dimension key for a JWT claim is the literal string
`$.context.jwt.<claim>`; `$.context.jwt.sub` is the one this stack uses. An entry's
dimension value can be `*`, which gives every distinct value of that dimension (every
signed-in user, in this case) its own independent bucket at the configured rate, rather
than one bucket shared by everyone
([rate limit
dimensions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits-dimensions.html)).
`stack.py` adds one `EdgeGatewayPerUserRateLimit` resource with `dimensionKeys` set to
`["$.context.jwt.sub"]` and a single entry: 30 requests per minute and 2 concurrent
connections, both keyed on `*`. Requests and connections share the entry because
`CreateGatewayRateLimit` refuses two rate limits on the same gateway with the same
dimension keys, and there is only one dimension worth keying on here.

Rate limits use fail-open behavior: if the rate limit service is unavailable, or a
dimension cannot be resolved from the request, the gateway allows the request through
([add rate limits to a
gateway](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-rate-limits.html)).
They are therefore one more layer alongside the WAF rate-based rule and the billing alarm,
not a replacement for either, which matches how design section 11 already frames the
control.

## Follow-ups

- Confirm the `Resource` dimension assumption for the three runtime alarms against real
  CloudWatch metric data once the runtime has taken traffic; the gateway devguide states
  it outright, the runtime devguide does not.
- Deliver the gateways' `TRACES` log type to an `XRAY` delivery destination, the only
  destination type the service accepted for it, if gateway spans are wanted beside the
  runtime's.
- Retune `EdgeGateway4xxRateAlarm`'s ten percent threshold and `RuntimeLatencyP90Alarm`'s
  30000 ms threshold once real traffic has been watched, the same posture the WAF alarms
  and `WAF_BLOCK` already take in `stack.py`.
- The connections rate limit (2 per user) is expressed as a `RateConfig` with a
  `period`, even though "concurrent connections" is a ceiling rather than a rate over
  time; confirm the deployed behavior matches that reading once real sessions can be
  tested against it.
- Vended log delivery adds CloudWatch Logs ingestion and storage on top of what
  `docs/proposals/traceability.md` already estimated for container spans; at the traffic
  this app sees today the added volume should be small, and the billing alarm bounds the
  surprise either way.
- CloudWatch Transaction Search is owned by the stack since 3 Sep 2026 (see
  `docs/proposals/traceability.md`); the `APPLICATION_LOGS` deliveries added here are
  independent of it.
