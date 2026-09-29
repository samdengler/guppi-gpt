# Platform phase 1: report

Run of `docs/proposals/platform-phase-1.md` on the `platform` branch, 28 Sep 2026 (UTC
29 Sep), unattended. Every step landed, the stack is deployed from this branch, and the
branch is pushed. Nothing was blocked. `main` is untouched and nothing was merged.

## Steps and commits

| Step | State | Commit |
| --- | --- | --- |
| 0. Proposal, briefs, overnight runner, `--reuse-parameters` | done | `0eb4efb` |
| 1. `/guppi/platform/...` SSM parameters | done | `906989c` |
| 2. CloudFront Functions for the page and agent paths | done | `827d212` |
| 3. Project resolution and manifest in the page | done | `957cc41` |
| 4. Sign-in round trip through `state` | done | `4fe9794` |
| 5. Extension registry, reply slot, `ext.js` import | done | `0a9247a` |
| 6. Agent project prefix filter, `forwardedProps.project` from the page | done | `215210b`, `ec736c4` |
| 7. `guppi-agent` installable by git URL | done | `006da74` |
| 8. `scripts/test-token.sh` | done | `34da670` |
| 9. AGENTS.md, README, decision log revision 19 | done | `837f075` |
| 10. Deploy and checks | done | `136efcd` (deploy sync fix), this report |

`uv run -- pytest` (113 tests), `npm test` in `web/` (107 tests), `uv run -- ruff check .`,
`ruff format --check`, and `cdk synth -c image_uri=<ecr uri>` were green after every
step that touched their area.

## What landed

