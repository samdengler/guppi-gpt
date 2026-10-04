# One host, many projects

chat.dengler.io becomes a host that serves many projects. This repository keeps the page,
sign-in, CloudFront, WAF, the edge gateway and observability, deployed once, and publishes
its identifiers as SSM parameters. Each experiment (guppi-mcp-app first) is a separate
repository with its own CDK stack that reads those parameters, adds an MCP server or an
agent Runtime, registers a gateway target, and publishes a manifest and an optional
extension bundle into the shared site bucket. No project changes this stack or the Google
OAuth client.

The page selects a project from its URL path (`chat.dengler.io/p/mcp-app/`) and routes the
project's turns to `/api/<project>/invocations`. MCP Apps host support (sandboxed iframes,
the postMessage bridge) is built once in this page; projects supply servers and UI
resources.

The design with its diagram, alternatives and open questions is the Claude Doc
"Guppi Chat Platform: one host, many projects"
(https://claude.ai/code/artifact/ce4663bd-34fa-4a94-8ffa-135ba08f1ac1). This file is the
part a session needs while building: the contract, the routing rules, the manifest, the
extension API, and what changes here.

## Why

hr-super-agent forked this repository at 8baf911. Its diff is almost entirely renames:
brand strings in twelve web files, hostnames and resource names in a copy of the 2,235
line stack, a second Cognito pool, a second CloudFront distribution, a second WAF, a second
Google redirect URI added by hand, and a copy of `app.py`, `keepalive.py`,
`validation.py` and `conversation_log.py`. The HR project's own code is a small fraction of
the tree. Every experiment would repeat that, and MCP Apps makes it worse, because the
host side (iframes, the bridge, resource reads) belongs in the page and would be
re-implemented in every copy.

## Platform contract

The contract between a project and this stack is a set of SSM parameters plus one Python
package. SSM rather than CloudFormation exports, so a platform deploy is never blocked by a
project that imports a value and a project can be destroyed without touching the platform.

| Parameter (`/guppi/platform/...`) | Value | Used by a project for |
| --- | --- | --- |
| `site-bucket-name` | The site bucket | Publishing `projects/<name>/` |
| `distribution-id` | The CloudFront distribution | Invalidating `/projects/<name>/*` after a publish |
| `site-url` | `https://chat.dengler.io/` | Documentation and smoke tests |
| `edge-gateway-id` | Gateway identifier | Creating a runtime target (`CfnGatewayTarget`) |
| `edge-gateway-arn` | Gateway ARN | Binding a Runtime's JWT authorizer to the gateway |
| `edge-gateway-role-arn` | The gateway's execution role | Attaching an `InvokeAgentRuntime` grant for the project's Runtime |
| `tools-gateway-id` | Tools gateway identifier | Registering an MCP target for the project's server |
| `tools-gateway-url` | Tools gateway `/mcp` URL | The project agent's `TOOLS_GATEWAY_URL` |
| `tools-gateway-role-arn` | The tools gateway's execution role | Attaching an `InvokeAgentRuntime` grant for a project's MCP server Runtime |
| `user-pool-client-id` | Cognito app client id | `allowed_clients` on a project Runtime's JWT authorizer |
| `jwt-discovery-url` | Cognito OIDC discovery URL | `discovery_url` on the same authorizer |
| `conversation-log-bucket-name`, `conversation-log-key-secret-arn` | Log bucket and pseudonym key | Optional: a project agent logs to the same place under the same pseudonym |
| `alarm-topic-arn` | The alarm SNS topic | Optional: project alarms notify the same address |

A project reads them at deploy time with `ssm.StringParameter.value_for_string_parameter`.
Parameter names are constants in `stack.py` beside the outputs; the outputs stay as they are.

The `guppi_agent` package (`app.py` with the AG-UI over SSE contract, `keepalive.py`,
`validation.py`, `conversation_log.py`) becomes installable by git URL from this
repository, with `agent.py` factored so a project supplies its own `build_strands_agent`.
A project agent depends on
`guppi-agent @ git+https://github.com/samdengler/guppi-gpt@<tag>#subdirectory=agent`.

## Request routing

A project is addressed by URL path on the one hostname. Two CloudFront Functions on the
distribution do the mapping; nothing else in the request path changes.

Page path. `https://chat.dengler.io/p/<name>/` is the project's page. A viewer-request
function on the default behavior rewrites `/p/<name>/` and `/p/<name>/index.html` to
`/index.html`, so the same bundle serves every project and the page reads `<name>` from
`location.pathname`. Files under `/projects/<name>/...` are served from the bucket as they
are. The root `/` stays the default project, the Guppi docs agent, unchanged.

Agent path. The edge gateway addresses a runtime target by its name as the first path
segment (`TARGET_NAME = "api"` is why the platform agent answers at `/api/invocations`). A
project's Runtime is a target named `<name>`, reachable on the gateway at
`/<name>/invocations`. A viewer-request function on the existing `/api/*` behavior
rewrites `/api/<name>/invocations` to `/<name>/invocations` and leaves `/api/invocations`
and `/api/feedback` alone. Origin, origin-verify header, WAF rule and per-user rate limits
are inherited. Project names cannot be `invocations` or `feedback`.

Sign-in round trip. The Cognito redirect URI stays `https://chat.dengler.io/`, so the
Google client is never edited for a project. `startSignIn` puts the current path in the
OAuth `state` parameter; `finishSignIn` reads it back after the code exchange and
`history.replaceState`s to that path instead of the root. The stored refresh token and the
silent refresh on load are unchanged, since IndexedDB is per origin.

Tools path, later. When an experiment wants the browser to hold its own MCP connection to
the tools gateway, a `/mcp/*` behavior to the tools gateway is added once, with the same
origin-verify header. The CSP's `connect-src 'self'` already permits it.

## Project manifest

`projects/<name>/manifest.json` is the only thing the page needs from a project. It is
fetched with `cache: "no-store"` at load, like `config.json`.

```json
{
  "name": "mcp-app",
  "label": "MCP App Lab",
  "assistant": "Guppi",
  "agent": "/api/mcp-app/invocations",
  "features": { "history": true, "feedback": false },
  "extension": "/projects/mcp-app/ext.js",
  "capabilities": ["mcp-apps"],
  "mcp": { "url": "/mcp", "toolPrefix": "mcp-app___" }
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Matches the URL path segment and the gateway target name; lowercase letters, digits and hyphens |
| `label` | yes | Brand shown in the header, the sign-in card and the tab title |
| `assistant` | no | The reply label and composer placeholder; defaults to `label` |
| `agent` | yes | Path the AG-UI client posts to; `"platform"` means `/api/invocations`, the Guppi agent, for tools-only projects |
| `features` | no | Booleans merged over `config.features` before `initFeatures` runs; browser overrides from `/flags.html` still win |
| `extension` | no | Same-origin ES module the page imports after the manifest; absent means built-in behavior only |
| `capabilities` | no | Built-in page behaviors to enable: `mcp-apps` (the MCP Apps host renderer) and `warm-start` ("Warm start" below) |
| `mcp` | no | For a browser-side MCP connection: the CloudFront path to the tools gateway and the tool-name prefix this project owns |
| `theme` | no | `{ light: {...}, dark: {...} }` of hex colors for the page's custom properties (`bg`, `fg`, `muted`, `border`, `surface`, `bubbleUser`, `accent`, `accentContrast`, `brand`); applied for the scheme in effect. Without one a project gets the page's default palette, Sky (blue actions on cool white, navy brand, from guppi-mcp-app). The page passes its palette to MCP Apps as the extension's style variables in `hostContext.styles` |
| `suggestions` | no | Up to six `{ "label", "prompt" }` entries shown as pills under the empty state; a click sends the prompt. Plain text; the default project has none |
| `description` | no | One or two plain-text sentences for the project's card on the home page ("Project list" below); trimmed and cut at 200 characters |

The page tolerates unknown fields so a project can carry its own settings for `ext.js`.

## Project list

The home page (`/`) shows a "Try a project" row of cards under its empty state, one per
project, each with the manifest's `label` and `description` and a link to `/p/<name>/`.
Every page's header name opens the same list as a menu, GuppiGPT first and the current
project highlighted, so switching projects is one click from anywhere.
`web/src/projects.json` in this repository names the projects, in display order:

```json
{ "projects": ["hr", "hr-diy", "mcp-app"] }
```

A card's text comes from that project's own manifest, fetched at load like the project
page does, so a project changes its card by publishing its manifest. A name whose
manifest is missing or unusable is left out, and any failure leaves the row hidden.
Adding a project to the row is one line here and a site deploy. A generated index was
considered: keeping it current without touching every project's deploy takes a function
on each manifest upload, which AGENTS.md asks to avoid, and projects are added rarely.

## Warm start

A project whose agent needs time before its first answer (a microVM to start, a contact to
open, a session to set up) lists `warm-start` in `capabilities`. For each new thread, as
soon as the signed-in page is in view (at load, at sign-in, on a new chat, or when a hidden
tab comes into view; not a resumed thread), the page posts a run with no messages and
`forwardedProps.warm: true` to the project's agent path, with the bearer, the runtime
session id and a traceparent its runs use. When the page has just left a thread that may
hold something open (one that was warmed or has messages), the run also carries
`forwardedProps.previousThreadId`, so the agent can release what it held for it. The kit
(kit-v0.3.0) answers the run with `RUN_STARTED` and `RUN_FINISHED` and, when the agent
built for the token has a `warm(run_input)` coroutine, awaits it in between; a failure is a
`RUN_ERROR` with the code `WARM_FAILED`. No thread record is written, and the run log line
carries `"warm": true`.

The page does not wait for it. An agent's `warm` prepares what its runs would otherwise do
on the first message, and a run that arrives while a warm start is still working shares or
waits for that work, so a first message sent at once can wait up to the warm start's
length. A token within five minutes of expiry skips the warm start, so a send's token
refresh is never raced. A thread still empty 50 minutes after its warm start is warmed
again when the employee comes back to it (a focus, a keystroke, a pill press, or the tab
coming into view), since guppi-hr's contact and hop tokens last an hour.

From 3 to 4 October the page warmed on engagement instead (the first focus on the composer,
a keystroke, or pressing a suggestion), after the guppi-hr critique found that every reader
opened a Connect contact, which held a chat and, until the bridge cleared it, the
employee's token. A suggestion is sent by the same press, so its question waited for the
whole warm start: 8.5 s to the first words against 3.7 s once the warm start had run
(guppi-hr D50, L24). The bridge now clears the token after the greeting and ends the
contacts it leaves, so page load is back.

## Connect chat transport

A project whose agent is an Amazon Connect chat can let the page talk to Connect itself
instead of through an agent on AgentCore Runtime (guppi-hr D55, decision 22 in
`docs/guppigpt-decision-log.html`). The manifest lists `connect-chat` in `capabilities`
and carries a `connectChat` block; `web/src/project.js` (`connectChatFor`) checks it and
the page uses the transport when the block has a same-origin `start` route and the
`connect-bridge` flag is off, which is the default.

```json
"connectChat": {
  "start": "/api/hr/chat/start",
  "report": "/api/hr/chat/report",
  "endMark": "\u2063",
  "closedMark": "\u2064",
  "hiddenPrefix": "[flow]",
  "endLine": "[flow] end",
  "closedLine": "[flow] closed",
  "escalationPrefix": "[flow] Escalation",
  "errorPrefix": "[flow] The Agentic CX block returned an error",
  "quietAfterMs": 800,
  "turnLimitMs": 28000,
  "maxChars": 1024,
  "lines": { "tooLong": "...", "noReply": "...", "restarted": "...", "signin": "...",
             "ended": "...", "escalated": "...", "error": "..." }
}
```

The turn rules are the project's data, so the page holds no project's conventions: the
marks, the hidden prefix, the legacy end and closed lines, the escalation and error
prefixes, the limits, and the lines it shows. `start` and `report` must be `/api/...`
paths on the page's origin, since the page sends its bearer there. A mark is one to four
characters, a line plain text up to 500 characters; an unusable field keeps the platform's
default, and an unusable `start` turns the transport off.

The pieces, all in `web/src/`:

- `connect-chat.js`: `classify` and `createTurnAssembler`, pure functions over raw Connect
  items; `createConnectChat`, one amazon-connect-chatjs 5.2.0 customer session
  (`disableCSM: true`, no logger, receipts off with `shouldSendMessageReceipts: false`, no
  typing or receipt events, so no `SendEvent`); and `createConnectChatClient`, the page's
  chats by thread.
- `connect-agent.js`: `ConnectChatAgent`, an `@ag-ui/client` `AbstractAgent` that keeps the
  caller's `abortController` as `HttpAgent` does and sends the bridge's event order:
  `RUN_STARTED`, `STEP_STARTED "Amazon Connect"`, the reply as text messages,
  `connect/<kind>` CUSTOM events for a closing event, `STEP_FINISHED`, `guppi.timing` on a
  debug run, `RUN_FINISHED`; a `ping` CUSTOM event every 15 s while it waits.
- `vendor/chatjs.js`: chatjs as its own ES module (`dist/vendor/chatjs.js`, about 300 KB
  minified), imported only by a page that uses the transport.

### Chat start

The warm start (D50 rules, `warmDue`) posts `{}` or `{ "previousContactId": "..." }` to
`start` with the bearer instead of the bridge's warm run. The project's route answers one
JSON body (guppi-hr D57, the shape of AWS's StartChatContact sample):
`data.startChatResult` (`ContactId`, `ParticipantId`, `ParticipantToken`), `region`,
`startedAt`, `expiresAt`, `restarted` and `timing`, or `{"error":"signin"}` or
`{"error":"unavailable"}`; a 401 is a refused sign-in and a 429 (API Gateway's throttle)
an unavailable start. The route warms no sub-agent. The page reads the body, calls
`setGlobalConfig` with the region, creates the session and connects. A question sent
before then waits for that one start. The participant token stays inside the chat
session's closure: it never reaches the extension host, history, the debug block or RUM.

A new chat names the left chat's contact as `previousContactId`, so the route ends it. A
refused send gets one fresh connection on the same participant, then a new chat. A chat
that ended (`chat.ended`, `participant.left`, the closed mark, an escalation, the
designer's error line) or has fewer than five minutes before `expiresAt` (checked after the
page refreshes its token) is replaced at the next question, which opens with the restart
line. A thread reopened from history starts a new chat and shows the restart line: there is
no server store, so the designer does not have the earlier turns. Sign-out awaits
`disconnectParticipant`, capped at 2 s, before the redirect.

### A turn

The assembler buffers items until `sendMessage` resolves with the message's `Id` and
`AbsoluteTime`, drops the message itself and every item older than it, takes each `Id`
once, and ends the turn on the end mark or end line, on a closing event, after
`quietAfterMs` of quiet that follows a reply without a mark, or at `turnLimitMs` with the
no-reply line when nothing came. Hidden lines are not shown; marks are stripped. Items that
arrive between turns are dropped, except `chat.ended`, which marks the chat ended. A
message over `maxChars` is answered with the too-long line and not sent. After every
`onConnectionEstablished` (it can fire twice, chatjs issues 124 and 298), whenever the
tab comes back into view, and when the window's `online` event says the network is back,
the session reads the transcript (`getTranscript`, newest 100) and feeds it through the
same `Id` check, so a catch-up never shows anything twice. The `online` read matters
because a socket can stay open through an outage (Chromium's offline emulation keeps it)
and then neither reconnects nor delivers what it missed; a read that fails right after the
event is tried once more 2 s later.

A turn that ends at `turnLimitMs` with the no-reply line leaves its question awaiting a
late answer until the next question is sent (or the page switches thread, starts a new
chat or signs out). A reply to it that arrives later, on the socket or from a catch-up,
goes through a late assembler (the same `Id` and time checks, no turn limit, ended by a
mark, a closing event or quiet) and replaces the no-reply line in that same reply, as
plain text, and in the stored thread. Once the next question is sent, a late reply to the
earlier one is dropped, as before.

After each turn the page posts the report to `report` with `fetch(..., { keepalive: true
})`, off the answer's path: `contactId`, `runId`, `threadId`, Connect's `AbsoluteTime` for
the message and the first and last reply items, `endReason` (`end_mark`, `closed`,
`ended`, `quiet`, `no_reply`, `error`, `aborted`), an `error` code, `transport` and the
timing value. Never reply text, never a token. The route's alarm reads `no_reply` as a
turn with no answer, `error` as the designer's error line (with `designer_error`), and an
error code that names the socket as a failed socket, so a turn the transport gave up on
reports `aborted` with its code (`socket_failed`, `send_failed`, `send_refused`), a stall
or Retry reports `aborted` alone, and a bridge turn reports `end_mark` or `aborted` with
why its thread left Connect (`start_unavailable`, `connect_failed`, `socket_failed`).

A late answer sends a second report for its run: the late answer's own end reason (one
the route accepts, usually `end_mark`) with the error code `late_reply`, under the run id
with `:late` added, since the route writes one run line per run id. The first report
already said `no_reply`, so the alarm counts the turn once. A report that fails to send
(a network error; any HTTP answer counts as sent) waits in a memory queue of at most 20
report bodies, oldest dropped first, and goes again before the next report and on the
`online` event. The queue holds the report body only, never text or a token; the bearer is
read when the report goes out.

The debug block keeps working: the agent sends its own `guppi.timing` (the D54 shape) with
the page-clock steps (the wait for the chat start, `SendMessage`, the first reply item, the
end), the ids (`contact`, `run`, `transport`), and notes that include Connect's own
interval from the message to the first and last reply, from `AbsoluteTime`. The page's
lines gain "chat ready".

### Fallback and rollback

If the start answers `unavailable`, the chat cannot connect, or the socket breaks and one
reconnect fails, that thread goes on through the bridge: `HttpAgent` to the manifest's
`agent` path, as before. Its reports carry `transport: "bridge"` and the reason as the
error code. A socket that fails during a turn ends that turn with `RUN_ERROR` (the page's
Retry), and the Retry goes through the bridge. `?ff=connect-bridge` puts the whole page
back on the bridge path: the bridge's warm start and `HttpAgent`, exactly as before; a page
already open keeps its transport until it reloads.

The page's CSP `connect-src` names `https://participant.connect.us-east-1.amazonaws.com`
and `wss://*.transport.connect.us-east-1.amazonaws.com` for every project page. The
CloudFront behavior `/api/hr/chat/*`, listed before `/api/*`, sends the HR routes to
guppi-hr's chat-start REST API (regional, stage `prod`, a standard Lambda proxy
integration, guppi-hr D57), whose host and stage path this stack reads from the SSM
parameters `/guppi/hr/chat-start-host` and `/guppi/hr/chat-start-path` at deploy time; the
stage path is the origin's `origin_path`, as for the invites API. HTTPS only, no caching,
no compression, and the managed all-viewer policy without Host, which forwards
`Authorization`, `Content-Type` and the body. The function checks the bearer itself; the
stage throttles each route.

