# Dynatrace RUM, trace export, and the dashboard (backlog items 1 and 2)

Everything in this change is shipped dark: a flag off by default, and five
CloudFormation parameters that default to an empty string. Sam has no Dynatrace tenant
yet, so nothing here depends on a real value. Once a tenant exists, turning Dynatrace on
is parameters plus one flag flip, in the order set out below.

## What is built

**The page.** `web/src/rum.js`, new, behind a `rum` flag in `web/features.json`
(`docs/proposals/feature-flags.md`). `initRum(flags, config)` is a no-op unless the flag
is on and `config.rum.scriptPath` is set; otherwise nothing about the feature reaches the
DOM, the same pattern `feedback` and `history` already use. When active it inserts a
`<script>` element pointing at `config.rum.scriptPath`, a same-origin path (the script is
self-hosted from the site bucket, so the CSP's `script-src 'self'` does not need to
change), and once `window.dtrum` exists:

- registers an OpenFeature hook that reports each flag evaluation as a RUM session
  property (`{flagName: "true"|"false"}`, since `sendSessionProperties` has no boolean
  property type)
- calls `dtrum.identifyUser` with a SHA-256 hash of the Cognito `sub` claim, only when
  `config.rum.identifyUser` is `true` (default `false`)

It also listened for the `guppi:feedback` DOM event and reported it as a RUM custom
action named `reply-feedback`, using `dtrum.enterAction`, `dtrum.addActionProperties`, and
`dtrum.leaveAction`. That listener was removed on 5 September 2026, once the tenant was
found to store nothing from the classic JavaScript API's custom actions under its new RUM
experience (recorded under "What the Dynatrace tenant records" in
`docs/proposals/feedback.md`). A vote now goes to Dynatrace as a business event through a
REST API and EventBridge, described in that proposal; nothing on the RUM path carries it.

The property-building function (`buildFlagSessionProperty`) and the activation gate
(`rumActive`) are pure and covered by
`web/test/rum.test.mjs`; the DOM and `dtrum` calls that wrap them are not, the same split
`feedback.js` uses. `web/src/app.js` calls `initRum(flags, config)` right after
`initFeatures` resolves and before it reads `isEnabled("history")` or
`isEnabled("feedback")`, so the OpenFeature hook is registered in time to see those two
evaluations; the hook itself stays inert until `window.dtrum` exists, so an evaluation
that happens before the script finishes loading is not reported, only the ones after.
`identifyRumUser(config, claims.sub)` is called from `showChat()`, once sign-in
completes.

