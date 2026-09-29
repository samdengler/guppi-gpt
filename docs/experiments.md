# MCP Apps experiments

Phase 4 (`docs/phase-4.md`): each way an MCP App can reach and talk to the chat page at
`https://chat.dengler.io/p/mcp-app/`, what it took, whether it works on this platform, and
the evidence. The host is guppi-gpt's phase 3 renderer (`web/src/mcp-apps/host.js` on the
`platform` branch) behind the platform agent's `mcp-app/resource` event. Message names are
from the MCP Apps extension, spec revision 2026-01-26 (modelcontextprotocol/ext-apps,
commit `82221c0`). Screenshots are in `.deploy/` (gitignored, on Sam's Mac).

| Experiment | Path | What the app can do | Works here | Evidence | Platform change needed |
| --- | --- | --- | --- | --- | --- |
| E1, embedded resource in the tool result (`show_card`) | A: the `tools/call` result embeds `ui://mcp-app/card`; the platform agent relays it as the `mcp-app/resource` event; the page frames it in `/sandbox/frame.html` | Render from `tool-input` and `tool-result` (`structuredContent`), resize itself, open http and https links | yes | Phase 3 browser check (card "Hello", frame resized 160 to 108 pixels); `.deploy/phase-4-E1.png` | none |
| E2, host reads `ui://` by `resources/read` (`show_chart`) | B: the result carries only `_meta.ui.resourceUri = ui://mcp-app/chart` and `structuredContent`; the host must fetch the page through the tools gateway | Same as E1 once rendered; the page is fetched once per resource, not rebuilt per call | blocked | `resources/read ui://mcp-app/chart` answers through the gateway (probe); the page has no MCP client (`guppi.mcp` is `null`) and the agent relays only embedded resources, so no frame appears; `.deploy/phase-4-E2.png`, `.deploy/phase4-E2-routes.txt` | A `guppi.mcp` client in the page (initialize, `resources/read`) that the MCP Apps host calls when a tool call's definition names a `ui://` resource, plus a way for the browser to reach the tools gateway: the `/mcp/*` behavior, or the gateway's host in the page CSP's `connect-src` |
| E3, app calls a tool (`card_clicked`) | The card's button sends `tools/call` `{name: "card_clicked", arguments: {card_id}}` to the host over the bridge; the host would relay it to the server | Ask its own server for work or fresh data without a model turn | partly | The request reaches the host and the host's refusal (`-32601`) is rendered in the card; `mcp-app___card_clicked` answers through the gateway (probe) and writes its audit line; `.deploy/phase-4-E3.png` | A relay in the host: `tools/call` through `guppi.mcp` to the tools gateway (the E2 client and route), adding the `mcp-app___` prefix and allowing only tools whose `_meta.ui.visibility` includes `app`; and the platform agent leaving `visibility: ["app"]` tools out of the model's list |
| E4, a later tool updates the same app (`update_card`) | A, twice: `update_card(card_id, body)` returns the same `ui://mcp-app/card` resource and card id as the earlier `show_card` | Change what an app already on screen shows, from a later turn | partly | The update renders, in a second frame under the second reply; the first card keeps its old body. The host mounts one frame per tool call id, and the extension defines no rule for sending a later tool's result to an existing view; `.deploy/phase-4-E4.png` | For in-place updates, a host rule of its own: route a result to the mounted frame whose resource uri and `structuredContent.card_id` match, as a second `ui/notifications/tool-result` (the card already re-renders on it). Or, within the spec, E3's relay, so the card fetches its own fresh state |
| E5, an app served as a static page (`show_static_page`) | The result embeds `ui://mcp-app/static-page` as `text/uri-list` naming `https://chat.dengler.io/projects/mcp-app/app/index.html`, which this repository publishes beside its manifest | Run a whole existing web app with its own files, instead of one inline document | blocked | No frame: the agent relays only `text/html;profile=mcp-app` resources. Four more layers would refuse it: the host accepts only that mime type, the proxy writes only `srcdoc`, the sandbox CSP is `default-src 'none'` (no `frame-src`), and `/projects/*` is served with `X-Frame-Options: DENY` and `frame-ancestors 'none'`. The extension lists `text/uri-list` as deferred from its first version; `.deploy/phase-4-E5.png` | The agent and host accepting `text/uri-list`, a proxy message carrying a URL, `frame-src` for the app's origin in the sandbox CSP, and framing allowed on the app's responses. Before doing it for anyone's pages, apps on a separate origin from the chat page |

## E1, embedded resource in the tool result

The baseline, built in phases 2 and 3 and unchanged here.

Path. The model calls `mcp-app___show_card(title, body)`. The tools gateway signs the call
to the Runtime and passes the whole result back: a text block, an embedded resource
(`type: "resource"`, uri `ui://mcp-app/card`, mime type `text/html;profile=mcp-app`, the
card page with the call's values baked in), `structuredContent: {title, body}` and
`_meta.ui.resourceUri`. The platform agent's `MCPClient` subclass sees the raw result in
`_handle_tool_result`, and the agent emits a `CUSTOM` event named `mcp-app/resource` right
after the call's `TOOL_CALL_RESULT`, carrying `{toolCallId, uri, mimeType, text,
toolResult}`. The adapter's own `TOOL_CALL_RESULT` holds only the HTML string, by position.

Message flow in the page.

1. The host claims the event and appends an iframe on `/sandbox/frame.html` with
   `sandbox="allow-scripts"` to the reply's `reply-attachments` slot.
2. The proxy posts `ui/notifications/sandbox-proxy-ready`; the host answers with
   `ui/notifications/sandbox-resource-ready` carrying the HTML, and the proxy writes it
   into a nested `srcdoc` frame, sandboxed the same way.
3. The app sends `ui/initialize`; the host answers with `hostCapabilities: {openLinks: {}}`,
   host info and host context (theme, `displayMode: "inline"`, locale).
4. The app sends `ui/notifications/initialized`; the host sends
   `ui/notifications/tool-input` (the arguments from `TOOL_CALL_ARGS`) and
   `ui/notifications/tool-result` (the `toolResult`: text blocks, `structuredContent`,
   `_meta`), once each.
5. The app renders and sends `ui/notifications/size-changed`; the host sets the frame
   height, clamped to 40 to 640 pixels.

What the app receives: the arguments and the tool result, never the page's token, storage
or cookies (the frames are opaque origins). What it can send back: `size-changed`,
`ui/open-link` (http and https only) and `ping`. Everything else is refused with JSON-RPC
`-32601`.

## E2, host reads `ui://` by `resources/read`

What was built. `show_chart(values: list[float])` returns a text block,
`structuredContent: {values}` and `_meta.ui.resourceUri = ui://mcp-app/chart`, and no
embedded resource. The tool definition carries the same `_meta`. `ui://mcp-app/chart` is a
`text/html;profile=mcp-app` resource in `resources/list` and `resources/read`: a bar chart
page with no values of its own, drawn from the `values` the host pushes as `tool-input` or
`tool-result`. The pages now share one app-side bridge (`server/.../bridge.py`).

What the spec asks of the host. "Host MUST use `resources/read` to fetch the referenced
resource URI" (spec, "Resource Discovery"), with the URI taken from the tool's
`_meta.ui.resourceUri`, then the same handshake as E1.

What happens here. The gateway half works: the probe lists both resources and reads
`ui://mcp-app/chart` through the tools gateway with the uri and mime type intact. The
host half is absent. The platform agent's `app_resource()` looks for an embedded
resource; with none it emits no `mcp-app/resource` event, and the page never learns the
result had a UI. The page's `guppi.mcp` is `null` (guppi-gpt `web/src/extensions.js`), so
nothing in the browser could issue `resources/read` either. The reply is text only.

The platform change, two parts.

1. A browser MCP client. `guppi.mcp`, bound to `manifest.mcp` (`{"url": ...,
   "toolPrefix": "mcp-app___"}`), that runs `initialize`, `tools/list` (following
   `nextCursor`) and `resources/read` with the user's access token. The MCP Apps host,
   on `TOOL_CALL_END` for a tool whose listed definition has `_meta.ui.resourceUri`, reads
   the resource through it and mounts the frame; the tool result arrives later as
   `TOOL_CALL_RESULT` (text only today) or from the agent's event.
2. A route from the browser to the tools gateway. The brief names the `/mcp/*` CloudFront
   behavior. The preflight check in `.deploy/phase4-E2-routes.txt` shows a shorter one:
   the tools gateway answers `OPTIONS` from `https://chat.dengler.io` with
   `access-control-allow-origin: *` and allows `authorization`, `content-type` and
   `mcp-protocol-version`, so adding the gateway's host to the page CSP's `connect-src`
   (today `'self' https://auth.dengler.io` and the Dynatrace beacon) is enough for the
   browser to call it directly. The `/mcp/*` behavior keeps the gateway's hostname out of
   the page and puts WAF and the origin-verify header in front; the CSP route is one
   string. Today `GET https://chat.dengler.io/mcp` is the site bucket's 403.

A direct route to the Runtime instead. The Runtime endpoint also answers the preflight
with `access-control-allow-origin: *`, but it accepts only SigV4 today ("Missing
Authentication Token" without it), because the gateway target signs with
`GATEWAY_IAM_ROLE` and a Runtime has one authorizer. The browser route would need a
second Runtime (or this one moved) with a JWT authorizer on the platform's Cognito client
and the `Authorization` header allowlist, the Runtime's host in `connect-src`, and the
page's MCP client talking to it with unprefixed tool names. It also skips the gateway's
own checks and logs. That is more than the gateway route for the same result, so the
gateway route is the one to build.

## E3, app calls a tool

What was built. The card gains a "Record a click" button. Pressing it sends
`{"method": "tools/call", "params": {"name": "card_clicked", "arguments": {"card_id":
...}}}` to the host and renders the answer (the first text block) or the JSON-RPC error
in a status line under the body. The app names the tool as its server does; the prefix is
the host's business. Each card has an id, its title as a slug (`hello` for "Hello"), in
`structuredContent` and in the text the model reads. `card_clicked(card_id)` on the server
returns a text block and `{card_id, clicked_at}`, and writes one line to stdout, which
lands in the Runtime's CloudWatch log:
`audit {"event": "card_clicked", "card_id": "hello", "at": "..."}`. Its definition carries
`_meta.ui.visibility = ["app"]`, the extension's mark for a tool the model must not see.

What happens here. The phase 3 host answers every `tools/call` with
`{"code": -32601, "message": "tools/call is not available on this host yet"}`, and the
card shows "Host refused tools/call: JSON-RPC error -32601: tools/call is not available on
this host yet". The server half works: the probe calls `mcp-app___card_clicked` through
the gateway. The platform agent does not read `visibility`, so the model is offered
`mcp-app___card_clicked` beside the other tools; the tool description says the card calls
it, which is the only thing keeping the model off it.

Which relay would carry it. Two candidates.

- Through `guppi.mcp`, the browser MCP client E2 needs. The host checks the tool's listed
  `_meta.ui.visibility` includes `app` (the spec's MUST), adds `manifest.mcp.toolPrefix`,
  sends `tools/call` to the tools gateway with the user's token, and returns the result
  to the app as the response to its request. One HTTP round trip, no model turn, the
  gateway's JWT check and logs still apply, and it is the shape the spec draws (the host
  proxies to the server). It reuses everything E2 needs and adds about thirty lines to the
  host.
- A new AG-UI turn. The host posts a run whose `forwardedProps` carries the app's call,
  and the platform agent runs the tool without asking the model and streams the result
  back. It needs no browser route to the gateway, but costs an agent Runtime invocation
  and an SSE stream per click, needs a new agent code path that bypasses the model, and
  mixes app traffic into the conversation's run history.

The first is the one to build, together with E2's client. The agent change to follow
`visibility` is separate and small: drop tools whose `_meta.ui.visibility` lacks `model`
in `select_tools`.

## E4, a later tool updates the same app

What was built. `update_card(card_id, body)` returns a text block, the card page embedded
again under the same `ui://mcp-app/card` with the card id and new body baked in,
`structuredContent: {card_id, body}` and the same `_meta.ui.resourceUri`. The server keeps
no state, so it sends no title; a card that is updated in place keeps the title it shows,
and a new frame shows "Card <id>". The card counts the `tool-result` notifications it
receives and, from the second on, says "Updated by a later tool result (n)" in its
status line. The tool description tells the model how a card id is made (the title as a
slug), since the page sends the model only the conversation's text, not earlier tool
results.

What the extension says. Nothing about this case. Spec revision 2026-01-26 and the draft
both tie a view to one tool call: the host renders the tool's resource, sends that call's
`tool-input` and `tool-result`, and the view's own route to fresh data is calling tools
("Interactive Updates", which is E3). Neither text has a rule for routing a later tool
call's result into a view that is already mounted, and the draft adds no view identity
the host could match on.

What happens here. The phase 3 host keys frames on the AG-UI tool call id
(`mounted.has(resource.toolCallId)`), and `update_card` is a new tool call, so it mounts a
second frame under the second reply. That frame renders "Card draft" with the new body;
the first card is unchanged. The host is doing what the extension describes.

Two ways to get an in-place update. A host rule outside the spec: when a
`mcp-app/resource` event names a uri already mounted in the thread and its
`structuredContent` carries an id the mounted frame was given, send the new result to
that frame as another `ui/notifications/tool-result` and mount nothing. The card needs no
change for that. Or stay inside the spec: the card asks its server for its current
state through E3's relay, which needs the server to keep state (the Runtime is stateless
today) and something to tell the card to ask.

## E5, an app served as a static page

What was built. `web/app/index.html` with `app.css` and `app.js`, published by
`scripts/deploy.sh` to `projects/mcp-app/app/` beside the manifest. The site CSP applies to
everything under `/projects/`, so the page has no inline script or style; `app.js` speaks
the same handshake as the inline pages and says whether a host answered. Opened on its own
it says "Opened on its own: no MCP Apps host around this page". `show_static_page()`
returns a text block, an embedded resource `ui://mcp-app/static-page` with mime type
`text/uri-list` and the page's URL as its text (one CRLF-terminated line, RFC 2483), the
same resource in `resources/list` and `resources/read`, `structuredContent: {url}` and
`_meta.ui.resourceUri`.

What the extension says. Spec revision 2026-01-26 defines only
`text/html;profile=mcp-app` content and lists "`externalUrl`: Embed external web
applications (e.g., `text/uri-list`)" under content types deferred from the first
version; the rationale names model visibility, screenshots and review as the reasons.
The draft keeps it deferred. So there is no extension message for a proxy to load a URL.

What happens here, layer by layer.

1. The platform agent's `app_resource()` takes only resources of type
   `text/html;profile=mcp-app`, so it emits no `mcp-app/resource` event and the reply is
   text only. This is where it stops.
2. The host's `resourceFromEvent` would drop the event for the same reason.
3. The proxy (`web/src/sandbox/relay.js`) takes `params.html` from
   `ui/notifications/sandbox-resource-ready` and writes it to `srcdoc`; it has no URL form.
4. The sandbox CSP is `default-src 'none'; script-src 'self' 'unsafe-inline'; ...` with no
   `frame-src`, so a nested frame at any URL is refused.
5. The page's own responses carry `X-Frame-Options: DENY` and `frame-ancestors 'none'`
   (the site's response headers policy covers `/projects/*`), so no document may frame it,
   the chat page included.

What it would take. A proxy message with a URL (outside the extension, or a later
revision of it), the agent and host passing `text/uri-list` resources through, `frame-src`
for the app's origin in the sandbox CSP, and the app's responses allowing the sandbox as
an ancestor. Serving apps from the chat page's own origin is the wrong place for that: a
framed page without `sandbox` would share the chat page's origin and its IndexedDB
session. Apps loaded by URL belong on a separate origin, the same one the sandbox proxy is
already owed (phase 3 report). The inline-HTML path (E1) needs none of this, which is why
the extension starts there.
