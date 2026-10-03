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

**The AWS integration role, removed, and log forwarding.** This stack originally created
an IAM role, `GuppiGptDynatraceMonitoring`, gated by two CfnParameters,
`DynatraceAwsAccountId` and `DynatraceExternalId`, for Dynatrace's role-based AWS
monitoring integration. IAM showed the role was never assumed, and Dynatrace's own
integration turned out to be push-based rather than role-based; the role, its two
parameters, its `HasDynatraceAws` condition, and its `DynatraceMonitoringRoleArn` output
were removed from the stack on 7 Sep 2026. "The AWS connection as Dynatrace builds it
now," below, covers what replaced it and what stays in the tenant.

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
4. The AWS integration, connected through Dynatrace's own push-based wizard (Settings,
   Cloud and virtualization, AWS, New connection), which deploys Dynatrace's own
   CloudFormation activation stack outside `GuppiGpt`. See "The AWS connection as
   Dynatrace builds it now," below, for the actual steps this took; the role-based
   description this proposal originally gave for this step was removed from the stack on
   7 Sep 2026.
5. Log forwarding needs nothing further here: it turns on with the OTLP endpoint and API
   token from step 3 above (`DynatraceOtlpEndpoint`, `DynatraceApiToken`), through the
   Firehose stream the stack creates (above). Only the runtime's own log group is a
   follow-up, since a CloudFormation subscription filter cannot target a log group the
   service creates lazily rather than the stack.

## The token in Secrets Manager

Added 2 October 2026. The runtime's environment carried the API token as
`OTEL_EXPORTER_OTLP_TRACES_HEADERS`, and `GetAgentRuntime` returns environment variables in
plain text, so anyone allowed to describe the runtime could read it; a session printed it
that day. The stack now keeps the token in the secret `DynatraceTokenSecret` (its value is
the `no_echo` parameter `DynatraceApiToken`, or "unset" while the parameters are blank),
grants only the runtime role read on it, and gives the runtime `DYNATRACE_TOKEN_SECRET_ARN`
in place of the header. The image starts `python /app/otel_headers.py`, which reads the
secret, sets the header in its own environment (replacing the value AgentCore injects into
the container, as the runtime variable did; keeping it gave 401 on every batch), and execs
the usual
`opentelemetry-instrument uvicorn ...`. The exporter reads its header once at startup, so
the value has to be in the environment before `opentelemetry-instrument` runs. Without the
ARN the launcher execs the command unchanged; a secret it cannot read is reported without
the token and the agent still starts. The launcher is a file beside the package, not part
of `guppi_agent`, because importing the package loads the whole kit. The Firehose
destination's access key and the EventBridge connection stay as they were: neither API
returns the value. guppi-hr made the same change for its orchestrator (its D35).

## The AWS connection as Dynatrace builds it now

The role-based connection this proposal first described (a settings object holding a
role ARN that Dynatrace assumes) turned out to be the older model: the role
`GuppiGptDynatraceMonitoring` was never assumed and no metric arrived. Dynatrace's
current integration is push-based. Its wizard (Settings, Cloud and virtualization, AWS,
New connection) mints two platform tokens and hands over a CloudFormation deployment of
Dynatrace's own activation template
(`https://dynatrace-data-acquisition.s3.amazonaws.com/aws/deployment/cfn/latest/da-aws-activation.yaml`),
which creates a CloudWatch metric stream, Firehose deliveries, and the Lambda functions
that ship inventory and logs. On 7 Sep 2026 that stack was created in the account as
`GuppiGPT-Dynatrace` (monitoring configuration id c916ab3f-9014-396d-97dc-4f8985437047,
region us-east-1, log ingest on, event ingest off, the recommended metric set). It is
Dynatrace's stack, deployed once by hand outside `GuppiGpt`, and it is the one place in
the account where Lambda functions exist; the repository rule about Lambda applies to the
request and content paths, which this stack does not touch. The CDK role and its
`DynatraceAwsAccountId` and `DynatraceExternalId` parameters were removed from the stack
on 7 Sep 2026, since IAM showed `GuppiGptDynatraceMonitoring` had never been assumed (no
`RoleLastUsed`) while the activation stack's own role, `DynatraceMonitoringRole-wfd05358-...`,
is assumed every few minutes. The role-based settings object stays in the tenant: the
push-based configuration references it as its credential entry, and the role ARN it
still carries is unused.

