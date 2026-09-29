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

### Step 6: deploy

- The first deploy failed before CloudFormation: the CDK asset stages the build context
  through `.dockerignore`, which left out `server/Dockerfile`. `.dockerignore` now keeps
  it, as guppi-gpt's keeps `agent/Dockerfile`. A local `docker build -f` does not show the
  problem, since the Dockerfile is read outside the filtered context.
- The second deploy built and pushed the image, then CloudFormation failed on the Runtime:
  "Access denied while validating ECR URI". The Runtime referenced its role but not the
  role's default policy, which carries the ECR pull grant, so the two were created in
  parallel. The Runtime now depends on the whole role construct, policy included. The
  failed create left `GuppiMcpApp` in `ROLLBACK_COMPLETE` with no resources; `cdk deploy`
  deletes a stack in that state before creating it again, which is its own behavior and
  not a `cdk destroy`.
- The third deploy created the Runtime, then the target failed: "MCP server target does
  not support JWT_PASSTHROUGH credential provider type". The brief's design (the user's
  token reaching the server) is not available for an MCP server target today. Of the
  credential types the target can take, `GATEWAY_IAM_ROLE` is the one that needs nothing
  outside this stack: the gateway signs each request to the Runtime with SigV4 as the
  tools gateway role (`IamCredentialProvider` service `bedrock-agentcore`), and the
  Runtime drops its JWT authorizer, since a JWT runtime rejects a SigV4 request as an
  authorization method mismatch (guppi-gpt's decision log). `OAUTH` would need a
  client-credentials app client and a resource server in the platform's Cognito pool plus
  a client secret in an AgentCore Identity provider, which this repository may not create.
  Consequences: the user's JWT is still checked by the tools gateway's own authorizer on
  the way in, but the server no longer sees who the user is (it needs no identity in this
  phase); the Runtime accepts only callers allowed `InvokeAgentRuntime`, which is the
  tools gateway role through this stack's policy, so that grant is now the one that
  matters. `-c target_credentials=JWT_PASSTHROUGH` synthesizes the brief's design (JWT
  authorizer on the Runtime, passthrough on the target) for when the service accepts it;
  a test covers both shapes.
- The fourth deploy failed on the Runtime: an `Authorization` request header allowlist is
  accepted only with a JWT authorizer. The allowlist now comes with the JWT variant only;
  with SigV4 there is no user token to forward to the container anyway.

### Step 7: checks through the platform

- The tools gateway pages `tools/list` and `resources/list`. With the target in `DYNAMIC`
  listing mode, page 1 held only the `docs` tools and `mcp-app___show_card` came on page 2
  behind `nextCursor`. The platform agent (guppi-gpt `agent.py`) calls Strands'
  `list_tools_sync()` once and never follows the cursor, so its run log said `project
  mcp-app has no mcp_app___* tools on the gateway` and the model answered in text
  (`.deploy/phase2-agui-dynamic.txt`). The fix belongs in guppi-gpt (loop on
  `pagination_token`) and needs a platform deploy, which this run may not make. Inside
  this repository the target moved to `DEFAULT` listing: now that the gateway signs with
  its own role, the control plane can sync the tools, and a cached target's tools come on
  the first page beside `docs`. After the change `tools/list` is one page with all three
  tools and the agent calls `mcp-app___show_card`. The agent fix is still owed, since any
  later DYNAMIC target, or a first page that fills up, hides project tools again.
- `scripts/probe.py` now follows `nextCursor` on both list calls and prints the page count.

## Phase 4 (29 Sep 2026)

- The pages share one app-side bridge (`server/src/mcp_app_server/bridge.py`) instead of a
  copy per page: `mcpApp()` runs the handshake, matches responses to the page's own
  requests, and keeps the two second fallback. The card's markup and behavior are as in
  phase 2 apart from the E3 button.
- Card ids are the title as a slug (`card_id_for`), not random. The page sends the agent
  only the conversation's text, so on the turn that asks to change a card the model has
  the title from the user's own words and nothing from the earlier tool result. A slug
  lets it name the card again (E4).
- `card_clicked` carries `_meta.ui.visibility = ["app"]`, as the extension marks an
  app-only tool, even though the platform agent does not read it yet; the gateway keeps
  the key, and the row records that the model is offered the tool anyway.
- E6 uses `ui/update-model-context`, not `ui/message`. The brief asks for the answers to
  join the next turn; the spec defines update-model-context as context "used in future
  turns" that the host may hold until the next user message, while `ui/message` starts a
  turn in the user's name.
- The E6 form uses a `type="button"` click, not a `<form>` submission: the host's sandbox
  has no `allow-forms`, and Chromium blocks the submission before the `submit` handler
  runs (found with a local harness before the deploy).
- E5's page is three files (`web/app/`) with its script and style as files, since the site
  CSP (`script-src 'self'`, `style-src 'self'`) covers `/projects/*`. The resource text is
  the URL as one CRLF-terminated `text/uri-list` line.
- The tool name for E5 is `show_static_page` and the resource `ui://mcp-app/static-page`;
  the brief named neither.
- `scripts/deploy.sh` synchronizes the `mcp-app` gateway target after `cdk deploy`. The
  first deploy of this phase updated the Runtime, but the gateway kept listing only
  `show_card` (DEFAULT listing caches at the last sync). Synchronizing a target of this
  stack's own is inside the repository's remit; the gateway itself is untouched.
- The browser check lives here (`scripts/browser-check.mjs`), adapted from guppi-gpt's
  phase 3 check, and reads Playwright and the platform's stack outputs from `../guppi-gpt`
  without writing there. It records each run's AG-UI events from the response body, so
  the report can say which tool the model called and whether a `CUSTOM` event followed.
