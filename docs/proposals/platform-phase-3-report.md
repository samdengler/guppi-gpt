# Platform phase 3: report

Run of `docs/proposals/platform-phase-3.md` on the `platform` branch, 29 Sep 2026 (commits
00:00 to 00:15 Eastern), unattended. Steps 1 to 9 hold. The stack is deployed from this branch,
`chat.dengler.io/p/mcp-app/` renders the `show_card` MCP App in a sandboxed iframe inside
the reply, and `/` is unchanged. Nothing was blocked. `main` is untouched, nothing was
merged, and nothing outside the `GuppiGpt` stack was changed.

## Steps and commits

| Step | State | Commit |
| --- | --- | --- |
| 1. Choose the path | done: A | `eac0cfe` |
| 2. Agent: the `mcp-app/resource` event | done | `c2ae7a1` |
| 3. Sandbox route and CSP | done | `73962c6` |
| 4. Host renderer | done | `7ab2fb6` |
| 5. Build | done | `48b29f5` |
| 6. Docs | done | `c514f75` |
| 7. Deploy | done, `deploy exit=0` | (no commit) |
| 8. Checks | done, all passed | `7439961` |
| 9. Report | done | this commit |

`uv run -- pytest` (127 tests), `npm test` in `web/` (135 tests), `uv run -- ruff check .`,
`ruff format --check`, `npm run build` and `cdk synth -c image_uri=<ecr uri>` were green
after every step that touched their area.

## Path built, and why

Path A: the UI resource reaches the page through the agent's own AG-UI stream. The phase 2
table (`../guppi-mcp-app/docs/phase-2-report.md`) showed the `TOOL_CALL_RESULT` the page
received for `mcp-app___show_card` held the complete card document as a JSON string, and
that the tools gateway passes the embedded resource, `structuredContent`, result `_meta`,
`resources/list` and `resources/read` through unchanged. C (the agent reading the
resource) was not needed, and B (the browser reading `ui://` through the gateway) stays a
phase 4 experiment, as the brief says.

The adapter does not carry the resource whole, so step 2 took the brief's fallback and
the agent emits the `CUSTOM` event from the tool result. Strands maps an embedded resource
to a bare text item and `ag_ui_strands` keeps only the last text item, so the HTML arrived
by position alone, and its uri, mime type, `structuredContent` and `_meta` were dropped.

## What landed

Agent (`agent/src/guppi_agent/agent.py`). `app_resource()` takes the first embedded text
resource under `ui://` with type `text/html;profile=mcp-app` from a raw MCP
`CallToolResult` (the one named by `_meta.ui.resourceUri` when several qualify).
`app_resource_client()` subclasses Strands' `MCPClient` and overrides its private
`_handle_tool_result` to record that resource per tool use id; `with_app_resources()`
yields a `CUSTOM` event named `mcp-app/resource` right after the matching
`TOOL_CALL_RESULT`. The value is `{ toolCallId, uri, mimeType, text, toolResult }`.
`agent/tests/test_app_resources.py` covers it with fake MCP results, calls the private
hook on the installed Strands client, and runs a whole `StrandsRun` with fake classes.

Stack (`infra/guppi_gpt_infra/stack.py`). A `/sandbox/*` behavior on the site bucket
origin (the same S3 origin object as the default behavior, no function, caching
optimized) with the `SandboxHeadersPolicy` response headers policy: CSP
`default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline';
img-src data:; frame-ancestors 'self'`, HSTS, nosniff, the referrer policy, and no
`X-Frame-Options`. The page's CSP (both Dynatrace branches) gains `frame-src 'self'`.
Behaviors are now `/api/feedback`, `/api/*`, `/sandbox/*`. Synth tests check the behavior,
both policies, and the order.

Sandbox proxy (`web/src/sandbox/`). `frame.html` loads `frame.js`, a classic IIFE script
(an opaque-origin document could not load a module script without CORS). It posts
`ui/notifications/sandbox-proxy-ready` to the page, takes the HTML from one
`ui/notifications/sandbox-resource-ready`, writes it into a nested `srcdoc` iframe with
`sandbox="allow-scripts"`, and relays every other JSON-RPC message both ways; the reserved
`ui/notifications/sandbox-*` methods are never relayed. The logic is `relay.js`, tested
without a DOM in `web/test/sandbox-relay.test.mjs`.