## Debug mode

A tab can ask the agent behind it for its timings and show them under each reply
(approved by Sam, 4 Oct 2026; README backlog item 11). The contract between the page and
an agent:

1. The `debug` flag in `web/features.json` is off by default. `?ff=debug` turns it on for
   the browser, the way `feedback` works, and it is read once at load like the other
   flags. It is a flag, unlike passive observability (AGENTS.md), because it changes the
   request and asks the agent to send more.
2. With the flag on, every run the page sends carries `forwardedProps.debug: true`,
   added after the extension's `onSend` hooks so none can drop it. A warm start does not.
3. An agent may answer with an AG-UI `CUSTOM` event named `guppi.timing`, sent before
   `RUN_FINISHED` or `RUN_ERROR`. Times are milliseconds from the agent receiving the
   request. A step whose `end_ms` is null is a point in time. Steps with the same `lane`
   belong together (for example `bridge`, `connect`, `exchange`). `lane` and `total_ms`
   may be absent; `ids` maps names to strings (for example contact, trace, run); `notes`
   is a list of strings.

   ```json
   { "steps": [{ "name": "contact ready", "start_ms": 0, "end_ms": 1905, "lane": "bridge" },
               { "name": "first reply", "start_ms": 3311, "end_ms": null, "lane": "bridge" }],
     "total_ms": 3324, "ids": { "contact": "...", "trace": "..." },
     "notes": ["warm contact reused"] }
   ```

