# Dynatrace RUM, trace export, and the dashboard (backlog items 1 and 2)

Everything in this change is shipped dark: a flag off by default, and three
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
- listens for the `guppi:feedback` DOM event (`docs/proposals/feedback.md`) and reports
  it as a RUM custom action named `reply-feedback`, with `vote`, `runId`, `traceId`,
  `requestId`, and `threadId` as its properties, using `dtrum.enterAction`,
  `dtrum.addActionProperties`, and `dtrum.leaveAction`
- calls `dtrum.identifyUser` with a SHA-256 hash of the Cognito `sub` claim, only when
  `config.rum.identifyUser` is `true` (default `false`)

The property-building functions (`buildFeedbackActionProperties`,
`buildFlagSessionProperty`) and the activation gate (`rumActive`) are pure and covered by
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
content into top-level fields once the Firehose forwarding below is set up; this was not
verified against a real tenant. The feedback tile queries the `reply-feedback` custom
action's `vote` property; its Grail table name is the least certain query in the file.
The WAF tile and the 403 loopback tile depend on the AWS integration (below) delivering
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
4. The AWS integration, set up from Dynatrace's own AWS integration page: it generates a
   CloudFormation template that creates an IAM role Dynatrace assumes to read CloudWatch
   metrics from the account. This is external to this stack; the template is Dynatrace's,
   deployed once, separately, not part of `GuppiGpt`.
5. Firehose log forwarding: a Kinesis Data Firehose delivery stream subscribed to the
   CloudWatch log groups this design already produces (`runtime-logs` for the agent's
   per-run record, and, once the log deliveries `docs/proposals/traceability.md`
   recommends exist, the gateways' vended logs), writing to Dynatrace's log ingest
   endpoint. Also external to this stack; AWS's own guide for stream-based CloudWatch
   Logs to Dynatrace forwarding covers the Firehose HTTP endpoint destination
   configuration.

## The flip procedure, in order

1. Complete the five steps above in Dynatrace: tenant, RUM application (note its beacon
   origin), API token (three scopes), the AWS integration role, Firehose log forwarding.
2. Save the RUM application's manually-injected JavaScript as
   `web/vendor/ruxitagentjs.js` (gitignored; not committed).
3. Create the `GuppiGPT Dynatrace` item in the Personal 1Password vault, an API
   Credential item, `hostname` set to the OTLP base endpoint
   (`https://<tenant>.live.dynatrace.com/api/v2/otlp`, no trailing slash, no `/v1/traces`
   suffix) and `credential` set to the API token from step 1.
4. Set `GUPPI_DYNATRACE_BEACON_ORIGIN` to the RUM application's beacon origin (for
   example `https://bfxxxxxx.bf.dynatrace.com`) in the shell that runs `scripts/deploy.sh`.
5. Run `scripts/deploy.sh`. This reads the OTLP endpoint and token from 1Password
   (`DynatraceOtlpEndpoint`, `DynatraceApiToken`), passes `DynatraceBeaconOrigin` from
   the environment variable above, copies `web/vendor/ruxitagentjs.js` into
   `web/dist/dt/ruxitagentjs.js` before the sync, and writes `config.rum` into
   `config.json` from the stack's `RumScriptPath` and `RumBeaconOrigin` outputs. The page
   still behaves exactly as before, since the `rum` flag is still off.
6. Edit `web/features.json`, set `"rum": true`, run `scripts/deploy.sh --site-only`
   (`docs/proposals/feature-flags.md`'s flip procedure) to publish the flag flip alone.
7. Verify: load the page, confirm `window.dtrum` is defined in the browser console,
   confirm a RUM session appears in Dynatrace within a few minutes, send a message and
   vote on the reply, and confirm a `reply-feedback` custom action appears with the
   expected `vote`, `runId`, and `traceId`. Send a turn and check whether spans still
   reach CloudWatch Transaction Search for that trace id, since step 5 also changed
   where the runtime exports traces (see below); if CloudWatch tracing stopped, that
   confirms the platform's own OTLP settings do not coexist with this stack's, which the
   next section covers.
8. Import `docs/dynatrace/dashboard.json` as a starting point for a Dynatrace dashboard,
   adjusting the wrapper and the two flagged queries (feedback ratio, WAF and loopback
   counts) against what Grail actually returns for this tenant.

## What was not possible to confirm

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
path in "What Sam has to create in Dynatrace first," not through OTLP.

**The dashboard's exact schema and two of its queries.** Covered above, under "The
dashboard."

## What is left out

- The AWS integration role and the Firehose log forwarding: described above, not built.
  Both live outside `GuppiGpt`, in Dynatrace's own CloudFormation template and a
  Firehose stack of their own.
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