The first stack attempt failed at its last step: the settings token the wizard mints
lacked `extensions:configurations:read`, which the template's report step calls with,
because the wizard's service-user group carries only the Data-Acquisition AWS
Integration policy and that policy omits the scope in this release. The fix was to add
the Admin User policy to that group at the environment scope in Account Management, run
the wizard again, and create the stack with the new tokens. The report step had already
run once by then, so the connection stayed Pending until the monitoring configuration was
updated by hand (a PUT on
`/platform/extensions/v2/extensions/com.dynatrace.extension.da-aws/monitoring-configurations/<id>`
from a signed-in tab, setting `value.aws.automatedDeploymentStatus` to COMPLETE), after
which it showed Healthy. The same object holds the extra namespaces under
`value.aws.namespaces`, entries of the schema type `dynatrace.datasource.aws:namespace`
(`namespace`, `autoDiscoveryEnabled`, `metrics`). Namespace entries with auto discovery
and no metric list were the first attempt on 7 Sep 2026 and delivered nothing after an
hour of healthy polling; the connection's Manage panel showed why. Its "Ingest any AWS
metrics" switch was off, and the panel expects one row per metric (namespace, metric
name, dimension names, statistics from Sum, Minimum, Maximum, and SampleCount, unit).
The connection now carries explicit rows, each with `type` `CUSTOM_AWS`:
`AWS/Bedrock-AgentCore` WafBlocks, UserErrors, SystemErrors, Throttles, Invocations, and
Latency on the dimensions Resource, Operation, Protocol, and `AWS/Bedrock` Invocations,
InputTokenCount, OutputTokenCount, and InvocationLatency on ModelId. The built-in
`AWS WAFv2` service was also switched on (feature set `WAFV2_essential`), which covers
the edge gateway's web ACL (a REGIONAL ACL associated with the AgentCore gateway; CloudFront has no web ACL of its own), whose AllowedRequests and BlockedRequests carry Region, Rule, and WebACL dimensions. Polled metrics arrive under keys of the form
`cloud.aws.<service>.<Metric>.By.<Dimension>...`, for example
`cloud.aws.cloudfront.Requests.By.DistributionId.Region`, with the polling source in the
`dt.da.source` field as `aws-metric-poller`; no CloudWatch metric stream exists in the
account. As of 22:30 UTC on 7 Sep 2026, ninety minutes after the rows and the WAFv2
service were saved, no `bedrock`, `waf`, or `apigateway` key had appeared while the
built-in CloudFront, Lambda, SQS, and ECR keys kept arriving every few minutes; the
monitoring configuration's status endpoint answered OK, its deployment status COMPLETE,
and the connection logs held only KMS inventory warnings. Either the poller refreshes its
service list on a long cycle or the custom rows need something the panel does not say;
if the keys are still missing a day later it is a question for Dynatrace support. The
dashboard's two metric tiles keep their guessed keys until the real ones are known.

## The flip procedure, in order

1. Complete the five steps above in Dynatrace: tenant, RUM application (note its beacon
   origin), API token (three scopes), the AWS integration through Dynatrace's own
   push-based wizard (see "The AWS connection as Dynatrace builds it now," below),
   Firehose log forwarding (nothing further needed here).
2. Save the RUM application's manually-injected JavaScript as
   `web/vendor/ruxitagentjs.js` (gitignored; not committed).
3. Create the `GuppiGPT Dynatrace` item in the Personal 1Password vault, an API
   Credential item, `hostname` set to the OTLP base endpoint
   (`https://<tenant>.live.dynatrace.com/api/v2/otlp`, no trailing slash, no `/v1/traces`
   suffix), and `credential` set to the API token from step 1. The item may still carry a
   custom `external_id` field from before 7 Sep 2026; nothing reads it any more, since the
   CDK role that used it was removed from the stack that day.