Host renderer (`web/src/mcp-apps/host.js`). Message names and shapes from
modelcontextprotocol/ext-apps 2.0.3 (commit `82221c0`, 25 Sep 2026), spec revision
2026-01-26. `extensions.js` creates it when the manifest's `capabilities` includes
`mcp-apps`; built-ins see an event before a project's renderers. It claims the
`mcp-app/resource` event (and a `TOOL_CALL_RESULT` whose content is a whole
`CallToolResult` with a `ui://` resource), appends an iframe with
`sandbox="allow-scripts"` on `/sandbox/frame.html` to the reply's `reply-attachments`
slot at 160 pixels, and runs the host half of the bridge: the HTML on proxy ready,
`ui/initialize` answered with `hostCapabilities: { openLinks: {} }`, host info and
context, `tool-input` and `tool-result` after `ui/notifications/initialized` only,
`size-changed` clamped to 40 to 640 pixels, `ui/open-link` for http and https with
`noopener,noreferrer`, `ping`, and `tools/call` refused with JSON-RPC `-32601`.
`web/test/mcp-apps-host.test.mjs` drives the dispatch with a fake `postMessage` and a fake
DOM; `web/test/extensions.test.mjs` covers the capability switch and dispatch order.

Build and scripts. `web/package.json` bundles `sandbox/frame.js` into `dist/sandbox/` and
copies `frame.html` beside it; Playwright 1.63.0 is a dev dependency. `scripts/browser-check.mjs`
is the signed-in browser check.

