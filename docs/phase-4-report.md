# Phase 4: report

Run of `docs/phase-4.md`, 29 Sep 2026 (commits 00:10 to 00:35 Eastern), unattended. Steps
1 to 8 hold. `docs/experiments.md` has a row for each of E1 to E6. One works here (E1),
three work partly (E3, E4, E6), and two are blocked on platform changes (E2, E5). Each row
names the change. No experiment depended on another that was blocked, so none was
skipped. Nothing in guppi-gpt was edited or deployed; its `platform` branch was only read.
Only the `GuppiMcpApp` stack and its own gateway target were changed.

## Steps and commits

| Step | State | Commit |
| --- | --- | --- |
| 1. E1, embedded resource (baseline) | done: works | `35c9bb6` |
| 2. E2, host reads `ui://` by `resources/read` | done: blocked | `ca95006` |
| 3. E3, app calls a tool | done: partly | `e121772` |
| 4. E4, a later tool updates the same app | done: partly | `e93118c` |
| 5. E5, an app served as a static page | done: blocked | `efcbb50` |
| 6. E6, a form that returns data to the model | done: partly | `7ec11d8` |
| 7. Deploy, target sync, probe, browser checks | done | `9ac0e39` |
| 8. Report, AGENTS.md, READMEs | done | this commit |

`uv run -- pytest` (22 tests), `uv run -- ruff check .`, `ruff format --check` and
`cdk synth -c image_uri=<ecr uri>` were green after every commit that touched their area.
Every commit is pushed to `origin/main`.

## What landed

Server. Five tools beside `show_card`, one per experiment: `show_chart(values)` (E2,
names `ui://mcp-app/chart` without embedding it), `card_clicked(card_id)` (E3, app-only by
`_meta.ui.visibility`, one audit line per call on stdout), `update_card(card_id, body)`
(E4, the same card resource and id again), `show_static_page()` (E5, a `text/uri-list`
resource), and `ask_preferences()` (E6, a two-field form). Four `ui://mcp-app/` resources
in `resources/list` and `resources/read`: `card`, `chart`, `preferences` (all
`text/html;profile=mcp-app`) and `static-page` (`text/uri-list`). The pages share one
app-side bridge (`bridge.py`). The card gained an id (its title as a slug), a "Record a
click" button, and a status line that also reports a second `tool-result` in the same
frame.

Site. `web/app/` (`index.html`, `app.css`, `app.js`), published to
`projects/mcp-app/app/`. Opened directly it says "Opened on its own: no MCP Apps host
around this page", with no console errors.

Scripts. `scripts/probe.py` takes `--call NAME JSON` for more tool calls and reads every
listed `ui://mcp-app/` resource. `scripts/deploy.sh` synchronizes the `mcp-app` gateway
target after `cdk deploy`. `scripts/browser-check.mjs` runs each experiment on the live
page and records the frames, what each app shows, the replies, and each run's AG-UI
events.

Docs. `docs/experiments.md` (the comparison, a section per experiment, and the findings
that cut across them), this report, the phase 4 section of `docs/decision-log.md`,
AGENTS.md and both READMEs.

## Checks and results

Deploy. `scripts/deploy.sh --require-approval never`, `.deploy/deploy-20260929-002403.log`,
`deploy exit=0`. A second run after the sync step was added ended `deploy exit=0` with
"GuppiMcpApp (no changes)" and "target mcp-app (4OHQJWHN5L) after sync: READY".

Gateway probe (`.deploy/phase4-gateway-probe.txt`, no token in it). Before the target sync
the gateway listed only `mcp-app___show_card` and answered `-32602 Unknown tool` for the
new tools. After `synchronize-gateway-targets` on the `mcp-app` target: one `tools/list`
page with `docs___AgenticRetrieveStream`, `docs___Retrieve` and all six `mcp-app___`
tools, `_meta.ui.visibility` kept on `card_clicked`; all six tools answered `tools/call`;
`resources/list` held the four resources; all four `resources/read` calls returned their
content with uri and mime type intact, the static page as its URL. 12 of 12 calls
answered. The `card_clicked` audit line is in the Runtime's log
(`.deploy/phase4-E3-audit.txt`).

Browser check (`node scripts/browser-check.mjs`, exit 0, `.deploy/phase4-browser.txt`).
Each experiment ran in a fresh signed-in context on `/p/mcp-app/`.

