# Phase 2: guppi-mcp-app, a tools-only project

The standalone instruction for phase 2 of the platform design, which lives in the
guppi-gpt repository at `../guppi-gpt/docs/proposals/platform.md`. Read that file, then
`../guppi-gpt/AGENTS.md`, then `../guppi-gpt/docs/proposals/platform-phase-1-report.md`.
If the phase 1 report says the SSM parameters or the agent's project filter did not land,
write `docs/phase-2-report.md` saying so and stop; everything below depends on them.

This repository is empty: no commits, no remote. This run is unattended. Never wait for
an answer: when a step needs a decision this brief does not settle, make the smaller,
reversible choice, record it and why in `docs/decision-log.md`, and continue. When a
step is blocked by something outside the repository, write the blocker and everything
done so far to `docs/phase-2-report.md`, skip to the steps that do not depend on it, and
finish with the report. Never touch guppi-gpt's `main`, never run `cdk destroy`, never
edit resources outside the `GuppiMcpApp` stack. Tokens from
`../guppi-gpt/scripts/test-token.sh` never appear in logs, reports, commits or fixtures.
If `docs/phase-2-report.md` already exists, continue from its first unfinished step.

The rules in guppi-gpt's `AGENTS.md` apply here (no Lambda without approval, secrets only
as parameters, plain text on the page, no em-dashes or en-dashes, no second person), and
this repository gets its own `AGENTS.md` stating them plus its own layout, with a
`CLAUDE.md` that points at it, the same as guppi-gpt.

Work in this order, one commit per step, each step green before the next.

1. Repository. Create the layout from the proposal: `infra/` (a CDK app in Python, a uv
   workspace like guppi-gpt's, one stack `GuppiMcpApp`), `server/` (the MCP server, a
   workspace member with its own `Dockerfile`), `web/` (`manifest.json`; `src/ext.js`
   only when needed, not in this phase), `scripts/deploy.sh`, `docs/` (this brief,
   `decision-log.md`, later the report), `AGENTS.md`, `CLAUDE.md`, `README.md`,
   `.gitignore` and `.dockerignore` modeled on guppi-gpt's. Commit as "Phase 2: repository
   layout". Then `gh repo create samdengler/guppi-mcp-app --private --source=. --remote=origin --push`;
   if `gh` refuses, keep committing locally and record it in the report.