Docs. AGENTS.md (files, the sandbox route and its CSP, the host renderer, the event name,
the browser check), `docs/proposals/platform.md` ("MCP Apps delivery": path A, why, and
the two paths set aside; the changes table; two open questions closed; the manifest
example's `toolPrefix` now `mcp-app___`), and decision log revision 20.

## Decisions taken without Sam

Each is the smaller, reversible choice, and each is in decision log revision 20.

- Path A, with the agent-side `CUSTOM` event, since the adapter drops the resource's
  metadata and keeps its HTML only by position.
- The event value adds `toolResult` (text blocks, `structuredContent`, `_meta`) to the
  four fields the brief names, so the host pushes a real `CallToolResult` as
  `ui/notifications/tool-result`. The card renders from it: "Hello" arrives through the
  bridge, not the card's two second fallback.
- The resource is captured by overriding Strands' private `MCPClient._handle_tool_result`,
  the one method that sees the raw MCP result beside the tool use id. `uv.lock` pins
  Strands, and a test calls the hook on the installed client, so an upgrade that renames it
  fails the suite rather than silently dropping the event.
- The page never sniffs the HTML string in today's `TOOL_CALL_RESULT`; without the event
  (an older agent) no app renders.
- The sandbox proxy is a path on the page's own origin, kept opaque by leaving out
  `allow-same-origin`. The spec asks for a different origin with `allow-same-origin`; the
  decision log records this as the proof of concept arrangement and a separate origin as
  the production answer.
- The proxy uses `srcdoc` for the app, as the brief says, where the ext-apps example host
  uses `document.write` into a same-origin inner frame.
- `hostCapabilities` declares only `openLinks`; `tools/call` and every unimplemented
  request get `-32601`, and an invalid link gets the extension's `-32000`.
- Default frame height 160 pixels, maximum 640, minimum 40.
- The `/sandbox/*` responses carry no `X-Frame-Options`; `frame-ancestors 'self'` is what
  admits the page.

## Checks and results

Local, before the deploy: a small node server applied the exact page and sandbox CSPs to
`web/dist` and a harness page that fed the host the real `show_card` HTML (rendered from
guppi-mcp-app's `card_html`). Headless Chromium showed the chain working: frames
`/`, `/sandbox/frame.html`, `about:srcdoc`; the page received `sandbox-proxy-ready`,
`ui/initialize`, `initialized` and two `size-changed`; the card showed the title from the
pushed `structuredContent`; the frame resized to 108 pixels; no console errors or CSP
violations.

Deploy: `scripts/deploy.sh --reuse-parameters --require-approval never`, log
`.deploy/deploy-20260929-000853.log`, ended `deploy exit=0`. The agent image was rebuilt
and published; `sandbox/frame.html` and `sandbox/frame.js` were uploaded; invalidation
`I85CU5JQSMKYDTJW7DPRIQRZJK` completed before the checks.

curl against `https://chat.dengler.io` (raw output in `.deploy/phase3-baseline.txt` and
`.deploy/phase3-after.txt`, both gitignored):

| Request | Before | After |
| --- | --- | --- |
| `GET /` | 200, page CSP without `frame-src`, `X-Frame-Options: DENY` | 200, the same CSP plus `frame-src 'self'` (Dynatrace beacon origin still in `connect-src`), `X-Frame-Options: DENY` |
| `GET /sandbox/frame.html` | 403 from S3 | 200 `text/html`, CSP `default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'`, no `X-Frame-Options`, HSTS, nosniff |
| `GET /sandbox/frame.js` | (not requested) | 200 `text/javascript`, the sandbox CSP |
| `GET /p/mcp-app/` | 200 | 200, the page CSP with `frame-src 'self'` |
| `GET /projects/mcp-app/manifest.json` | 200, `capabilities: ["mcp-apps"]` | 200, unchanged |

AG-UI run: the phase 2 run posted again (`POST /api/invocations`, token from
`scripts/test-token.sh`, `forwardedProps: {"project": "mcp-app"}`, "Show me a card titled
Hello with the body It works"), trace `b3d32e171b32474c9e4a107622b44efd`. Script and
output: `.deploy/phase3-agui.py`, `.deploy/phase3-agui.txt`. Status 200
`text/event-stream`, 26 events: `RUN_STARTED`, `STATE_SNAPSHOT`,
`TOOL_CALL_START` (`mcp-app___show_card`), 5 `TOOL_CALL_ARGS`, `TOOL_CALL_END`,
`TOOL_CALL_RESULT`, `CUSTOM` `mcp-app/resource`, the text message, `STATE_SNAPSHOT`,
`RUN_FINISHED` (success). The `CUSTOM` event came right after `TOOL_CALL_RESULT`, with the
same tool call id, uri `ui://mcp-app/card`, mime type `text/html;profile=mcp-app`, 2681
characters of HTML identical to the decoded `TOOL_CALL_RESULT` content, and `toolResult`
holding the text block, `structuredContent: {"title": "Hello", "body": "It works"}` and
`_meta.ui.resourceUri`.

Headless browser check: `node scripts/browser-check.mjs`, Playwright 1.63.0 with Chromium
(headless shell 153), run twice (the second run added failed-response logging), both
exit 0. Output in `.deploy/phase3-browser.txt`.

| Check | Result |
| --- | --- |
| `/p/mcp-app/` opens on the chat screen through the silent refresh | yes, brand "MCP App Lab" |
| An iframe inside `.reply-attachments` after the prompt | yes, one, `sandbox="allow-scripts"`, `src="/sandbox/frame.html"` |
| The card inside the nested frame | title "Hello", body "It works" |
| The frame resized by the app's `size-changed` | 160 to 108 pixels |
| Reply text | "Done! I've shown you a card with the title "Hello" and the body "It works"." |
| `/` opens on the chat screen, unchanged | brand, tab title "GuppiGPT", placeholder "Ask GuppiGPT", empty state as before |

Screenshots: `.deploy/phase-3-card.png` (the project page with the card under the reply)
and `.deploy/phase-3-root.png` (the root page). Both are gitignored, on Sam's Mac.

The console on `/p/mcp-app/` showed only Dynatrace RUM errors: the beacon at
`https://bf49265sdi.bf.dynatrace.com/bf` refused by CORS five times and answered 400 once.
They come from the `rum` flag and the tenant's beacon settings, not from this phase; no
page, sandbox or CSP error appeared.

Test session: every refresh token rotation (the script's own refresh for the id token
claims and each page's silent refresh) was written back to
`$HOME/.config/guppi/test-session.json` at mode 600, and `scripts/test-token.sh` still
returned a token afterwards. No token was printed, logged, or committed.

## Not done, and why

- The status line for the card tool reads "Searching the knowledge base" and then
  "Searched the knowledge base", since the page's built-in status text assumes the only
  tool is the search and the host cannot know a call is an app until its result arrives.
  Left for Sam to decide on wording; the screenshot shows it.
- The sandbox has no origin of its own yet (see the decisions). Before anyone else's app
  runs here, the proxy should move to a separate hostname and gain `allow-same-origin`.
- `tools/call`, `resources/read`, `ui/message` and `ui/update-model-context` from an app
  are refused; phase 4 takes up tool calls.
- Still owed from phase 2: the platform agent reads only the first `tools/list` page. The
  DEFAULT listing on the `mcp-app` target keeps the tool on page one today.
- Still owed from phase 1: `docs/guppigpt-design.html` does not describe the platform or
  the MCP Apps host.
- The Claude Doc linked from `platform.md` was not changed.

## Morning checks for Sam

- [ ] Open `https://chat.dengler.io/p/mcp-app/` signed in and ask "Show me a card titled
      Hello with the body It works": a card with "Hello" and "It works" appears under the
      reply, sized to the card, with no scroll bar inside it.
- [ ] Ask for a second card with other words in the same chat: a second frame appears
      under the second reply with those words, and the first card is unchanged.
- [ ] In the developer tools, the card's frame is `/sandbox/frame.html` with
      `sandbox="allow-scripts"`, and the console shows no CSP violation from the page or
      the sandbox.
- [ ] Open `https://chat.dengler.io/` and ask a documentation question: GuppiGPT as
      before, no frame under the reply.
- [ ] Look at `.deploy/phase-3-card.png` and `.deploy/phase-3-root.png`.
- [ ] Decide the status line wording for app tools, and when the sandbox moves to its own
      origin.
- [ ] Review decision log revision 20, the private Strands hook in particular.
