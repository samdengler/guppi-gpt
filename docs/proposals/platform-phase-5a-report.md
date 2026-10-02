# Platform phase 5a report

Run interactively on 2 October 2026 from a Claude Code session, on the `platform` branch,
following `platform-phase-5a.md` step by step.

## What landed

| Step | Commit | Change |
| --- | --- | --- |
| 1 | `5d66058` | `create_app(build_agent)` in `guppi_agent/app.py`; the module-level `app` is `create_app()` with the builder looked up at call time, so tests that replace `agent.build_strands_agent` still work. `guppi_agent` exports `create_app`, `with_keepalive`, `trim_messages`, `validate_run`, `conversation_log`, `app_resource`, `with_app_resources` |
| 2 | `3a31ef4` | `list_all_tools` follows `tools/list` pagination to the last page, at most 50 |
| 3 | `317161c` | `toolStatus` and `toolDisplayName` in `web/src/copy.js`; the page's status line names the tool, `docs___*` keeps the knowledge base wording |
| 4 | `358109c` | `TOOL_CALL_START` and `TOOL_CALL_END` in `EXTENSION_EVENT_TYPES` (a renderer that takes one leaves that call's status line to the extension); `setLabel` on the render context (`addTurn` returns the label); `guppi.onThread`; an `onSend` hook's `state` reaches the wire through the `HttpAgent` initial state |
| 5 | `763fe42`, tag `kit-v0.2.0` | `agent/README.md` shows a project's `app.py` and Dockerfile line; tag pushed |
| 6 | `5344e76` | AGENTS.md, platform.md's extension API table, decision log revision 21, `guppigpt-design.html` section 13 "Platform" with later sections renumbered and the architecture paragraph naming the project targets |

Also on the branch the same morning, before this phase: `06140c8`, Sky as the page's
default palette.

## Checks

- `uv run -- pytest`: 132 passed. `npm test` in `web/`: 146 passed. `npm run build` clean.
- `scripts/deploy.sh --reuse-parameters --require-approval never`: `deploy exit=0`, the
  agent image rebuilt and the page republished.
- `https://chat.dengler.io/`, `/p/mcp-app/` and `/projects/mcp-app/manifest.json`: 200.
- With a token from `scripts/test-token.sh`, the phase 2 run for project `mcp-app`
  ("Show me a card titled Hello with the body It works"): `mcp-app___show_card` called and
  the `CUSTOM` event `mcp-app/resource` arrived, then `RUN_FINISHED`.
- A docs question on `/`: `docs___Retrieve` called, `RUN_FINISHED`.

## Decisions

- The factory's argument is any `build_agent(token)` whose result has an async `run`
  yielding AG-UI events, not only a Strands agent; hr-super-agent's Orchestrator and the
  planned guppi-connect bridge both fit without a kit change.
- The pagination cap is 50 pages with a warning past it, so a misbehaving gateway cannot
  hold a run in a loop.
- A tool call event an extension renderer takes suppresses only that call's built-in
  status line; tool renderers by name (`renderTool`) are unchanged.

## Blockers

None.

## For Sam

- Check `/p/mcp-app/` in the browser: the status line should read "Using show card…" then
  "Used show card".