4. With the flag on, each reply the page runs gets a debug block as its last element; a
   reply drawn from history never does, and a Retry replaces the failed run's block.
   Collapsed, it is one line (`debug · first words 4.13 s · done 4.95 s · 12 steps`), a
   `details` element whose `summary` opens it by click or keyboard. Open, it shows the
   page's own times from the send, measured with `performance.now()` (token refreshed,
   when a refresh happened; request sent; run started; first text; run finished or run
   failed), then the trace id the page generated, the gateway's request id and the run
   id. When `guppi.timing` arrived it adds a waterfall: one row per step in start order
   with its lane, name, start and duration (or "point"), and a bar whose left offset and
   width are percentages of the larger of `total_ms` and the latest end or point; then
   the agent's ids and notes. The block is small, muted and monospace, uses the page's
   theme variables in both schemes, and puts each bar on its own line under 30rem.
5. With the flag off, nothing changes: no `forwardedProps.debug`, no block, and the
   page ignores `guppi.timing`.

Extensions receive `guppi.timing` like any other `CUSTOM` event, flag on or off: the
block reads the event before the extension host sees it and never claims it. The value
is untrusted, so `web/src/debug.js` keeps at most 60 steps, 12 ids and 12 notes, coerces
times to numbers between 0 and an hour (a step without a usable start is dropped; an end
before its start becomes the start; an unusable end makes a point), cuts names to 80
characters, ids to 200 and notes to 300 on one line, and sets every value with
`textContent`.

