# Platform phase 5a: what an agent project needs

The standalone instruction for the platform half of phase 5 of
`docs/proposals/platform.md`, on the `platform` branch. Read that file, `AGENTS.md` and
the three phase reports under `docs/proposals/` first. hr-super-agent becomes the first
agent project right after this phase (`../hr-super-agent/docs/phase-8.md`), and every
step here is something that project needs from the platform.

The unattended rules from `docs/proposals/platform-phase-1.md` apply unchanged: no
waiting for answers, reversible choices recorded in the decision log, blockers to the
report, no force push, no `main`, no `cdk destroy`, nothing outside the `GuppiGpt` stack,
tokens never in logs, reports, commits or fixtures, and the flag files untouched. If
`docs/proposals/platform-phase-5a-report.md` already exists, continue from its first
unfinished step.

Work in this order, one commit per step, each step green before the next.

1. Agent kit: an app factory. `guppi_agent/app.py` gains `create_app(build_agent)` that
   returns a FastAPI app on the AG-UI contract using the given `build_strands_agent`
   callable; the module-level `app` becomes `create_app(agent.build_strands_agent)` so
   this repository's Dockerfile and tests are unchanged. Everything a project needs
   (`create_app`, `with_keepalive`, `trim_messages`, `validate_run`, `conversation_log`,
   `app_resource`, `with_app_resources`) is importable from `guppi_agent`. A test builds
   an app with a fake agent through the factory.
2. Agent: every `tools/list` page. `StrandsRun.run` loops `list_tools_sync` with its
   `pagination_token` until it is `None`, and `select_tools` sees the whole list. The
   phase 2 report records why: a `DYNAMIC` target hid `mcp-app___show_card` on page two.
   Test with a fake client that pages.
3. Page: status lines by tool name. The built-in status text is chosen by the tool's
   name: `docs___*` keeps "Searching the knowledge base…" and "Searched the knowledge
   base"; any other tool reads "Using <name>…" and "Used <name>", with the target prefix
   (`<target>___`) removed and underscores as spaces. Pure function in `web/src/copy.js`
   with tests. `guppi.status(text)` from an extension still wins.
4. Page: what an agent project's extension needs. In `web/src/extensions.js` and
   `app.js`: `TOOL_CALL_START` and `TOOL_CALL_END` join `EXTENSION_EVENT_TYPES` (a
   renderer sees them before the built-in status line runs; `renderTool` is unchanged);
   the render context gains `setLabel(text)`, which sets the running reply's label
   (`addTurn` returns the label element); `guppi.onThread(fn)` registers a callback the
   page calls with `{ threadId }` on a new chat, a resumed or switched thread, and the
   first load, so an extension can drop per-thread state. `applySendHooks` already
   passes `state`; confirm a hook can set it and that the value reaches the wire as
   AG-UI `state`. Tests for each in `web/test/extensions.test.mjs`.
5. Kit tag. With `uv run -- pytest` and `npm test` green, tag the branch `kit-v0.2.0`
   and push the tag; hr-super-agent depends on
   `guppi-agent @ git+https://github.com/samdengler/guppi-gpt@kit-v0.2.0#subdirectory=agent`.
   `agent/README.md` shows a project's `app.py` (`app = create_app(build_strands_agent)`)
   and the Dockerfile line that runs it.
6. Docs. `AGENTS.md` for the factory, the pagination, the status rule and the new
   extension members; `docs/proposals/platform.md`'s extension API table; decision log
   revision 21. Then the owed update to `docs/guppigpt-design.html`: a "Platform"
   section (projects, routing, the manifest, the extension API, the MCP Apps host and
   the sandbox route), with the architecture and request flow sections corrected where
   they say the page talks only to the Guppi agent. Keep the document's own HTML
   conventions; prose with no em-dashes or en-dashes and no second person.
7. Deploy and check. `scripts/deploy.sh --reuse-parameters --require-approval never`
   (the agent image changed) to `deploy exit=0`. With a token from
   `scripts/test-token.sh`, post the phase 2 run for project `mcp-app` and confirm the
   card's `CUSTOM` event still arrives; post a docs question on `/` and confirm
   `RUN_FINISHED`. Curl `/`, `/p/mcp-app/` and `/projects/mcp-app/manifest.json` for 200.
8. Report. `docs/proposals/platform-phase-5a-report.md`: what landed, the tag, checks,
   decisions, blockers, and Sam's manual checks. Commit and push.

Done when steps 1 to 8 hold and the report is committed.