Stack. Fourteen `ssm.StringParameter` resources under `/guppi/platform/`, names as
`PARAM_*` constants in `stack.py`, the tools gateway role ARN included. Two viewer request
CloudFront Functions (`cloudfront-js-2.0`, code in `infra/guppi_gpt_infra/functions/`,
loaded with `FunctionCode.from_file`): `page-path.js` on the default behavior and
`agent-path.js` on `/api/*`. `/api/feedback` is still the first behavior and carries no
function. Synth tests check the names, that each parameter points at the right resource
attribute (the role parameters are compared with the gateways' own `RoleArn`), that each
behavior carries its function, and the behavior order. `web/test/cloudfront-functions.test.mjs`
also runs both function files against sample URIs.

Page. `web/src/project.js` (pure: `resolveProject`, `projectPath`, `acceptedReturnPath`,
`manifestUrl`, `checkManifest`, `mergeFeatures`, `brandFor`, `agentUrlFor`) with
`web/test/project.test.mjs`. `app.js` gains `resolveProject()`, `loadManifest(name)` and
`installExtension(url)`, sets the tab title, header brand, sign-in title, reply label and
composer placeholders, posts to the manifest's agent, sends
`forwardedProps: { project: <name> }` on a project page, and carries the path in the
OAuth `state`. `web/src/extensions.js` builds the `guppi` object with
`web/test/extensions.test.mjs`. Each reply has a `reply-attachments` element after
`reply-text` (hidden while empty). The esbuild IIFE bundle keeps the dynamic import native
(`import(b)` in `dist/app.js`).

Agent. `select_tools`, `run_project` and `project_tool_prefix` in `agent.py`;
`system_prompt(project, project_tools)` adds one sentence naming the project's tools.
`agent/tests/test_project_tools.py` covers the filter with fake tool lists and fake
Strands classes.

Scripts and docs. `scripts/test-token.sh`; `scripts/deploy.sh` now excludes
`projects/*` from its `aws s3 sync --delete`; AGENTS.md, README.md, `agent/README.md`,
and decision log revision 19.

## Decisions taken without Sam

Each is the smaller, reversible option; all are recorded in decision log revision 19.

- `index.html` now addresses `app.css`, `app.js`, `privacy.html` and `terms.html` from the
  root, and the page fetches `/config.json`. Relative paths resolve under `/p/<name>/`,
  which the page path function leaves alone, so they would 403.
- The page that receives the sign-in code (always `/`) resolves the project from `state`,
  so the project brand shows from the first paint; `finishSignIn` then replaces the URL
  with the accepted path, as the brief describes.
- A manifest is used only when `name` matches the path, `label` is set, and `agent` is
  `"platform"` or a same-origin `/api/<name>/invocations` path (the page sends the bearer
  there). Anything else falls back to the default project with one console warning.
- The tool filter matches `<name>___` with hyphens replaced by underscores, as the brief
  says, and also the name as given. The design says hyphens become underscores only where
  AgentCore requires it, so a target named `mcp-app` could keep the hyphen. Phase 2 should
  note that the proposal's manifest example uses `toolPrefix: "mcpapp___"`, which neither
  form produces for a project named `mcp-app`.
- The agent's keepalive `ping` never reaches a `CUSTOM` renderer.
- `agent-path.js` also leaves `/api/invocations/invocations` and `/api/feedback/invocations`
  alone, the two names a project cannot take.
- An `onSend` hook can change only `forwardedProps` and `state`; the thread, run id and
  messages stay the page's.
- The deploy script's `aws s3 sync --delete` excludes `projects/*`. Without it, every
  platform deploy would delete every project's published files, including the demo
  manifest. Found while preparing the deploy.
- `agent/pyproject.toml` already built and installed by git URL (hatchling, `src/guppi_agent`);
  the change adds `readme` and the repository URL, and `uv lock --check` stayed clean.
- The demo manifest was written to `.deploy/projects/demo/manifest.json` (gitignored) and
  not committed, since it belongs to no project in this repository.

## Checks and results

Deploy: `scripts/deploy.sh --reuse-parameters --require-approval never`, log
`.deploy/deploy-20260928-230659.log`, ended `deploy exit=0`. The 14 parameters exist in
SSM; `site-bucket-name` matches the stack output. The demo manifest
(`{"name":"demo","label":"Demo","agent":"platform"}`) was uploaded to
`s3://<site bucket>/projects/demo/manifest.json` with `Cache-Control: no-store`, and
`/projects/demo/*` was invalidated (`I7MK4IZEYG922PIW2TF9RGJD5F`, completed). The demo
prefix is left in place.

curl against `https://chat.dengler.io`, before the deploy and after it (script and raw
output in `.deploy/phase1-checks.sh`, `.deploy/phase1-baseline.txt`,
`.deploy/phase1-after.txt`):

| Request | Before | After |
| --- | --- | --- |
| `GET /` | 200 `text/html` | 200 `text/html`, the new `index.html` |
| `GET /p/demo/` | 403 from S3 | 200 `text/html`, the same `index.html` |
| `GET /p/demo/index.html` | 403 from S3 | 200 `text/html`, the same `index.html` |
| `GET /projects/demo/manifest.json` | 403 (not uploaded) | 200 `application/json`, the manifest |
| `POST /api/invocations`, no bearer | 401 `Missing Bearer token` | 401 `Missing Bearer token` (gateway) |
| `POST /api/demo/invocations`, no bearer | 401 `Missing Bearer token` | 401 `Missing Bearer token` (gateway) |
| `POST /api/feedback`, no bearer | 401, `x-amz-apigw-id` present | 401 `UnauthorizedException`, `x-amz-apigw-id` present (API Gateway) |
| `POST /api/demo/invocations`, bearer | 404 `<UnknownOperationException/>` | 404 `No Target found for Target name: demo` |

The gateway answers 401 to a missing bearer before it routes, so the no-bearer rows are
the same either side of the deploy. The signed-in row is the proof of the rewrite: before
the deploy the gateway received `/api/demo/invocations`; after it, the gateway looks for
a target named `demo`.

AG-UI run: one `POST /api/invocations` with a token from `scripts/test-token.sh`, one user
message ("In one sentence, what is the AG-UI protocol?") and
`forwardedProps: {"project": "demo"}`, trace id `3e3e4ab6198cb479a369a7b39726cd1a`.
Status 200 `text/event-stream`; events in order: `RUN_STARTED`, `STATE_SNAPSHOT`, a
`docs___Retrieve` tool call (start, 10 args, end, result), `TEXT_MESSAGE_START`, 30
`TEXT_MESSAGE_CONTENT`, `TEXT_MESSAGE_END`, `STATE_SNAPSHOT`, `RUN_FINISHED`. The runtime
log on that trace id holds `project demo has no demo___* tools on the gateway; running
with the retrieve tool`, so the deployed agent read the project from the run.

Headless Chrome over the DevTools protocol, signed out, against the live site: `/` shows
title, brand and sign-in title "GuppiGPT" and placeholder "Ask GuppiGPT"; `/p/demo/`
shows "Demo" in all four. Clicking "Continue with Google" sent Cognito
`state=/p/demo/` from `/p/demo/` and `state=/` from `/`, both with
`redirect_uri=https://chat.dengler.io/`. Before the deploy, the same checks ran against
a local copy of `web/dist` behind a small server that mimics the page path function: a
missing manifest logged one warning and fell back to GuppiGPT, a test `ext.js` received
`guppi` with the demo manifest and `mcp` null, and a missing `ext.js` logged one warning
and the page carried on. `chrome --headless --dump-dom` returns before the page finishes
booting (it did so for `main`'s build too), so those checks drive Chrome through the
DevTools protocol instead.

`test-token.sh`: exit 0, a three-part access JWT (`token_use` access, 3599 seconds left),
and the session file was rewritten with the rotated refresh token at mode 600. No token
was printed, logged, or committed.

Kit: `uv pip install "guppi-agent @ git+file://$(pwd)#subdirectory=agent"` into a
throwaway Python 3.12 venv installed `guppi-agent 0.1.0` with `app`, `agent`,
`keepalive`, `validation` and `conversation_log` importable, the README as its long
description, and the repository URL.

## Not done, and why

- `docs/guppigpt-design.html` does not describe the platform yet. The brief scoped the
  docs step to AGENTS.md, the README and the decision log; the design document needs its
  routing, page and agent sections updated before the branch merges.
- The proposal's factoring of `agent.py` so a project supplies its own
  `build_strands_agent` is not part of the phase 1 brief and was not started.
- `manifest.extension` is not restricted beyond the CSP (`script-src 'self'`), which
  already limits it to this origin.
- The Claude Doc linked from the proposal was read for its alternatives table and not
  changed.

## Morning checks for Sam

- [ ] Open `https://chat.dengler.io/p/demo/` signed out: the tab title, header and
      sign-in card say "Demo".
- [ ] Sign in from there: Google returns to `https://chat.dengler.io/p/demo/` (not `/`),
      with "Demo" still in the header and no `code` or `state` left in the address bar.
- [ ] Ask a documentation question there: the reply label and placeholder say "Demo", the
      status line shows the knowledge base search, and the answer arrives through the
      platform agent (the request in the network panel goes to `/api/invocations` with
      `forwardedProps.project` set to `demo`).
- [ ] Open `https://chat.dengler.io/` in another tab: GuppiGPT exactly as before (brand,
      placeholders, sign-in state, a question answered, the feedback control if the flag
      is on).
- [ ] Sign out from `/p/demo/` and sign back in from `/`: it returns to `/`.
- [ ] Review the branch before merging, including the decisions above and the design
      document update still owed.