## Page extension API

The page exposes one object, `guppi`, to an extension module's default export:
`export default function (guppi) { ... }`. Anything two projects both need moves into
the page as a built-in.

| Member | Signature | Purpose |
| --- | --- | --- |
| `guppi.project` | manifest object | The manifest as loaded |
| `guppi.renderers.tool(name, fn)` | `fn(toolCall, slot, ctx)` | Render a tool call's arguments or result into the reply's attachment slot; called on `TOOL_CALL_END` and again when the result arrives |
| `guppi.renderers.event(type, fn)` | `fn(event, slot, ctx)` | Handle `TOOL_CALL_START`, `TOOL_CALL_END`, `CUSTOM`, `STEP_STARTED`, `STEP_FINISHED`, `STATE_SNAPSHOT` or `STATE_DELTA`; taking a tool call event leaves that call's status line to the extension |
| `guppi.onSend(fn)` | `fn(runInput) -> runInput` | Add `forwardedProps` or `state` to the run before it is posted |
| `guppi.status(text)` | | Set the reply's status line |
| `guppi.token()` | `-> string` | The current access token, for a renderer that calls the tools gateway directly |
| `guppi.onThread(fn)` | `fn({ threadId })` | Called on the first load, a new chat and a resumed or switched thread, so an extension can drop per-thread state |
| `ctx.setLabel(text)` | in a renderer's context | Replaces the running reply's label, for example an agent name per delegation; history keeps text only |
| `guppi.mcp` | client or `null` | A browser-side MCP client bound to `manifest.mcp`, when configured |