The `dtrum` methods used here (`enterAction`, `addActionProperties`, `leaveAction`,
`sendSessionProperties`, `identifyUser`) are confirmed against Dynatrace's published
TypeScript declarations, `@dynatrace/dtrum-api-types`
(https://unpkg.com/@dynatrace/dtrum-api-types/dtrum.d.ts). The page named in the original
ask, `docs.dynatrace.com`'s RUM JavaScript API reference, returns 404 as of 3 September
2026; its shortlink (`docs.dynatrace.com/docs/shortlink/api-javascript`) resolves to a
different page, the RUM configuration REST API rather than the browser `dtrum` object.
The type declarations are the same API surface Dynatrace's own community examples use
(`dtrum.enterAction(...)`, `dtrum.addActionProperties(id, ..., { key: value })`,
`dtrum.leaveAction(id)`), so the method names and argument shapes in `rum.js` are not a
guess, but they were not cross-checked against the prose page the task named, since that
page could not be reached.

Bundle size: `web/src/rum.js` adds about 1.4 KB to the minified bundle (265.3 KB, up from
263.9 KB before this change); no new dependency, since it reuses the `@openfeature/web-sdk`
import `features.js` already brings in.

**Config plumbing.** `infra/guppi_gpt_infra/stack.py` adds a stack constant,
`RUM_SCRIPT_PATH = "/dt/ruxitagentjs.js"`, output as `RumScriptPath`, and a
`DynatraceBeaconOrigin` CfnParameter (default empty), output as `RumBeaconOrigin`.
`scripts/deploy.sh` merges both into `config.json`'s `rum` object alongside
`identifyUser: false` (hardcoded off; nothing in this change turns it on).
`config.rum.beaconOrigin` is carried through for reference; nothing in `rum.js` reads it
today, since the injected script carries its own beacon target from the RUM
application's own configuration in Dynatrace, not from a value the page passes in.

**The Content Security Policy.** `DynatraceBeaconOrigin` also controls `connect-src` on
the response headers policy: a `HasDynatraceBeaconOrigin` condition
(`Fn.condition_not(Fn.condition_equals(...))`, the same shape `HasAlarmEmail` already
uses) selects between two full CSP strings with `Fn.condition_if`, wrapped in
`Token.as_string` so the result is usable as CDK's `content_security_policy` string
property. With the parameter blank, the default, the rendered CSP is character for
character what the stack had before this change; `infra/tests/test_stack.py` asserts
both branches of the `Fn::If` from the synthesized template. Uploading the RUM script
itself is not part of this stack: `docs/proposals/feature-flags.md`'s pattern of an
optional `scripts/deploy.sh` step covers it, below.

**Backend trace export.** Two more CfnParameters, `DynatraceOtlpEndpoint` (default
empty) and `DynatraceApiToken` (`no_echo`, default empty), and a `HasDynatraceOtlp`
condition requiring both to be non-empty. When both are set, the runtime's
`EnvironmentVariables` gains `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
(`<endpoint>/v1/traces`) and `OTEL_EXPORTER_OTLP_TRACES_HEADERS`
(`Authorization=Api-Token <token>`, the header format Dynatrace's OTLP ingest
documentation shows). When either parameter is blank, the false branch of each `Fn::If`
is `Aws.NO_VALUE`, which removes the key from the `EnvironmentVariables` map entirely
rather than setting it to an empty string; the container's environment is unchanged from
before this change in the default, dark state. `scripts/deploy.sh` reads both values from
1Password (`op://Personal/GuppiGPT Dynatrace/hostname` and `.../credential`, an API
Credential item in the Personal vault, `op read` with `|| true`, skipped entirely when
the item does not exist), the same style as the Google OAuth client.

What this does not do: export traces to CloudWatch and Dynatrace at the same time. See
"What was not possible to confirm" below.

**The AWS integration role and log forwarding.** Two more CfnParameters,
`DynatraceAwsAccountId` and `DynatraceExternalId` (`no_echo`, both default empty), and a
`HasDynatraceAws` condition requiring both to be non-empty. Under that condition, an IAM
role named `GuppiGptDynatraceMonitoring`, trusted by `arn:aws:iam::<DynatraceAwsAccountId>:root`
with an `sts:ExternalId` condition, carrying Dynatrace's own read-only CloudWatch
monitoring policy: `cloudwatch:GetMetricData`, `cloudwatch:GetMetricStatistics`,
`cloudwatch:ListMetrics`, `sts:GetCallerIdentity`, `tag:GetResources`, `tag:GetTagKeys`,
and about ninety Describe or List actions across the services Dynatrace's AWS monitoring
integration polls, taken from the `MonitoringPolicy` statement in Dynatrace's own
CloudFormation template
(`github.com/dynatrace-oss/cloud-snippets/blob/main/aws/role-based-access/role_based_access_monitored_account_template.yml`),
the template Dynatrace's own AWS integration setup page generates. `wafv2:List*` is
added on top of that list, since WAF is a service this stack uses that is not one of
the roughly ninety services in Dynatrace's own policy. The role's ARN is a stack
output, `DynatraceMonitoringRoleArn`,
present only under the condition. The two pages the original ask named for this policy
(`docs.dynatrace.com/docs/ingest-from/amazon-web-services/aws-platform/set-up-aws-monitoring`
and its `-role` variant) return 404 as of 4 September 2026, the same finding recorded
above for a different Dynatrace page;
`docs.dynatrace.com/docs/ingest-from/amazon-web-services/ingest-telemetry/aws-cloudwatch-metrics`
confirms the external-id trust policy and the `ListMetrics` and `GetMetricData` calls
without itself listing every action.

