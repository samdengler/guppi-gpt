# Platform phase 1: session brief

Paste this into a Claude Code session started in this repository on the `platform`
branch. It is the standalone instruction for phase 1 of `docs/proposals/platform.md`;
read that file and `AGENTS.md` first.

---

Implement phase 1 of `docs/proposals/platform.md` on the `platform` branch, which already
exists and carries the proposal, the phase briefs, `scripts/overnight.sh`, and a
`--reuse-parameters` flag in `scripts/deploy.sh`, all uncommitted. Commit them first as
"Platform: proposal, phase briefs, overnight runner, and --reuse-parameters".

Rules that apply throughout, from `AGENTS.md`: no Lambda in the request path (CloudFront
Functions are not Lambda), secrets only as CloudFormation parameters, `uv run -- pytest`
and `uv run -- cdk synth -c image_uri=<any ecr uri>` green after every stack change, plain
text only on the page (no `innerHTML` with model or user text), no em-dashes or en-dashes
in prose, no second person. Do not edit `web/src/features.js`, `flags-core.js`,
`flags.js`, `flags.html` or their tests; another branch changes them.

This run is unattended. Never wait for an answer: when a step needs a decision the
proposal does not settle, make the smaller, reversible choice, record it and why in the
decision log's next revision, and continue. When a step is blocked by something outside
the repository (an expired AWS session, Docker not running, a push refused), write the
blocker and everything done so far to `docs/proposals/platform-phase-1-report.md`,
skip to the steps that do not depend on it, and finish with the report. The report is
what Sam reads in the morning: what landed, what was checked and how, what was skipped
and why, and the exact manual checks left for him. Never run `git push --force`, never
touch `main`, never run `cdk destroy`, and never edit resources outside the `GuppiGpt`
stack.

Work in this order, one commit per step, each step green before the next.

1. Stack: SSM parameters. Add the `/guppi/platform/...` parameters listed in the
   proposal's contract table as `ssm.StringParameter` resources beside the outputs in
   `infra/guppi_gpt_infra/stack.py`, names as constants (the tools gateway's role ARN
   included; the tools gateway role is what a project's MCP server Runtime must grant
   `InvokeAgentRuntime` to). Extend `infra/tests/test_stack.py`
   to assert the parameter names and that each points at the right resource attribute.
2. Stack: CloudFront Functions. One viewer-request function on the default behavior that
   rewrites `/p/<name>/` and `/p/<name>/index.html` to `/index.html` and leaves every other
   URI alone; one on the `/api/*` behavior that rewrites `/api/<name>/invocations` to
   `/<name>/invocations` and leaves `/api/invocations` and `/api/feedback` alone. Keep the
   function code in `infra/guppi_gpt_infra/functions/*.js` and load it with
   `cloudfront.FunctionCode.from_file`. Add synth tests that read the rendered template
   and check each behavior carries its function. Confirm `/api/feedback` still lists ahead
   of `/api/*`.
3. Page: project resolution and manifest. In `web/src/app.js`, before `config.json` is
   read, add `resolveProject()` (reads `<name>` from a `location.pathname` of the form
   `/p/<name>/`, else `null`) and `loadManifest(name)` (fetches
   `/projects/<name>/manifest.json` with `cache: "no-store"`; a missing manifest falls
   back to the default project and logs one console warning). Merge `manifest.features`
   over `config.features` before `initFeatures(config)`; set the header brand, sign-in
   title, tab title, reply label and composer placeholders from `manifest.label` and
   `manifest.assistant`; post to `manifest.agent`, with `"platform"` meaning
   `/api/invocations`. Put the resolver and the merge in `web/src/project.js` as pure
   functions with `node:test` coverage in `web/test/project.test.mjs`, following the
   existing tests' style.
4. Page: the sign-in round trip. `startSignIn` puts the current path in the OAuth
   `state` parameter; `finishSignIn` reads `state` from the URL and passes it to
   `history.replaceState` in place of `location.pathname`. Only paths of the form
   `/p/<name>/` or `/` are accepted from `state`; anything else becomes `/`. Cover the
   acceptance rule with a test.