Each reply gains a `reply-attachments` element after `reply-text`. Renderers write into
it; the plain-text reply, feedback controls and history are untouched. Threads saved to
IndexedDB keep only text, as today. Renderers never put model or user text through
`innerHTML`; an MCP App's HTML goes into a sandboxed iframe, which is the point of the
sandbox.

A running reply also shows three pulsing dots (`reply-pending`, `web/src/pending.js`)
between its status line and `reply-text`, for every project and with no extension
involved. They show from the send while the reply has no text: until the first words are
painted, again while a tool call has cleared the text, and never after the run finishes,
fails or stalls. The reply carries `aria-busy="true"` while they show, a visually hidden
"Working" labels them, and under `prefers-reduced-motion` they hold still. A warm start
is not a turn and shows nothing; a reply drawn from history never shows them. The page
draws no status line from `STEP_STARTED` itself: a step name is the agent's own word
(the Connect bridge sends "Amazon Connect" at the start of every turn), so turning it
into a sentence stays with the project's extension, as guppi-hr's `/p/hr-diy/` does.

`ext.js` is imported with a dynamic `import()` from the same origin, so the CSP stays
`script-src 'self'`. The project builds it with esbuild as an ES module; this bundle stays
an IIFE.

## MCP Apps delivery

A project's MCP App reaches the page through the agent's own AG-UI stream (path A), built
in phase 3. guppi-mcp-app's phase 2 run through the platform showed why that is enough:
the tools gateway passes the whole MCP Apps surface through (tool `_meta`, the embedded
resource, `structuredContent`, `resources/list` and `resources/read`), and the
`TOOL_CALL_RESULT` the page received for `mcp-app___show_card` held the complete card
document. It held it only by position, though: Strands maps an embedded resource to a
bare text item and the `ag_ui_strands` adapter keeps the last text item of a result, so
the resource's uri, mime type, `structuredContent` and `_meta` were gone. The platform
agent therefore relays the resource itself, as a `CUSTOM` event named `mcp-app/resource`
right after the tool call's `TOOL_CALL_RESULT`:

```json
{ "toolCallId": "...", "uri": "ui://mcp-app/card", "mimeType": "text/html;profile=mcp-app",
  "text": "<!DOCTYPE html>...", "toolResult": { "content": [...], "structuredContent": {...}, "_meta": {...} } }
```

The page's built-in host renderer (`web/src/mcp-apps/host.js`) claims that event, frames
`/sandbox/frame.html` in the reply's attachment slot with `sandbox="allow-scripts"`, and
runs the host half of the ext-apps bridge; the proxy page writes the HTML into a nested
`srcdoc` frame. `/sandbox/*` has its own CSP, since a `srcdoc` frame inherits its parent's
and the page's forbids inline script. The sandbox shares the page's origin, kept opaque by
leaving out `allow-same-origin`; that is the proof of concept arrangement, and a separate
origin for the sandbox is the production answer.

The two paths set aside:

| Path | What it is | Why not in phase 3 |
| --- | --- | --- |
| B | The page reads `ui://` through the tools gateway itself, from the tool's `_meta.ui.resourceUri` | Open at the gateway, and the spec-shaped path, but it needs the `/mcp/*` behavior and a browser MCP client; an experiment in phase 4, for when an app calls tools |
| C | The agent issues `resources/read` for the tool's `_meta` reference and relays the result | Not needed while the result embeds the resource; the same `CUSTOM` event can carry it if a server stops embedding |

