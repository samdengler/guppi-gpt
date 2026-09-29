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
