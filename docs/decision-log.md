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

### Step 4: stack

- Target name `mcp-app`, so the tools are `mcp-app___show_card`, instead of the brief's
  `mcpapp`. Two facts decide it. The CloudFormation schema for
  `AWS::BedrockAgentCore::GatewayTarget` gives `Name` the pattern
  `^([0-9a-zA-Z][-]?){1,100}$`: letters, digits and single hyphens, no underscores, so
  `mcp_app` is not a valid name. And the platform agent (guppi-gpt `agent.py`,
  `select_tools`) offers a project page's tools only when they start with
  `mcp_app___` or `mcp-app___` for the project `mcp-app`; `mcpapp___show_card` would
  never reach the agent, and the phase's done condition (the agent lists the project's
  tools beside `docs___Retrieve`) could not hold. guppi-gpt's phase 1 report flagged the
  same mismatch. Renaming the target is one constant (`TARGET_NAME`) and a redeploy.
- `McpTargetConfigurationProperty` in `aws-cdk-lib` 2.271.0 has `mcp_server`,
  `lambda_`, `open_api_schema`, `smithy_model`, `api_gateway` and `connector`; none takes
  a Runtime ARN (only the HTTP target's `agentcore_runtime` does, and that is not an MCP
  target). The target uses `mcp_server` with `endpoint` set to the Runtime's MCP
  invocation URL,
  `https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<URL-encoded ARN>/invocations?qualifier=DEFAULT`,
  with the ARN rebuilt from the runtime id so `:` and `/` can be written as `%3A` and
  `%2F` in the template.
- `listing_mode` `DYNAMIC`. The API documentation says a DEFAULT target's MCP resources
  are cached at the control plane and a DYNAMIC target's are fetched when tools are
  listed. With `JWT_PASSTHROUGH` a control plane sync has no user token to present to
  the Runtime's JWT authorizer. No `mcp_tool_schema`: the API documentation says it is
  supported only with an OAuth authorization code credential provider.
- `allowed_workload_configuration` is left off. guppi-gpt's AGENTS.md and decision log
  record that binding a JWT runtime to a gateway demands a transaction token, which the
  gateway supplies only when it signs with its own role, and that combination was
  rejected; token passthrough, which this target needs, forwards the user JWT without
  one.
- The `InvokeAgentRuntime` grant is an `AWS::IAM::Policy` owned by this stack and attached
  to the tools gateway role imported by its SSM ARN. With `JWT_PASSTHROUGH` to an HTTPS
  endpoint the gateway presents the user's token and not its role, so the grant is likely
  unused; it is kept because the brief and the platform contract ask for it, and it lets
  a later switch to `GATEWAY_IAM_ROLE` work without a policy change.
- The runtime role follows guppi-gpt's documented execution role minus the workload token
  and Bedrock model permissions, which an MCP server with no outbound calls does not use.