4. Set `GUPPI_DYNATRACE_BEACON_ORIGIN` to the RUM application's beacon origin (for
   example `https://bfxxxxxx.bf.dynatrace.com`) in the shell that runs
   `scripts/deploy.sh`.
5. Run `scripts/deploy.sh`. This reads the OTLP endpoint and the API token from 1Password
   (`DynatraceOtlpEndpoint`, `DynatraceApiToken`), passes `DynatraceBeaconOrigin` from the
   environment variable above, copies `web/vendor/ruxitagentjs.js` into
   `web/dist/dt/ruxitagentjs.js` before the sync, and writes `config.rum` into
   `config.json` from the stack's `RumScriptPath` and `RumBeaconOrigin` outputs. The page
   still behaves exactly as before, since the `rum` flag is still off; the Firehose stream
   starts working immediately, with nothing on the page to flip.
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
   next section covers. Separately, confirm CloudWatch metrics arrive in Dynatrace
   through the push-based AWS connection (outside this repo) and the vended logs arrive
   through the Firehose stream created in step 5.
8. The dashboard exists in the tenant as "GuppiGPT operations" (document id
   48b747fb-31cf-496c-a05e-cc8dc09e83e1, created 6 Sep 2026 UTC from
   `docs/dynatrace/dashboard.json`, which is now in the platform's exported shape:
   `version`, `variables`, `tiles` keyed by id with `type: "data"`, `layouts`). Re-import
   after edits through the Dashboards app upload or the document API.

## Moving to a new tenant: a runbook Claude follows in Chrome

Written on 8 Sep 2026 for the day the trial tenant (wfd05358, 15 days from 5 Sep 2026)
expires and Sam starts another. Everything tenant specific enters through three doors
(the 1Password item `GuppiGPT Dynatrace`, the `GUPPI_DYNATRACE_BEACON_ORIGIN` variable,
and the gitignored file `web/vendor/ruxitagentjs.js`); the code, the stack, and the
dashboard file are tenant free. The steps below are written for Claude working in a
Chrome tab that is signed in to the new tenant, with the AWS CLI and 1Password CLI in the
shell. Steps marked **Sam** need something only Sam can do; Claude stops and asks at each
one. Secrets never pass through the transcript: a token travels from the tenant page to
the clipboard (`navigator.clipboard.writeText` from the signed-in tab, after a click into
the page and a one time "allow") and from the clipboard into 1Password or a shell variable
with `pbpaste`, never through a tool result. Old data does not move: traces, logs, votes,
and RUM sessions in the old tenant end with it, while the S3 thread records and the
CloudWatch logs are untouched.

### Before the old tenant expires

1. **Sam:** create the new trial and sign in to it in Chrome. Tell Claude the tenant id
   (the `xxx` in `https://xxx.apps.dynatrace.com`).
2. Claude: confirm the tab is signed in by fetching
   `/platform/classic/environment-api/v2/settings/schemas` from it (200 with a schema
   list). The `/platform/storage/query/v1/query:execute` endpoint works from that tab
   only right after a Settings or Notebooks app page has loaded; reload one of those when
   a query answers 403.

### Tear down the old tenant's footprint in AWS

3. Claude: delete Dynatrace's activation stack from the account, since it embeds the old
   tenant's tokens: `aws cloudformation delete-stack --stack-name GuppiGPT-Dynatrace`,
   then wait for `DELETE_COMPLETE`. The log ingest piece is a StackSet instance
   (`StackSet-DynatraceLogIngest-<old tenant>-...`); if it survives the parent, delete the
   StackSet's instances and then the StackSet (`aws cloudformation list-stack-sets`,
   `delete-stack-instances`, `delete-stack-set`). The `GuppiGpt` stack is not touched.
4. Claude: leave the `GuppiGPT Dynatrace` 1Password item in place for now; its values are
   replaced in step 9. Nothing in `GuppiGpt` breaks while the old endpoint is dead: the
   Firehose stream and the feedback API destination park what they cannot deliver, and
   the runtime's exporter drops spans.

### Create what the tenant needs, from the signed-in tab