Log forwarding reuses `DynatraceOtlpEndpoint` and `DynatraceApiToken` rather than adding
a third parameter that names the same tenant again. A `HasDynatraceLogs` condition
(structurally the same two checks as `HasDynatraceOtlp`, kept as its own named condition
since it gates a different resource) turns on a Kinesis Data Firehose delivery stream
with an HTTP endpoint destination named `Dynatrace`, pointed at
`https://<tenant>.live.dynatrace.com/api/v2/logs/ingest/aws_firehose`, the ingest path
Dynatrace's own Firehose forwarding guide names, with the API token as the destination's
access key, GZIP content encoding, and buffering of 1 MiB or 60 seconds: the exact
values `docs.dynatrace.com/docs/ingest-from/amazon-web-services/integrate-with-aws/aws-logs-ingest/lma-stream-logs-with-firehose`
specifies. The tenant's base URL is
recovered from `DynatraceOtlpEndpoint` by splitting off its fixed `/api/v2/otlp` suffix
(`Fn::Split`, `Fn::Select`, `Fn::Join`) rather than asking for a separate
`DynatraceLogsEndpoint` parameter naming the same tenant a second time. Failed
deliveries land in a small S3 bucket of their own, seven day expiry, not the site or
content buckets; a Firehose service role and a CloudWatch Logs-to-Firehose role are
created alongside it. Subscription filters on the three vended log groups
(`docs/proposals/operations.md`) send everything to the stream. The runtime's own log
group (`/aws/bedrock-agentcore/runtimes/guppi_gpt-*`) is created by the service rather
than this stack, so a CloudFormation subscription filter has no stack-owned resource to
target; that group is a follow-up, not something this change forwards.

**The dashboard.** `docs/dynatrace/dashboard.json`, a draft. No Dynatrace tenant exists
to export a real dashboard from, so this is written by hand as the JSON shape the
platform dashboard editor exports (`dashboardMetadata` plus a `tiles` array of DQL query
tiles); the wrapper (bounds, `tileType`, query id) may need adjusting on import, and each
tile carries a `reliesOn` field naming the exact log field, metric, or RUM action
property its query depends on. Eight tiles: runs per hour, first delta latency p50/p90,
run outcome split, retrieval rate, token usage, feedback up/down ratio, WAF counts, and
403 loopback rejections. The first five read the agent's per-run JSON log record
(`agent/src/guppi_agent/app.py`: `run`, `first_delta_ms`, `outcome`, `tool_calls`,
`input_tokens`, `output_tokens`), assuming Dynatrace's log ingest parses that JSON
content into top-level fields once the Firehose forwarding above is set up; this was not
verified against a real tenant. The feedback tile now queries `fetch bizevents` for the
`guppigpt.reply-feedback` business event, since that is where a vote lands
(`docs/proposals/feedback.md`); it queried the RUM custom action until 5 September 2026.
The WAF tile and the 403 loopback tile depend on the AWS integration (above) delivering
CloudWatch metrics into Dynatrace, and the 403 tile is an approximation: nothing in the
stack breaks the loopback-URL rejection out from other 4xx responses on the edge
gateway's front door, so the query counts every 4xx as the closest available proxy.

## What Sam has to create in Dynatrace first

None of this exists yet; the parameters above stay empty until it does.

1. A Dynatrace tenant (SaaS or Managed), and its base URL,
   `https://<tenant>.live.dynatrace.com`.