## Project tiers

A project is one of two shapes and can grow from the first into the second without
changing its URL.

| | Tools-only project | Agent project |
| --- | --- | --- |
| What it deploys | An MCP server and a target on the tools gateway named after the project | The above plus its own agent Runtime and a runtime target on the edge gateway |
| Who runs the turn | The platform's Guppi agent | The project's agent, built on the shared kit |
| Tools the agent sees | `<name>___*` from the project's target, chosen by the project name the page sends in `forwardedProps` | Whatever the project agent asks the tools gateway for |
| Manifest `agent` | `"platform"` | `"/api/<name>/invocations"` |

For tools-only projects `agent.py` gains a second filter: when the run's
`forwardedProps.project` is set, tools with that project's prefix are included beside
`docs___Retrieve`, and the system prompt names them. The agent stays stateless per request.

## Changes to this repository

All changes are additive and landed on the `platform` branch, merged into `main` on 2 October
2026. The flag system is not
touched.

| File | Change |
| --- | --- |
| `web/src/app.js` | Resolve the project from the path before `config.json` is read; fetch the manifest; merge `manifest.features` into `config.features` before `initFeatures(config)`; carry the path in the OAuth `state`; set brand strings from the manifest; add the `reply-attachments` slot in `addTurn`; dispatch tool and custom events to the renderer registry; apply `onSend` hooks to the run input; import `ext.js`. Kept in named functions (`resolveProject`, `loadManifest`, `installExtension`) called from the boot |
| `web/src/extensions.js` (new) | The `guppi` API object and the renderer registry |
| `web/src/mcp-apps/` (new, phase 3) | The built-in MCP Apps host renderer and bridge, created by `extensions.js` for `capabilities: ["mcp-apps"]` |
| `web/src/sandbox/` (new, phase 3) | `frame.html` and `frame.js`, the sandbox proxy served under `/sandbox/*` |
| `web/src/index.html` | Brand elements keep their ids; text is set at load. Title falls back to GuppiGPT |
| `web/test/` | Tests for project resolution, manifest merge and the `state` round trip |
| `infra/guppi_gpt_infra/stack.py` | Two CloudFront Functions (page path rewrite, agent path rewrite) and the SSM parameters; in phase 3 the `/sandbox/*` behavior with its response headers policy and `frame-src 'self'` on the page's CSP. Everything else as is |
| `agent/src/guppi_agent/agent.py` | Project prefix filter driven by `forwardedProps.project`; in phase 3 the `mcp-app/resource` event; `build_strands_agent` stays the test hook |
| `agent/pyproject.toml` | Package metadata so the kit installs by git URL |
| `scripts/deploy.sh` | `--reuse-parameters`; the platform still publishes only its own page |

