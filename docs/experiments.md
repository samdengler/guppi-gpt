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
