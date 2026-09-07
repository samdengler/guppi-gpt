# Up/down feedback on a reply (backlog item 4)

This proposal covers the thumbs up/down control on each reply: two small buttons under
a committed answer, an event contract nothing subscribes to yet, and a mapping onto
Dynatrace RUM for when backlog item 1 (Dynatrace RUM on the page) lands. Sam's stated
preference is AWS native services for anything on the backend; this feature sets that
preference aside for the reasons below and captures the signal in the browser instead,
with no new AWS infrastructure.

The RUM mapping did not survive contact with the tenant, and the sink that was built is
neither the RUM action nor the trace. The sections run in the order the work happened;
"What the Dynatrace tenant records" and "The pipeline as built", at the end, are what is
true today.

## The control

Two buttons sit under a reply, after it has finished streaming: `▲` for a good reply,
`▼` for a bad one, plain inline SVG thumbs rather than icons, matching the page's plain
text rendering rule. Both are muted (`--muted`) until hovered (`--fg`) or chosen
(`--accent`), reusing the color tokens already in `web/src/app.css` rather than adding
new ones. `aria-pressed` on each button reflects whether it is the chosen one;
`aria-label` reads "Good reply" and "Bad reply".

A second click on the already-chosen button withdraws the vote: both buttons return to
unpressed and the reply's `data-feedback` attribute is removed. Clicking the other
button replaces the vote. This toggle is `nextVote` in `web/src/feedback.js`, pure and
covered by `web/test/feedback.test.mjs`.

The control appears only after `RUN_FINISHED`, on a reply that reached its end without
interruption. `web/src/app.js` builds it in the same place it already pushes the
finished assistant message onto the thread and calls `persistCurrentThread`, right
before setting `status = "idle"`. Streaming text never carries the control, and an
interrupted reply (the one that shows "The reply was interrupted." and a Retry link)
never does either, since that path returns before reaching the success tail. A Retry
that succeeds reaches the same success tail and gets the control on its own attempt.

The control renders only when the `feedback` flag is on
(`docs/proposals/feature-flags.md`), read once at load into a `feedbackEnabled`
constant the same way `historyEnabled` already works. With the flag off, `app.js` never
calls `renderFeedbackControls`, so nothing about the feature reaches the DOM, the
bundle size aside.

## The sink and the event contract

`web/src/feedback.js` exports `recordFeedback({ replyEl, threadId, runId, traceId,
requestId, messageId, vote })`. Given a vote (`"up"`, `"down"`, or `null` for a
withdrawn vote) it does three things, all local to the browser:

1. Stamps `data-feedback="up"` or `data-feedback="down"` on the reply element, or
   removes the attribute for a withdrawn vote. This mirrors `markReply` in `app.js`,
   which already stamps `data-run-id`, `data-trace-id`, and `data-request-id` on the
   same element (`docs/proposals/traceability.md`); a reply's data attributes now carry
   the vote alongside the identifiers that name the turn.
2. Dispatches a `CustomEvent` named `guppi:feedback` on `document`, with a detail object
   of exactly `{ threadId, runId, traceId, requestId, messageId, vote }` (fields not
   supplied normalize to `null`). This is the sink: a plain DOM event, nothing sent
   anywhere by this change. `buildFeedbackDetail`, the pure function that shapes this
   object, is what `web/test/feedback.test.mjs` checks, since a live `document` is not
   available under `node:test`.
3. When the `history` flag is also on and the current thread already has a stored
   record, saves the vote onto that message via `setMessageFeedback(threadId,
   messageId, vote)`, a new export in `web/src/history.js`. It is a no-op when the
   thread was never persisted, so a vote never creates a partial history record on its
   own. `app.js` also listens for `guppi:feedback` to keep its own in-memory copy of the
   thread in sync, so a vote is not lost if the next message resends the whole thread to
   `history.js`.

Nothing here is a network call. The event is the integration point: whatever attaches
next reads `guppi:feedback` off `document` and decides what to do with the detail. The
control and the sink are built now; the captured, reviewable signal this backlog item
is really asking for depends on a subscriber, described next.

## The Dynatrace RUM mapping

Once Dynatrace RUM is on the page (backlog item 1), a hook attaches a `document`
listener for `guppi:feedback` and reports it as a **custom action** named
`reply-feedback`, with `vote`, `runId`, and `traceId` as its properties.