| Experiment | Tool the model called | `mcp-app/resource` event | Frames | What the app showed |
| --- | --- | --- | --- | --- |
| E1 | `show_card` | yes, `ui://mcp-app/card` | 1, 151 pixels | "Hello", "It works" |
| E2 | `show_chart` | no | 0 | nothing; the reply said the chart was shown |
| E3 | `show_card` | yes | 1 | after a press: "Host refused tools/call: JSON-RPC error -32601: tools/call is not available on this host yet" |
| E4 | `show_card`, then `update_card` (`draft`) | yes, both | 2 | first "Draft", "First version" (unchanged); second "Card draft", "Second version" |
| E5 | `show_static_page` | no | 0 | nothing; the reply said the page was shown |
| E6 | `ask_preferences`, then no tool | yes | 1 | after "Sam", "Detailed" and a press: "Host refused ui/update-model-context: JSON-RPC error -32601: Method not found: ui/update-model-context"; the follow-up reply said the model had no access to the answers |

Screenshots `.deploy/phase-4-E1.png` to `.deploy/phase-4-E6.png` (gitignored, on Sam's
Mac). The console showed no CSP violation and no page or sandbox error; the only errors
were the Dynatrace beacon's CORS refusals, as in phase 3. Every refresh token rotation
was written back to the test session file; no token was printed, logged or committed.

Route checks for E2 (`.deploy/phase4-E2-routes.txt`). The tools gateway and the Runtime
endpoint both answer a CORS preflight from `https://chat.dengler.io` with
`access-control-allow-origin: *`. The Runtime refuses a request without SigV4. The page's
`connect-src` is `'self' https://auth.dengler.io` plus the Dynatrace beacon, and
`/projects/*` is served with `X-Frame-Options: DENY` and `frame-ancestors 'none'`.

## Decisions

All are in the phase 4 section of `docs/decision-log.md`. The ones Sam may want to
revisit:

- Card ids are the card's title as a slug, so the model can name a card again from the
  conversation text alone.
- E6 uses `ui/update-model-context` rather than `ui/message`.
- The E6 form uses a button click, since the host's sandbox blocks form submission.
- `scripts/deploy.sh` now synchronizes the gateway target after every stack deploy.
- The browser check lives in this repository and reads Playwright and stack outputs from
  `../guppi-gpt`.

## Blockers and the platform changes they need

None of these was built; each belongs to guppi-gpt. In the order that unblocks the most:

1. A browser MCP client and a route to the tools gateway (E2, and the relay for E3).
   `guppi.mcp` bound to `manifest.mcp`, running `initialize`, `tools/list` and
   `resources/read` with the user's token; the host reads a tool's `ui://` resource
   through it when the listed definition names one. The route is either the `/mcp/*`
   behavior or the gateway's host in the page CSP's `connect-src`; the gateway already
   answers CORS preflights from the site.
2. A `tools/call` relay in the host (E3): check the tool's `_meta.ui.visibility` includes
   `app`, add `mcp-app___`, call through `guppi.mcp`, return the result to the app, and
   declare `hostCapabilities.serverTools`.
3. The platform agent leaving `visibility: ["app"]` tools out of the model's list (E3).
   Today the model is offered `mcp-app___card_clicked`.
4. `ui/update-model-context` in the host, carried into the next run by an `onSend` step
   and added to the prompt by the agent (E6).
5. In-place updates (E4), only if wanted: a host rule outside the extension that sends a
   result to the mounted frame with the same resource uri and card id.
6. URL-loaded apps (E5): wait for the extension to define them. They also need apps and
   the sandbox proxy on an origin separate from the chat page.

Still owed from earlier phases and unchanged here: the platform agent reads only the first
`tools/list` page, the sandbox proxy has no origin of its own, and the status line reads
"Searching the knowledge base" for every tool.

## Manual checks for Sam

- [ ] Look at `.deploy/phase-4-E1.png` to `.deploy/phase-4-E6.png`.
- [ ] On `https://chat.dengler.io/p/mcp-app/`, ask "Show me a card titled Hello with the
      body It works" and press "Record a click": the card's status line shows the host's
      `-32601` refusal.
- [ ] Then ask "Change the body of the Hello card to Updated": a second card, "Card hello",
      appears under the new reply, and the first card is unchanged.
- [ ] Ask "Show me a bar chart of the values 3, 1, 4": no chart appears, though the reply
      may say one did.
- [ ] Ask "Ask me for my preferences", fill the form and press "Send to the assistant":
      the form shows the host's refusal.
- [ ] Open `https://chat.dengler.io/projects/mcp-app/app/index.html` directly: "Opened on
      its own: no MCP Apps host around this page".
- [ ] Decide which of the platform changes above to build, starting with the browser MCP
      client and its route (CSP `connect-src` or `/mcp/*`).
- [ ] Decide whether tool text blocks should stop saying "Showed the user ..." (the model
      repeats the claim when nothing rendered).
- [ ] Review the phase 4 section of `docs/decision-log.md`.