2. A RUM web application, configured for manual injection rather than automatic
   injection (this page injects the script itself, from the site bucket, rather than
   Dynatrace's OneAgent modifying responses). The application's settings page shows its
   RUM JavaScript for manual insertion and its beacon origin, the endpoint the injected
   script posts session data to.
3. An API token with three scopes: `openTelemetryTrace.ingest` (for the OTLP trace
   export above), `logs.ingest`, and `metrics.ingest` (for the AWS integration below).
4. The AWS integration, started from Dynatrace's own AWS integration setup page: note the
   AWS account id and the external id it generates. These become the
   `DynatraceAwsAccountId` and `DynatraceExternalId` parameters; the stack creates the
   `GuppiGptDynatraceMonitoring` role itself (above), so there is no separate
   CloudFormation template to deploy for this step.
5. Log forwarding needs nothing further here: it turns on with the OTLP endpoint and API
   token from step 3 above (`DynatraceOtlpEndpoint`, `DynatraceApiToken`), through the
   Firehose stream the stack creates (above). Only the runtime's own log group is a
   follow-up, since a CloudFormation subscription filter cannot target a log group the
   service creates lazily rather than the stack.

## CloudWatch namespaces the integration must import

Dynatrace's AWS integration imports its built-in service metrics by default. The metrics this design alarms on live in `AWS/Bedrock-AgentCore` (Invocations, Latency, UserErrors, SystemErrors, Throttles, WafBlocks and the other WAF counters, all observed in the account on 5 Sep 2026) and `AWS/Bedrock` (InvocationThrottles), which are not in that default set. In the Clouds app, edit the AWS connection and add both namespaces as custom metric sources, keying on the gateway and runtime dimensions the metrics carry (Operation, Method, and the resource id). Until that is done, the dashboard's WAF and 4xx tiles show nothing while every span and RUM tile works.

## The flip procedure, in order

1. Complete the five steps above in Dynatrace: tenant, RUM application (note its beacon
   origin), API token (three scopes), the AWS integration setup page (note the AWS
   account id and the external id), Firehose log forwarding (nothing further needed here).
2. Save the RUM application's manually-injected JavaScript as
   `web/vendor/ruxitagentjs.js` (gitignored; not committed).
3. Create the `GuppiGPT Dynatrace` item in the Personal 1Password vault, an API
   Credential item, `hostname` set to the OTLP base endpoint
   (`https://<tenant>.live.dynatrace.com/api/v2/otlp`, no trailing slash, no `/v1/traces`
   suffix), `credential` set to the API token from step 1, and a custom `external_id`
   field set to the external id from the AWS integration setup page.
4. Set `GUPPI_DYNATRACE_BEACON_ORIGIN` to the RUM application's beacon origin (for
   example `https://bfxxxxxx.bf.dynatrace.com`) and `GUPPI_DYNATRACE_AWS_ACCOUNT_ID` to
   the AWS account id from the AWS integration setup page, both in the shell that runs
   `scripts/deploy.sh`.
5. Run `scripts/deploy.sh`. This reads the OTLP endpoint, the API token, and the external
   id from 1Password (`DynatraceOtlpEndpoint`, `DynatraceApiToken`,
   `DynatraceExternalId`), passes `DynatraceBeaconOrigin` and `DynatraceAwsAccountId` from
   the environment variables above, copies `web/vendor/ruxitagentjs.js` into
   `web/dist/dt/ruxitagentjs.js` before the sync, and writes `config.rum` into
   `config.json` from the stack's `RumScriptPath` and `RumBeaconOrigin` outputs. The page
   still behaves exactly as before, since the `rum` flag is still off; the monitoring role
   and the Firehose stream start working immediately, with nothing on the page to flip.