A custom action was chosen over a session property. A session property holds one value
per name for the whole RUM session; a person can vote on more than one reply in a
session, including changing their mind on the same reply, and a session property would
keep only the last value, losing every vote before it. A custom action is a discrete,
timestamped record, so Dynatrace keeps one per vote (including a withdrawal, reported
as `vote: null`), and a session's feedback history is the list of its `reply-feedback`
actions rather than one overwritten field.

The mapping needs no change to `web/src/feedback.js`: the hook subscribes to the same
`guppi:feedback` event every other future subscriber would, and reads `vote`, `runId`,
and `traceId` straight off the detail object described above.

### Feedback rate, joined to traces

`runId` and `traceId` are the same identifiers the reply element already carries and
that `docs/proposals/traceability.md` documents end to end: the trace id is the one
that reaches the runtime, the tools gateway, and the Bedrock call, all under a W3C trace
context minted by the page. A `reply-feedback` action's `traceId` property is the same
value, so a bad vote can be followed straight into CloudWatch Transaction Search (once
transaction search and the log deliveries in that proposal are turned on) to see the
tool calls and the model response behind the reply that was voted down, without a
separate correlation step.

With the action landing in Dynatrace, the questions this backlog item exists to answer
become queries rather than new plumbing: feedback rate (votes divided by replies shown)
split by whether the visitor had the `feedback` flag on, split by model once more than
one `MODEL_ID` is in use, or filtered to the traces that also show a tool call. None of
that needs a new store; it is what a RUM custom action already gives for free once it
carries the run and trace ids.

## The AWS-native alternative, and why it is set aside for now

The repository's stated preference is AWS native services, serverless where possible.
The natural AWS-native shape for this signal is an S3 object per vote, written either
through the agent (a new tool call, or a field on the existing per-run log record) or
through a small API the page calls directly (API Gateway in front of a Lambda, or a
Fargate service, writing to S3 or a table).

Both routes add infrastructure for a single low-volume signal at a stage where
Dynatrace RUM is already a nearer-term backlog item (item 1) that will put the same
identifiers in front of Sam regardless of this feature. Routing the vote through the
agent means a browser event becomes a network call the agent has to authenticate,
validate, and log, on a path the design otherwise keeps stateless and reply-focused.
A dedicated API is its own stack addition: an endpoint, an IAM role, a storage target,
and a CORS policy, for a boolean vote and three identifiers already available in the
browser's document. Neither is ruled out permanently; if a future need does not fit
Dynatrace (a durable, queryable store the account owns outright, or a signal needed
before RUM lands), the same `guppi:feedback` event is where that sink would attach,
built the same way this proposal builds the DOM stamp and the history write: a listener
on one event, no change to the control itself.

## What is left out

- The Dynatrace hook itself. It cannot be written before Dynatrace RUM is on the page;
  this proposal describes the mapping it would use.
- Restoring the control on a reply loaded from local chat history. `web/src/history.js`
  stores a vote on the message record (`setMessageFeedback`) and `switchToThread` keeps
  it in the in-memory thread so a later save does not drop it, but `hydrateThread` does
  not redraw the buttons for a resumed conversation, since a resumed reply's element
  never had `data-run-id` or `data-trace-id` stamped on it in the first place (those are
  per-run identifiers, not stored with the thread). The vote itself is not lost; only
  the control's visible state is not rebuilt.
- Any display of the vote, or an aggregate count, back to the person who cast it. The
  control shows which choice is pressed for the current page load and nothing else.
- Rate limiting or deduplication of repeated votes. A vote is idempotent by
  construction (the toggle only has two states plus none), so nothing further is
  needed.
- The AWS-native alternative described above, beyond naming it and the event it would
  attach to.

## Files changed

- `web/src/feedback.js`: new. `nextVote`, `buildFeedbackDetail`, `recordFeedback`,
  `renderFeedbackControls`.
- `web/src/history.js`: `setMessageFeedback(threadId, messageId, vote)`, and the stored
  message shape's comment updated to note the optional `feedback` field.
- `web/src/app.js`: a `feedbackEnabled` constant read once from `isEnabled("feedback")`;
  a `guppi:feedback` listener that keeps the in-memory thread's messages in sync;
  `persistCurrentThread` and `switchToThread` carry the `feedback` field through instead
  of dropping it; the success tail of `runTurn` calls `renderFeedbackControls` once,
  gated on the flag.
- `web/src/app.css`: `.feedback-controls` and `.feedback-btn`, reusing the existing
  color tokens.
