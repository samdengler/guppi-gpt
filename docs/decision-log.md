# Decision log

Decisions taken during unattended runs where the brief did not settle the question. Each
entry names the step, the choice, and why. The smaller, reversible option wins by default.

## Phase 2 (28 Sep 2026)

### Step 2: server

- MCP SDK. `uv add mcp` resolved `mcp` 2.2.0, where FastMCP is renamed `MCPServer`
  (`mcp.server.mcpserver`). The server uses `MCPServer`, which is the brief's FastMCP
  under its current name, instead of pinning `mcp<2`. The SDK still negotiates the
  `initialize` handshake for protocol revisions 2024-11-05 to 2025-11-25, which is what
  the AgentCore gateway and runtime speak. Reversible by pinning `mcp<2` and renaming the
  import.
- MCP Apps keys, from `specification/2026-01-26/apps.mdx` in
  modelcontextprotocol/ext-apps (latest commit `82221c0`, 25 Sep 2026): the tool metadata
  key is the nested `_meta.ui.resourceUri` (the flat `_meta["ui/resourceUri"]` is marked
  deprecated and is not sent), and the resource mime type is `text/html;profile=mcp-app`.
  The brief's `text/html` is replaced by the profile form everywhere: `resources/list`,
  `resources/read`, and the embedded resource block. The SDK's own `mcp.server.apps`
  uses the same two values.
- `_meta.ui.resourceUri` is on the tool definition (where the spec puts it, read from
  `tools/list`) and also on the `tools/call` result (where the brief asks for it), so the
  gateway check in step 7 can see whether either survives.
- The result also carries `structuredContent: {title, body}`. The spec forwards the whole
  `CallToolResult` to the page as `ui/notifications/tool-result` and recommends
  `structuredContent` for UI data, so the page reads the values from there, or from
  `ui/notifications/tool-input` arguments.
- The page sends `ui/initialize` with `protocolVersion` `2026-01-26`, answers the result
  with `ui/notifications/initialized`, and reports `ui/notifications/size-changed` after
  each render. It declares no CSP in `_meta.ui`, so a host applies the spec's restrictive
  default, which allows the inline script and style the page uses.
- The SDK's `Apps` extension class is not used. It advertises a SEP-2133 extension
  capability during negotiation, which is one more thing the gateway would have to pass;
  a plain tool with `meta=` and a `TextResource` put the same bytes on the wire.
- DNS rebinding protection is off in the container. The runtime sets the Host header, and
  the check exists for servers on a developer's loopback.
- `.python-version` pins 3.12 so the local venv matches the container image.
- The in-process tests use `Client(mcp, mode="legacy")`, which runs the `initialize`
  handshake and JSON-RPC framing instead of the SDK's direct in-process dispatch.

### Step 3: local check

- `scripts/probe.py` speaks JSON-RPC over streamable HTTP with the standard library
  instead of the SDK's `Client`, and prints each result as the server sent it. The point
  of the probe in step 7 is to see which fields survive the gateway, and a typed client
  would parse, rename, or drop fields before they could be seen. It offers protocol
  revision 2025-06-18 in `initialize`.
- The bearer token goes in with `--token -` from stdin, so it never appears on a command
  line, in `ps`, or in shell history.