6. Edit `web/features.json`, set `"rum": true`, run `scripts/deploy.sh --site-only`
   (`docs/proposals/feature-flags.md`'s flip procedure) to publish the flag flip alone.
7. Verify: load the page, confirm `window.dtrum` is defined in the browser console,
   confirm a RUM session appears in Dynatrace within a few minutes, and confirm the flag
   session properties are attached to it. A vote no longer appears in RUM: with the
   `feedback` flag on, it appears under `fetch bizevents` instead
   (`docs/proposals/feedback.md`). Send a turn and check whether spans still
   reach CloudWatch Transaction Search for that trace id, since step 5 also changed
   where the runtime exports traces (see below); if CloudWatch tracing stopped, that
   confirms the platform's own OTLP settings do not coexist with this stack's, which the
   next section covers. Separately, confirm CloudWatch metrics and the vended logs
   arrive in Dynatrace, using the monitoring role and Firehose stream created in step 5.
8. Import `docs/dynatrace/dashboard.json` as a starting point for a Dynatrace dashboard,
   adjusting the wrapper and the two flagged queries (feedback ratio, WAF and loopback
   counts) against what Grail actually returns for this tenant.

## What was not possible to confirm

**Observed on 5 Sep 2026.** With the Dynatrace parameters set, the container's environment won: the turn's 19 spans (invocation, agent loop, model call, MCP retrieval, Secrets Manager and S3 calls) appeared in Dynatrace and none reached CloudWatch Transaction Search. The export is redirected, as the paragraph below predicted; dual export needs the second exporter in code.

**Simultaneous export to CloudWatch and Dynatrace.** The runtime supplies its own
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (observed pointing at
`https://xray.<region>.amazonaws.com/v1/traces`, SigV4 signed, no header needed) when
`AGENT_OBSERVABILITY_ENABLED` is set, which is how spans reach CloudWatch Transaction
Search today (`docs/proposals/traceability.md`). The OpenTelemetry SDK's environment
variable scheme carries exactly one endpoint and one header set per signal type;
`OTEL_TRACES_EXPORTER` can name more than one exporter type, but not two instances of the
same OTLP exporter pointed at two different endpoints. Setting
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and `_HEADERS` in this stack's `EnvironmentVariables`
therefore redirects trace export to Dynatrace rather than adding it as a second
destination; whether the container's explicit environment wins over the platform's own
injected value, or the reverse, was not confirmed without a real deploy against a real
Dynatrace tenant, since the platform's injection mechanism is not documented in enough
detail to say for certain which one a container process sees when both are set. Step 7
above is the way to find out. Genuine dual export (CloudWatch and Dynatrace at once)
would need a second `SpanExporter` registered in code, an additional `BatchSpanProcessor`
added to the tracer provider `agent/src/guppi_agent/app.py` or `agent.py` would create
explicitly, since `opentelemetry-instrument` only wires up what its environment
variables ask for. That code change is not part of this proposal; only the parameters
and the environment variable plumbing are built, per the brief for this change.

**OTLP log export.** The agent writes its per-run record with Python's standard library
`logging` module to stdout, not through an OpenTelemetry `LoggerProvider`; there is
nothing today for `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` or `_HEADERS` to act on, so those
two variables are not added. Logs reach Dynatrace only through the Firehose forwarding
path described under "What is built," not through OTLP.

**The dashboard's exact schema and two of its queries.** Covered above, under "The
dashboard."

## What is left out

- A subscription filter on the runtime's own log group
  (`/aws/bedrock-agentcore/runtimes/guppi_gpt-*`): the service creates that group lazily
  rather than this stack, and a CloudFormation subscription filter needs an exact,
  stack-owned log group name. Only the three vended log groups are subscribed.
- A second, code-level span exporter for genuine CloudWatch-and-Dynatrace dual export:
  described above as the alternative once dual export by environment variable proved not
  to be possible with confidence; not built.
- OTLP log export: not applicable today, since the agent does not emit logs through an
  OpenTelemetry `LoggerProvider`.
- `dtrum.identifyUser` stays off by default (`config.rum.identifyUser: false`,
  hardcoded in `scripts/deploy.sh`); nothing in this change turns it on for any visitor.
- Restoring the RUM session or its hook registrations across anything: RUM
  reinitializes on every page load, so there is nothing to restore.
- Uploading the RUM script to the site bucket automatically from a source other than
  `web/vendor/ruxitagentjs.js`: the optional `scripts/deploy.sh` step covers a file
  placed there by hand; nothing fetches it from Dynatrace directly.
