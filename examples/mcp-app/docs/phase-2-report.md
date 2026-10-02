# Phase 2: report

Run of `docs/phase-2.md`, 28 Sep 2026 (UTC 29 Sep), unattended. Steps 1 to 8 hold. The
`GuppiMcpApp` stack is deployed, the manifest is published, and the platform agent calls
`mcp-app___show_card` on a run for the `mcp-app` project. Three parts of the brief did not
survive contact with the service and were changed; each is below and in
`docs/decision-log.md`. Nothing in guppi-gpt was edited or deployed, and its `main` is
untouched.

Phase 1 preconditions held: guppi-gpt's `platform-phase-1-report.md` records the
`/guppi/platform/*` parameters and the agent's project filter as landed, and all 13
parameters were present in SSM before step 4.

## Steps and commits

| Step | State | Commits |
| --- | --- | --- |
| 1. Repository layout, private GitHub repository | done | `f83bfd6` |
| 2. MCP server with `show_card` | done | `0bd2383` |
| 3. Probe script, local container check | done | `116e669`, `89a2f45` (cursor paging) |
| 4. `GuppiMcpApp` stack | done | `8d2d9bd` |
| 5. Manifest and deploy script | done | `9ce8e9d` |
| 6. Deploy | done after four fixes | `e235dc9`, `0b14ae9`, `b29c8d2`, `a49e783` |
| 7. Checks through the platform | done | `da9cc03` (listing mode) |
| 8. Report, AGENTS.md | done | this commit and the one before it |

`uv run -- pytest` (13 tests), `uv run -- ruff check .`, `ruff format --check` and
`uv run -- cdk synth -c image_uri=<ecr uri>` were green after every commit that touched
their area. `gh repo create samdengler/guppi-mcp-app --private` worked; every commit is
pushed to `origin/main`.

## What landed