5. Claude, RUM application: create a web application configured for manual injection
   through the RUM configuration API, `POST
   /platform/classic/environment-api/config/v1/applications/web` with a body naming it
   `GuppiGPT`, `type` `AUTO_INJECTED` replaced by manual insertion (the request body
   the earlier tenant used is not recorded; read the schema from
   `/platform/classic/environment-api/config/v1/applications/web` GET on any existing
   application first, or create it in Settings, Web and mobile monitoring, Applications,
   and switch it to manual insertion). Record the application id
   (`APPLICATION-...`). Then read the beacon origin and the inline script: `GET
   /platform/classic/environment-api/v1/rum/jsInlineScript/<application id>` returns the
   JavaScript; write it to the clipboard from the tab and save it in the shell with
   `pbpaste > web/vendor/ruxitagentjs.js`. The beacon origin is the
   `https://<random>.bf.dynatrace.com` host inside that script (search it for
   `bf.dynatrace.com`); it is not a secret and may be read into the transcript.
6. Claude, RUM session properties: declare one session property per flag name in
   `web/features.json` (`history`, `feedback`, `logging`, `rum`) on the application, as
   settings objects of schema `builtin:rum.web.capture-properties` scoped to the
   application id, so `dtrum.sendSessionProperties` calls are stored rather than
   dropped. Read the schema's current shape from
   `/platform/classic/environment-api/v2/settings/schemas/builtin:rum.web.capture-properties`
   before posting.
7. Claude, ingest token: `POST /platform/classic/environment-api/v2/apiTokens` from the
   tab with `name` `guppigpt-ingest` and scopes `openTelemetryTrace.ingest`,
   `logs.ingest`, `metrics.ingest`, `bizevents.ingest`. The response holds the token
   once; copy `token` to the clipboard from the same script and never return it.
8. Claude, dashboard: from a tab that has the Dashboards app open, `POST
   /platform/document/v1/documents` with FormData fields `name` (`GuppiGPT operations`),
   `type` (`dashboard`), and `content` (the text of `docs/dynatrace/dashboard.json` as a
   JSON blob). Record the new document id in this file, replacing
   48b747fb-31cf-496c-a05e-cc8dc09e83e1 wherever it appears. Later edits are a `PATCH`
   on `/platform/document/v1/documents/<id>?optimistic-locking-version=<version from
   the metadata endpoint>` with the same FormData shape.

### Hand the values to the stack

9. **Sam** (or Claude, when the 1Password CLI is unlocked and `op item edit` is
   permitted): set the `GuppiGPT Dynatrace` item's `hostname` to
   `https://<tenant>.live.dynatrace.com/api/v2/otlp` (no trailing slash) and its
   `credential` to the clipboard contents from step 7, for example
   `pbpaste | op item edit "GuppiGPT Dynatrace" credential=-`. Claude never sees the
   value either way.
10. **Sam:** run the full deploy with the beacon origin from step 5 in the environment:
    `GUPPI_DYNATRACE_BEACON_ORIGIN=https://<random>.bf.dynatrace.com scripts/deploy.sh
    --require-approval never` from the `!` prompt. The classifier blocks Claude from
    running it. Claude follows `.deploy/latest.log`. This redeploys the runtime with the
    new OTLP endpoint, repoints the Firehose stream and the feedback API destination,
    puts the new beacon origin in the CSP, and publishes the new RUM script. The `rum`
    flag is already on, so no site only deploy follows.

### Connect AWS to the new tenant

11. Claude, then **Sam** if the wizard's service user lacks rights: in the new tenant open
    Settings, Collect and capture, Cloud and virtualization, AWS, New connection. The
    wizard mints two platform tokens and hands over Dynatrace's CloudFormation activation
    template. Before running it, give the wizard's service-user group the Admin User
    policy at the environment scope in Account Management (myaccount.dynatrace.com,
    Identity and access management, Groups); on 7 Sep 2026 the report step failed with
    403 `extensions:configurations:read` without it. Account Management is outside the
    tenant tab and may need Sam.