Files left alone: `features.js`, `flags-core.js`, `flags.js`, `flags.html`,
`features.test.mjs`, `flags.test.mjs`, `history.js`, `feedback.js`, `session.js`,
`rum.js`, the legal pages, and every part of the stack that is not the two functions, the
parameters, and the sandbox behavior.

## Phases

Each phase ends with a deploy and a browser check.

1. Platform branch: SSM parameters, the two CloudFront Functions, project resolution and
   manifest loading in `app.js`, the `state` round trip, the extension registry and reply
   slot, the kit's package metadata, the platform agent's prefix filter. Done when
   `chat.dengler.io/` behaves as today and `chat.dengler.io/p/demo/` loads a hand-uploaded
   manifest with a different label and talks to the platform agent.
2. guppi-mcp-app, tools-only: one MCP server with a single tool that returns a `ui://`
   resource; a target on the tools gateway; manifest with `agent: "platform"`. Done when the
   Guppi agent lists `mcpapp___*` beside `docs___Retrieve` for that project and it is known
   whether the gateway passes `resources/read` and tool `_meta` through.
3. MCP Apps host: the built-in renderer and bridge, behind `capabilities: ["mcp-apps"]`;
   the `/mcp/*` behavior if the browser path is needed; CSP `frame-src`. Built on path A
   ("MCP Apps delivery" above); `/mcp/*` was not needed.
4. Experiments, each inside guppi-mcp-app, none touching this repository.
5. hr-super-agent re-homed as an agent project.

## Open questions

- Answered in phase 2: AgentCore Gateway passes `resources/read`, `resources/list`, tool
  `_meta`, `structuredContent` and embedded resources through an MCP target unchanged.
- Decided in phase 3: the page's CSP names `frame-src 'self'`, and `/sandbox/*` carries its
  own CSP. A separate origin for the sandbox is still owed before anyone else's apps run
  here.
- `import()` of `ext.js` from the IIFE bundle: esbuild keeps native dynamic import at
  `--target=es2022`; verify in the build.
- The per-user rate limit on the edge gateway is per JWT subject across every project.
  Fine for one user; noted for anyone invited.
- The kit as a git dependency pins a tag; a contract change needs a tag bump per project.
