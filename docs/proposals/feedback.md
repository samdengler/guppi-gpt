# Up/down feedback on a reply (backlog item 4)

This proposal covers the thumbs up/down control on each reply: two small buttons under
a committed answer, an event contract nothing subscribes to yet, and a mapping onto
Dynatrace RUM for when backlog item 1 (Dynatrace RUM on the page) lands. Sam's stated
preference is AWS native services for anything on the backend; this feature sets that
preference aside for the reasons below and captures the signal in the browser instead,
with no new AWS infrastructure.

## The control

Two buttons sit under a reply, after it has finished streaming: `▲` for a good reply,
`▼` for a bad one, plain Unicode triangles rather than icons, matching the page's plain
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