2. Server. `server/src/mcp_app_server/`: a Python MCP server on the official `mcp` SDK
   (FastMCP), streamable HTTP, stateless, bound to `0.0.0.0:8000` at path `/mcp`, which is
   the AgentCore Runtime contract for protocol MCP. One tool, `show_card(title: str,
   body: str)`, whose result carries three things: a text content block that says the
   card was shown, an embedded resource content block (`type: "resource"`, uri
   `ui://mcp-app/card`, mime type `text/html`, the card HTML as text), and tool
   metadata that names that resource for an MCP Apps host. Look up the exact `_meta` key
   and the resource mime type profile in the MCP Apps extension repository
   (https://github.com/modelcontextprotocol/ext-apps, SEP-1865) and use those; record
   what was used. The same resource is served by `resources/list` and `resources/read`.
   The HTML is one self-contained page: it renders a title and a body from the tool
   result it receives from the host over `postMessage` per the extension, and when no
   host message arrives within two seconds it renders the values baked in at call time,
   so the page works on every delivery path. No external scripts, no network calls.
   Tests in `server/tests` call the tool and read the resource through an in-process MCP
   client. Commit as "Phase 2: MCP server with show_card".
3. Local check. `docker build` for arm64 with Colima, run the container, and
   `scripts/probe.py` (a small Python MCP client that takes a URL and an optional bearer
   token, then runs `tools/list`, `tools/call show_card`, `resources/list` and
   `resources/read ui://mcp-app/card` and prints what came back) against
   `http://localhost:8000/mcp`. All four work. Commit the probe.
4. Stack. `infra/`: read the platform's `/guppi/platform/*` parameters with
   `ssm.StringParameter.value_for_string_parameter`. Build the server image with
   `DockerImageAsset` (arm64, the same pattern as guppi-gpt's Agent image section). A
   `CfnRuntime` with protocol `MCP`, a JWT authorizer on the platform's discovery URL and
   client id, the request header allowlist naming `Authorization`, and the platform's
   tools gateway ARN in `allowed_workload_configuration` only if guppi-gpt's decision log
   says that binding works (it did not for the AG-UI runtime; default to leaving it off
   and record). A `CfnGatewayTarget` on the platform's tools gateway named `mcpapp`
   (target names are letters, digits and underscores, so the tool prefix is
   `mcpapp___`), with the MCP target configuration that addresses a Runtime MCP endpoint:
   inspect `aws_cdk.aws_bedrockagentcore.CfnGatewayTarget.McpTargetConfigurationProperty`
   in the installed `aws-cdk-lib` for the property that takes a Runtime ARN or an MCP
   server endpoint, pick the one that fits, record it. Credential provider
   `JWT_PASSTHROUGH`, so the user's token reaches the server. Grant
   `bedrock-agentcore:InvokeAgentRuntime` on the Runtime to the platform's tools gateway
   role, imported by ARN from SSM. Outputs: runtime ARN, target name. Tests assert the
   target's gateway identifier is the SSM value and the runtime protocol is MCP.
   `uv run -- pytest` and `uv run -- cdk synth -c image_uri=<any ecr uri>` pass. Commit.
5. Manifest and deploy script. `web/manifest.json`: `name` `mcp-app`, `label`
   `MCP App Lab`, `assistant` `Guppi`, `agent` `"platform"`, `capabilities`
   `["mcp-apps"]`, no `extension`, no `mcp`. `scripts/deploy.sh`: `cdk deploy` (no
   1Password; this stack has no secret parameters), then sync `web/` (only
   `manifest.json` in this phase) to `s3://<site-bucket>/projects/mcp-app/` and invalidate
   `/projects/mcp-app/*`, with the bucket and distribution read from SSM by the AWS CLI;
   `--site-only` skips the deploy, the same as guppi-gpt. Log to `.deploy/` the same
   way. Commit.
6. Deploy. `scripts/deploy.sh`, following `.deploy/latest.log` to `deploy exit=0`.
7. Checks through the platform, with a token from `../guppi-gpt/scripts/test-token.sh`
   and the tools gateway URL from SSM. Run `scripts/probe.py` against the gateway and
   record, in a table in the report (call, works or not, evidence): whether `tools/list`
   shows `mcpapp___show_card` beside `docs___Retrieve`; whether `tools/call` returns the
   text block, the embedded resource block, and the tool `_meta`, or only some of them;
   whether `resources/list` and `resources/read` work through the gateway at all. Then
   post one AG-UI run to `https://chat.dengler.io/api/invocations` with `forwardedProps`
   `{ "project": "mcp-app" }` and the message "Show me a card titled Hello with the body
   It works", and record which AG-UI events arrive (`TOOL_CALL_START`, `TOOL_CALL_END`,
   `TOOL_CALL_RESULT` and what its content holds, text, `RUN_FINISHED`). This table is
   what phase 3 reads to choose the delivery path, so state plainly which of the three
   paths in the proposal the evidence supports: A, the embedded resource in the tool
   result reaching the page through the agent; B, the host reading `ui://` through the
   gateway; C, the agent reading the resource and relaying it.
8. Report. `docs/phase-2-report.md`: what landed, the table from step 7, decisions made,
   blockers, and the manual checks left for Sam (open `https://chat.dengler.io/p/mcp-app/`,
   see the MCP App Lab brand, ask for a card, and confirm the reply's text mentions the
   card; the rendered card arrives in phase 3). Commit and push.

Done when steps 1 to 8 hold and the report is committed.