12. Claude: name the connection `GuppiGPT`, region us-east-1, log ingest on, event
    ingest off, the recommended metric set. On the deployment step, copy each token with
    the wizard's copy buttons into shell variables with `pbpaste`, then create the stack
    from the CLI with the template
    `https://dynatrace-data-acquisition.s3.amazonaws.com/aws/deployment/cfn/latest/da-aws-activation.yaml`,
    stack name `GuppiGPT-Dynatrace`, capabilities `CAPABILITY_NAMED_IAM` and
    `CAPABILITY_AUTO_EXPAND`, and the parameters the wizard's deep link carries (tenant
    URL, the two tokens, the monitoring configuration id, log ingest flags, regions).
    The template rejects `pExternalId` even though the deep link carries it. Lambda
    functions in this stack are Dynatrace's own and were approved by Sam on 7 Sep 2026;
    the approval covers the same stack on a new tenant.
13. Claude: wait for `CREATE_COMPLETE`, then check the connection's status in the tenant.
    If it stays Pending after the report step ran, `PUT` the monitoring configuration
    (`/platform/extensions/v2/extensions/com.dynatrace.extension.da-aws/monitoring-configurations/<id>`,
    body `{value: <the GET value with aws.automatedDeploymentStatus set to COMPLETE>}`).
14. Claude: add the metric rows. Either on the connection's Manage panel (switch on
    "Ingest any AWS metrics", one row per metric) or by the same `PUT`, setting
    `value.aws.namespaces` to the two entries recorded under "The AWS connection as
    Dynatrace builds it now" (type `CUSTOM_AWS`, statistics limited to Sum, Minimum,
    Maximum, SampleCount) and adding `WAFV2_essential` to `value.featureSets`.
    Namespace entries with auto discovery alone deliver nothing.
15. Claude: send one chat turn at chat.dengler.io (the namespaces are event driven and
    the poller has nothing to fetch until a request happens), wait ten minutes, and run
    `fetch metric.series | filter startsWith(metric.key, "cloud.aws") | summarize keys =
    collectDistinct(metric.key)`; expect `cloud.aws.bedrock_agentcore.*` and
    `cloud.aws.wafv2.*` keys.

### Verify, then record

16. Claude, in order: `fetch spans | filter service.name == "guppi_gpt.DEFAULT" | limit
    3` (traces); `fetch logs | filter contains(log.source, "guppi-gpt-edge") | limit 3`
    (Firehose logs, after a turn); a vote on the reply and then `fetch bizevents |
    filter event.type == "guppigpt.reply-feedback"` (business events); `window.dtrum`
    defined on the page and a session in the RUM application (RUM); the dashboard
    rendering all eight tiles. If spans do not arrive, the token scope or the hostname
    in 1Password is wrong; if logs do not, the Firehose stream's destination is, which
    the deploy derives from the same hostname.
17. Claude: update this file (tenant id, application id, beacon origin, document id,
    dates), the design document's section 12 where it names the tenant, and the
    memory notes; commit and push. Delete the old tenant's token from nowhere: it
    expires with the tenant.

### The move to zfr04910, 2 October 2026

Tenant `wfd05358` expired; its OTLP path answered 404 even without a token, and every
span batch from the runtime and guppi-hr's orchestrator was refused. Sam started tenant
`zfr04910` (signed up as `samdengler+dt4@gmail.com`). Done: step 3 (the `GuppiGPT-Dynatrace`
stack deleted, its log ingest StackSet with it), a new ingest token `guppigpt-ingest`
with the four scopes, step 9 (the 1Password item now names
`https://zfr04910.live.dynatrace.com/api/v2/otlp`; the OTLP endpoint answered 200 to the
token), and step 10 for this repository and guppi-hr. Both runtimes export to
`zfr04910`, and guppi-hr's orchestrator turns since show no export errors.

Not done: RUM (steps 5 and 6; `web/vendor/ruxitagentjs.js` and the beacon origin still
name the old tenant, so RUM beacons go nowhere), the dashboard (step 8) and the AWS
connection (steps 11 to 15). This tenant breaks the runbook's method: each app runs in a
cross-origin frame, so the Chrome extension's clicks, element search and scripts do not
reach it, and calls from the outer page carry the shell's OAuth scopes (the token API
answered 403 "missing required scope"; the classic config API answered "404 Api Gateway
error" and put the shell on its error page). Sam made the token by hand. The remaining
steps need the same, or an API path that takes a platform token with the right scopes.

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