Server (`server/src/mcp_app_server/`). `MCPServer` from `mcp` 2.2.0 (FastMCP's current
name), stateless streamable HTTP on `0.0.0.0:8000/mcp`. `show_card(title, body)` returns
a text block, an embedded resource block (`ui://mcp-app/card`,
`text/html;profile=mcp-app`, the card HTML with the call's values baked in),
`structuredContent: {title, body}`, and `_meta.ui.resourceUri`; the tool definition
carries the same `_meta`. `resources/list` and `resources/read` serve the same URI with
template values. The page speaks the MCP Apps handshake (`ui/initialize`,
`ui/notifications/initialized`, then `tool-input` and `tool-result`), renders only through
`textContent`, and falls back to its baked values after two seconds of host silence. A
headless Chrome run showed both paths: opened alone it renders the baked "Hello"; inside a
test host it renders the host's `structuredContent` and never falls back. Five tests
drive the tool and resource through an in-process client with the `initialize`
handshake.

Stack (`infra/guppi_mcp_app_infra/stack.py`). Reads the tools gateway id and role ARN
from SSM (and the JWT discovery URL and client id in the passthrough variant). Server
image as an arm64 `DockerImageAsset`. `CfnRuntime` `guppi_mcp_app` with protocol `MCP`.
`CfnGatewayTarget` `mcp-app` on the tools gateway: `mcp.mcp_server` with the Runtime's
invocation URL as endpoint, `DEFAULT` listing, `GATEWAY_IAM_ROLE` credentials (SigV4 for
`bedrock-agentcore`). An `InvokeAgentRuntime` policy on the imported tools gateway role.
Outputs `RuntimeArn`
(`arn:aws:bedrock-agentcore:us-east-1:009080466601:runtime/guppi_mcp_app-jpNrum5WRT`) and
`TargetName` (`mcp-app`).

Manifest and scripts. `web/manifest.json` as specified (`agent: "platform"`,
`capabilities: ["mcp-apps"]`, no `extension`, no `mcp`), published to
`projects/mcp-app/manifest.json` with `Cache-Control: no-store`; `/p/mcp-app/` answers 200.
`scripts/deploy.sh` (`--site-only`, `.deploy/` logs ending in `deploy exit=<code>`).
`scripts/probe.py`, a standard library JSON-RPC client that prints results as sent and
takes the token on stdin.

## Changes to the brief

- Target name `mcp-app`, tools `mcp-app___show_card`, not `mcpapp`. Target names allow
  letters, digits and hyphens (`^([0-9a-zA-Z][-]?){1,100}$`), not underscores, and the
  platform agent's filter for project `mcp-app` matches only `mcp_app___` or
  `mcp-app___`. With `mcpapp` the agent would never see the tool.
- `GATEWAY_IAM_ROLE` instead of `JWT_PASSTHROUGH`, and no JWT authorizer on the Runtime.
  The deploy failed with "MCP server target does not support JWT_PASSTHROUGH credential
  provider type". The user's JWT is still checked by the tools gateway on the way in; the
  server no longer sees it. The Runtime accepts only principals allowed
  `InvokeAgentRuntime`, which is the tools gateway role. `-c
  target_credentials=JWT_PASSTHROUGH` synthesizes the brief's shape for the day the
  service accepts it.
- No `Authorization` request header allowlist on the Runtime: the service accepts it only
  with a JWT authorizer.
- Mime type `text/html;profile=mcp-app`, not `text/html`, as the brief's instruction to
  use the extension's profile requires (ext-apps `specification/2026-01-26/apps.mdx`).
- `DEFAULT` listing, not `DYNAMIC`. See the pagination finding below.

The deploy also needed two ordinary fixes: `.dockerignore` had to keep
`server/Dockerfile` for the CDK asset, and the Runtime had to depend on its role's
default policy (the ECR pull grant) instead of racing it.

## Checks through the platform

Gateway URL from `/guppi/platform/tools-gateway-url`, token from guppi-gpt's
`scripts/test-token.sh` piped to `scripts/probe.py --token -`. Raw output is in
`.deploy/phase2-gateway-probe.txt` (DEFAULT listing, current) and
`.deploy/phase2-gateway-probe-dynamic.txt` (DYNAMIC listing, first deploy), both
gitignored; neither contains a token. The gateway negotiated protocol `2025-03-26` and
names itself `guppi-gpt-tools`.

| Call | Works | Evidence |
| --- | --- | --- |
| `tools/list` shows `mcp-app___show_card` beside `docs___Retrieve` | yes | One page: `docs___AgenticRetrieveStream`, `docs___Retrieve`, `mcp-app___show_card`. With DYNAMIC listing the tool came only on page 2, behind `nextCursor` |
| Tool definition `_meta` in `tools/list` | yes | `mcp-app___show_card` carries `_meta.ui.resourceUri = ui://mcp-app/card` |
| `tools/call`: text block | yes | `Showed the user a card titled 'Hello'.` |
| `tools/call`: embedded resource block | yes | `type: resource`, uri `ui://mcp-app/card`, mimeType `text/html;profile=mcp-app`, 2681 chars of HTML, identical to the server's |
| `tools/call`: result `_meta` | yes | `_meta.ui.resourceUri = ui://mcp-app/card` |
| `tools/call`: `structuredContent` | yes | `{"title": "Hello", "body": "It works"}` |
| `resources/list` | yes | `ui://mcp-app/card`, mimeType and description kept, uri not prefixed; the gateway sets `name` to the uri (the server sends `card`). Under DYNAMIC the resource came on page 2 |
| `resources/read ui://mcp-app/card` | yes | One content item, uri and mimeType kept, 2725 chars of HTML; worked under both listing modes |

The gateway passes the whole MCP Apps surface through: tool `_meta`, the embedded
resource, `structuredContent`, and `resources/list` and `resources/read` with the `ui://`
URI unchanged.

AG-UI run: `POST https://chat.dengler.io/api/invocations`, token from
`test-token.sh`, `forwardedProps: {"project": "mcp-app"}`, message "Show me a card titled
Hello with the body It works". Script and output: `.deploy/phase2-agui.py`,
`.deploy/phase2-agui.txt` (trace `52adfbe53ef5418fa5e347d1dc5cdb63`), and
`.deploy/phase2-agui-result.txt` (the full `TOOL_CALL_RESULT` content).

| Event | Arrived | What it holds |
| --- | --- | --- |
| `RUN_STARTED`, `STATE_SNAPSHOT` | yes | |
| `TOOL_CALL_START` | yes | `toolCallName: mcp-app___show_card` |
| `TOOL_CALL_ARGS` | yes, 8 deltas | `{"title": "Hello", "body": "It works"}` |
| `TOOL_CALL_END` | yes | |
| `TOOL_CALL_RESULT` | yes | `content` is a JSON-encoded string whose value is exactly the card HTML with "Hello" and "It works" baked in (2681 chars decoded). The text block, the resource uri and mime type, `_meta` and `structuredContent` are not in it |
| `TEXT_MESSAGE_*` | yes | "Done. The card is displayed above." |
| `RUN_FINISHED` | yes | `outcome: success` |

Why the result holds only the HTML: `ag_ui_strands/agent.py` (around line 2277 in
guppi-gpt's venv) walks the Strands tool result's `text` items and keeps the last one.
Strands turns the embedded resource into a text item, so the HTML overwrites the text
block; `_meta` and `structuredContent` are not text items and are dropped.

Before the listing mode change, the same run (`.deploy/phase2-agui-dynamic.txt`) had no
tool call at all: the runtime log said `project mcp-app has no mcp_app___* tools on the
gateway; running with the retrieve tool`, and the model replied that it cannot show
cards.

## Delivery path the evidence supports

Path A, the embedded resource reaching the page through the agent, works today with no
platform change beyond phase 3's renderer. `TOOL_CALL_RESULT.content` for
`mcp-app___show_card` is the complete card document, and the document carries its own
values, so a renderer that puts that string in a sandboxed iframe shows the right card
after the two second fallback, or at once if the host answers `ui/initialize` and sends
`ui/notifications/tool-input` built from the `TOOL_CALL_ARGS` it already holds. What A
loses is the metadata: the renderer cannot learn from the event that the result is an MCP
App (no mime type, no `_meta`), so it has to key on the tool name prefix
(`manifest.mcp.toolPrefix`, which the phase 3 manifest would need as `mcp-app___`, not the
proposal's `mcpapp___`), and it cannot forward `structuredContent`.

Path B, the host reading `ui://` through the gateway, is open at the gateway: `tools/list`
carries `_meta.ui.resourceUri`, and `resources/read` returns the resource with its mime
type. It needs the browser to reach the tools gateway (the proposal's `/mcp/*` CloudFront
behavior, not built) and gives the page the full metadata. It is the spec-shaped path.

Path C, the agent reading the resource and relaying it, is possible (the agent's token
reaches `resources/read`) but is not needed while A delivers the HTML and B is available.

Recommendation for phase 3: build A first, keyed on the tool prefix, since it needs no new
CloudFront behavior; add B when an experiment needs `structuredContent` or tool `_meta`.
An agent-side change that emits the whole MCP result (for example as a `CUSTOM` event)
would give A the metadata as well.

## Blockers and work owed outside this repository

- guppi-gpt's agent reads only the first `tools/list` page. `list_tools_sync()` returns a
  page with `pagination_token`; the agent should loop until it is `None`. The DEFAULT
  listing mode here works around it, but a DYNAMIC target, or a first page that fills,
  hides project tools again. This is a guppi-gpt change and a platform deploy, so it was
  not made.
- `JWT_PASSTHROUGH` is not available for MCP server targets. If the server ever needs the
  user's identity, the options are an OAuth credential provider (a client-credentials
  client and a resource server in the platform's Cognito pool, which is a platform change)
  or passing identity another way.
- The platform proposal's manifest example (`toolPrefix: "mcpapp___"`) should read
  `mcp-app___` for this project.

## Manual checks for Sam

- [ ] Open `https://chat.dengler.io/p/mcp-app/`: the tab title, header and sign-in card
      say "MCP App Lab", and the reply label and placeholder say "Guppi".
- [ ] Sign in and ask "Show me a card titled Hello with the body It works". The status
      line shows the `mcp-app___show_card` tool call, and the reply's text mentions the
      card. The rendered card arrives in phase 3; today the reply shows only text.
- [ ] Ask a documentation question on the same page: `docs___Retrieve` still answers.
- [ ] Open `https://chat.dengler.io/`: GuppiGPT as before, with no `mcp-app___` tool
      offered (the default project sends no `forwardedProps.project`).
- [ ] Review the changes to the brief above, the `GATEWAY_IAM_ROLE` choice in particular.