- `web/test/feedback.test.mjs`: `node:test` coverage for `nextVote` and
  `buildFeedbackDetail`, the two functions that need no DOM.
- `AGENTS.md`: `feedback.js` added to the project structure listing.

## Testing

`cd web && npm ci && npm test` passes, 16 tests (the 9 already covering
`flags-core.js` plus 7 new ones for `feedback.js`). `npm run build` completes with no
errors; the minified bundle grew from 261.9 KB to 263.9 KB, about 2 KB, all of it
`feedback.js` and the small wiring in `app.js` (no new dependency). `node --check`
passes on every changed JavaScript file. A pass over `app.js` against `index.html` found
no id referenced by one without being declared in the other; this feature adds no static
markup, since the control is built the same way `addTurn` already builds a reply
element, with `document.createElement`.

Manual verification, done with both flags on (`web/features.json`'s `feedback` and
`history` set to `true`, or `?ff=feedback,history` on the URL for one tab):

1. Build the page, serve `web/dist/` locally, sign in, send a message.
2. Confirm no buttons appear until the reply finishes streaming.
3. Click the up triangle: it turns to `--accent`, `aria-pressed="true"`, the reply
   element gains `data-feedback="up"` (checked in the DOM inspector), and a
   `guppi:feedback` listener registered in the console
   (`document.addEventListener("guppi:feedback", console.log)` before sending) logs a
   detail with `vote: "up"` and the same `runId`/`traceId` the reply's data attributes
   show.
4. Click the up triangle again: it returns to muted, `aria-pressed="false"`,
   `data-feedback` is removed, and the logged detail shows `vote: null`.
5. Click the down triangle: only it is highlighted, `data-feedback="down"`.
6. Open DevTools' IndexedDB inspector (`guppigpt-history` → `threads`) and confirm the
   assistant message in the current thread carries `"feedback":"down"`.
7. Send a second message in the same thread and confirm the vote is still present in
   IndexedDB afterward (checks that `persistCurrentThread` did not drop it).
8. With the `feedback` flag left off (default), confirm no buttons ever appear under a
   reply and `document.body.dataset.features` does not list `feedback`.

## What the Dynatrace tenant records, observed 5 Sep 2026

The tenant wfd05358 runs Dynatrace's new RUM experience (Grail, schema 0.24.0) with RUM
Classic also enabled on the application. The page's calls succeed on the agent side: the
agent (1.345.3) returns the action id and reports every property as sent, and the beacons
reach the beacon origin with 200. Nothing from those calls is stored. Grail's `user.events`
holds page views, resources, and the automatically detected clicks, with `user_action.name`
set only on the clicks; no event carries the `reply-feedback` name, the `vote` property, or
the flag session properties. RUM Classic's user session query returns no user actions at
all for the same hour, so the classic property declarations made through
`builtin:rum.web.capture-properties` govern data that never arrives. The agent exposes only
the classic API surface (44 methods, no custom event call), and no settings schema for
custom events exists in the new experience. Closing the action after a delay
(`e02a4ab`) changed nothing.

The conclusion is that under the new RUM experience the classic JavaScript API's custom
actions and API-reported properties have no ingestion path on this tenant today. The RUM
signal was chosen to avoid new AWS infrastructure; the alternative that keeps that goal is
the trace: the page already carries the run id and trace id on the reply element, and the
agent already emits spans that Dynatrace stores. A vote posted to the runtime as a small
run input (a `forwardedProps.feedback` value on the next turn, or a dedicated tiny run)
would let the agent log it and attach it as a span event or attribute on the trace, where
Dynatrace's distributed tracing and DQL on `fetch spans` find it by trace id. That needs
no new AWS resources and reuses the traceability work. The control and the DOM event stay
as built; only the sink changes.

Sam decided on 5 September 2026 against both the RUM action and the trace: a vote is a
business event, not a turn and not a span, so it does not travel with the chat request
and it is not attached to the trace the turn started. It gets its own path, serverless
and without a Lambda, described next.

## The pipeline as built

The page posts a vote to `/api/feedback` on the existing CloudFront domain. CloudFront forwards the viewer path unchanged under the origin path `/prod`, so the API's resource tree is `/api/feedback` too; a resource at `/feedback` alone answered `/prod/api/feedback` with "Missing Authentication Token" on the first deploy. From there:

**The REST API.** `aws_apigateway.RestApi` named `guppi-gpt-feedback`, regional endpoint,
one stage named `prod`, no CORS configuration (the page is same origin with the API
through CloudFront, so no preflight is ever sent). A REST API rather than an HTTP API,
which is Sam's requirement: the direct AWS service integration below, request validation
against a model, and gateway response templates are all REST API features. The account
level API Gateway CloudWatch role is left alone (`cloud_watch_role=False`), since it is an
account wide setting this stack does not own.

**The authorizer.** `CognitoUserPoolsAuthorizer` on the existing user pool, on the one
`POST /feedback` method. The method also names one authorization scope, `openid`. Without
a scope, API Gateway treats the bearer as an identity token and refuses an access token,
which carries `client_id` rather than `aud` ("Integrate a REST API with an Amazon Cognito
user pool", API Gateway developer guide). The page holds both tokens but sends the access
token everywhere, so naming a scope switches the authorizer to access token validation.
Every token this app client issues claims `openid`, since the page asks for `openid`,
`email`, and `profile` at sign-in, so the scope check passes for every signed-in visitor
and grants nothing finer than the authorizer already does.

**The request validator.** A body-only validator and a JSON schema model: an object with
`vote` (one of `up`, `down`, `none`), `runId` (a UUID), and `threadId` required;
`traceId` (32 hex digits), `requestId`, and `messageId` optional; no additional
properties. A body that fails is a 400 from API Gateway with a JSON message, before the
integration runs.

**The integration.** `AwsIntegration` to `events:PutEvents`, `POST`, with a credentials
role that trusts `apigateway.amazonaws.com` and may put events on the one bus. The two
request parameters are the target header (`AWSEvents.PutEvents`) and the JSON 1.1 content
type the EventBridge API expects. The body mapping template reads each field from the
validated body, escapes it for JSON, and builds one entry whose `Detail` is a string, as
PutEvents requires. One field does not come from the body: `receivedAt`, the epoch
millisecond API Gateway received the request (`$context.requestTimeEpoch`). The caller's
Cognito `sub` claim travelled as `subject` until 7 Sep 2026 and is now left out (see the
follow-ups). A 200 from PutEvents maps to a 202 with an empty body, and a 4xx to a 400.

**The bus and the rule.** An `aws_events.EventBus` named `guppi-gpt-feedback`, with one
rule matching `source: guppigpt.feedback`.

**The API destination.** Under the existing `HasDynatraceLogs` condition, the same
condition that turns on log forwarding, so the two Dynatrace parameters switch both on
together. An `aws_events.Connection` with API key authorization puts the token in the
`Authorization` header as `Api-Token <token>`, and an `aws_events.ApiDestination` posts to
the tenant's business events endpoint, `https://<tenant>.live.dynatrace.com/api/v2/bizevents/ingest`,
derived from `DynatraceOtlpEndpoint` the same way the Firehose logs endpoint is: split off
the fixed `/api/v2/otlp` suffix, append the ingest path. The rule's input transformer
builds the body: `event.type` is `guppigpt.reply-feedback`, `event.provider` is
`guppigpt`, and the vote, `run.id`, `trace.id`, `thread.id`, `message.id`, `request.id`,
and `received_at` follow as flat fields, since Grail stores every top-level attribute of
an ingested event as a top-level field and turns a nested object into a string. The
target retries twice and sends what it cannot deliver to an SQS dead letter queue that
holds it for 14 days. A CloudWatch alarm (`FeedbackDeadLetterAlarm`) on the queue's
visible message count, at least one over five minutes, goes to the alarm topic, since
EventBridge itself reports nothing when a target keeps failing; the alarm sits under the
same `HasDynatraceLogs` condition as the queue.

The ingest facts above come from "Ingest business events via API"
(https://docs.dynatrace.com/docs/observe/business-analytics/ba-api-ingest, which redirects
to `.../observe/business-observability/bo-events-capturing/bo-events-capturing-external-sources`,
read 5 September 2026): the endpoint URL and its POST method, `Content-Type:
application/json` for the pure JSON format, the token attached as `Authorization: Api-Token
<token>` with the Ingest bizevents scope, and the note that pure JSON has no mandatory
fields while `event.type` and `event.provider` are the attributes the guidance asks a
caller to set so its events can be told apart. The payload limit is 5 MB, which one vote
is in no danger of reaching.

**The archive.** An `aws_events.Archive` on the bus, 30 day retention, matching the same
source, created whether or not Dynatrace is configured. Every vote is therefore kept and
replayable even while the tenant details are missing, and a replay refills Dynatrace once
they are supplied. Firehose to S3 is more than a handful of votes a day needs; it is the
upgrade if long term storage is wanted, and the vended log forwarding stream in
`docs/proposals/dynatrace.md` is the shape it would take.

**The CloudFront behavior.** An additional behavior for the exact path `/api/feedback`,
whose origin is the REST API's regional hostname with `/prod` as the origin path, HTTPS
only, all methods allowed, caching disabled, and the same
`AllViewerExceptHostHeader` origin request policy `/api/*` uses, so the `Authorization`
header reaches the API while the `Host` header stays the API's own. CloudFront compares a
request path against behaviors in the order they are listed, so this one is listed before
`/api/*`, which would otherwise send every vote to the edge gateway. The stack builds the
feedback section before the distribution for that reason, and a test asserts the order in
the synthesized template. The origin carries no `X-Origin-Verify` header, unlike the edge
gateway origin: this API authorizes every request itself, so a caller who finds the
`execute-api` hostname is refused by the Cognito authorizer rather than by a WAF rule.

**No Lambda.** Nothing in this path is compute owned by this stack, which the existing
test in `infra/tests/test_stack.py` enforces for the whole template.

### What the page sends

Everything stays behind the `feedback` page flag, which is off. With it on,
`initFeedbackSink` in `web/src/feedback.js` subscribes to the same `guppi:feedback` event
the DOM stamp and the history write already use, and posts:

```json
{"vote": "up", "runId": "...", "threadId": "...", "traceId": "...", "requestId": "...", "messageId": "..."}
```

with `content-type: application/json` and `Authorization: Bearer <access token>`, read
from the same place `runTurn` reads it. A withdrawn vote is `"none"` rather than an absent
field, so a withdrawal is a record of its own. The three optional identifiers are left out
when the page does not have them, since the model accepts no null and no unknown property.
The request is fire and forget: no retry, no reading of the response, nothing written to
the console, `keepalive` so a vote survives a page that is closing, and an abort after
three seconds. `buildFeedbackRequestBody`, the pure function that shapes the body, is what
`web/test/feedback.test.mjs` covers.

### Querying the votes

In Dynatrace, the votes are business events:

```
fetch bizevents
| filter event.type == "guppigpt.reply-feedback"
| summarize count(), by: {vote}
```

`trace.id` on each record is the same trace id the reply element carries and the runtime
logs (`docs/proposals/traceability.md`), so a down vote leads straight to the turn behind
it. Without a Dynatrace tenant, the same votes are on the bus archive and can be replayed
onto the bus.

### Follow-ups

- The subject, closed on 7 Sep 2026 by dropping it. The event carried the raw Cognito
  `sub` at first, because the mapping template has no HMAC and the agent's pseudonym is a
  keyed hash of the same claim (`agent/src/guppi_agent/conversation_log.py`). Computing
  the hash would need the page (which does not hold the key) or a processing step, and a
  raw subject in Dynatrace is what the pseudonym exists to avoid. The event now carries no
  subject at all. A vote still reaches its conversation: `thread.id` on the event is the
  key of the thread record in the conversation log bucket, and that record holds the
  pseudonym, so the join by subject goes through the investigator role like every other
  re-identification. Votes ingested before the change keep their `subject` field in
  Dynatrace until the tenant's retention drops them.
- An S3 archive through Firehose, if votes are ever wanted beyond the 30 days the bus
  archive keeps.
- An `OPTIONS` mock method on the resource, if a preflight ever appears. It cannot today:
  the page and the API share an origin, and a simple POST with `content-type:
  application/json` from another origin would be blocked by the missing CORS headers
  rather than by a preflight.
- A usage plan on the API. Throttling is API Gateway's account-wide default today
  (10,000 requests a second), which is far above anything one page can produce, and the
  Cognito authorizer already bounds who can reach it.
- Reading `FailedEntryCount` from the PutEvents response. The integration answers 202
  whenever PutEvents answers 200, including the case where EventBridge accepted the call
  and rejected the entry. The archive and the dead letter queue are what would show it.

## Verified on 5 Sep 2026

After the resource path fix, a vote cast from the signed-in page answered 202 through CloudFront, and within a minute Dynatrace returned it from `fetch bizevents` with `event.type` guppigpt.reply-feedback, `event.provider` guppigpt, the vote, and the run id the reply element carried. An unauthenticated call through CloudFront gets 403 and a direct call to the execute-api hostname without a token gets 401 from the authorizer. The EventBridge archive count lags by minutes and is not a live check.