5. Page: extension points. Add `web/src/extensions.js` with the `guppi` object from the
   proposal's API table (renderer registry for tool names and event types, `onSend`
   hooks, `status`, `token`, `project`; `mcp` is `null` in this phase). In `addTurn`, add a
   `reply-attachments` element after `reply-text`. In `runTurn`, apply `onSend` hooks to
   the run input, and dispatch `TOOL_CALL_END`, `TOOL_CALL_RESULT`, `CUSTOM`,
   `STEP_STARTED`, `STEP_FINISHED`, `STATE_SNAPSHOT` and `STATE_DELTA` to registered
   renderers with the reply's slot; the built-in status line behavior for tool calls
   stays as it is when no renderer claims the tool. When `manifest.extension` is set,
   `installExtension(url)` does `await import(url)` and calls the default export with
   `guppi`; a failed import logs one warning and the page continues. Confirm esbuild keeps
   the dynamic import native in the IIFE bundle.
6. Agent: project prefix filter. In `agent/src/guppi_agent/agent.py`, read
   `forwardedProps.project` from the run input; when set, keep tools whose name starts
   with `<project>___` (hyphens replaced by underscores) beside `RETRIEVE_TOOL`, and add
   one sentence to the system prompt naming the project's tools. The page sends
   `forwardedProps: { project: manifest.name }` on every run of a project page. Tests in
   `agent/tests` cover the filter with a fake tool list, never reaching the gateway.
7. Kit metadata. In `agent/pyproject.toml`, make `guppi-agent` installable as a package
   by git URL (name, version, `[tool.uv]` or build backend as the workspace already
   uses), without changing how the Dockerfile installs it. Verify with `uv pip install
   "guppi-agent @ git+file://$(pwd)#subdirectory=agent"` in a throwaway venv.
8. Test session helper. Add `scripts/test-token.sh`, committed, no secrets inside: it
   reads the newest refresh token from `$HOME/.config/guppi/test-session.json` (seeded by
   `scripts/overnight.sh` from 1Password before the run), posts a `refresh_token` grant to
   `https://$GUPPI_AUTH_DOMAIN/oauth2/token` with `client_id=$GUPPI_USER_POOL_CLIENT_ID`,
   writes any rotated refresh token back to that file (mode 600), and prints the access
   token on stdout and nothing else. Every later step and phase that needs a token calls
   this script. Tokens never appear in logs, reports, commits or test fixtures. Document
   it in `AGENTS.md` beside the deploy notes.
9. Docs. Update `AGENTS.md` (project structure for the new files, the two functions, the
   SSM parameters, the `forwardedProps.project` filter) and `README.md` (one paragraph
   under the stack overview pointing at `docs/proposals/platform.md`). Add a decision log
   revision recording the platform decision and the alternatives the proposal rejects.
10. Deploy and check. `scripts/deploy.sh --reuse-parameters`, following
   `.deploy/latest.log` until its `deploy exit=0` line. Then write
   `projects/demo/manifest.json` with `name: "demo"`, `label: "Demo"` and
   `agent: "platform"`, upload it to the site bucket under that key, and invalidate
   `/projects/demo/*`. Check without a browser, with curl against `https://chat.dengler.io`:
   `/` and `/p/demo/` both return the page bundle with status 200; `/projects/demo/manifest.json`
   returns the JSON; `/api/invocations` and `/api/demo/invocations` with no bearer both
   return the gateway's 401 or 403 (proof the rewrite reaches the gateway rather than S3);
   `/api/feedback` still answers from API Gateway. Then, with a token from
   `scripts/test-token.sh`, post one AG-UI run (a single user message, `forwardedProps`
   `{ "project": "demo" }`) to `/api/invocations` and confirm the SSE stream carries
   `RUN_STARTED`, text content and `RUN_FINISHED`. Record each URL and status in the
   report. Leave the demo prefix in place. The signed-in checks (the Demo brand shows, sign-in
   returns to `/p/demo/`, a question is answered through the platform agent, and `/` is
   unchanged) are Sam's morning checks; list them in the report as a checklist.

Done when steps 1 to 10 hold, the branch is pushed, and
`docs/proposals/platform-phase-1-report.md` is committed on it. Do not merge to `main`;
Sam reviews the branch first.
