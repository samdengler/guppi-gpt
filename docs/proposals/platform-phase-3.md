# Platform phase 3: the MCP Apps host

The standalone instruction for phase 3 of `docs/proposals/platform.md`, on the `platform`
branch. Read that file, `AGENTS.md`, `docs/proposals/platform-phase-1-report.md` and
`../guppi-mcp-app/docs/phase-2-report.md` first. Phase 2's table decides the delivery
path built here. If the phase 2 report says the `mcpapp` target never reached the
gateway, write `docs/proposals/platform-phase-3-report.md` saying so, still build the
page side against a fixture, and stop before the deploy checks.

The unattended rules from `docs/proposals/platform-phase-1.md` apply unchanged: no
waiting for answers, reversible choices recorded in the decision log, blockers to the
report, no force push, no `main`, no `cdk destroy`, nothing outside the `GuppiGpt` stack,
tokens never in logs, reports, commits or fixtures, and the flag files untouched. If a
report for this phase already exists, continue from its first unfinished step.

Work in this order, one commit per step, each step green before the next.

1. Choose the path. From the phase 2 table: A when the `TOOL_CALL_RESULT` content the
   page receives still holds the embedded resource (or its HTML can be recovered from
   the stringified content); C when the agent can read the resource but the page does
   not receive it; B only when `resources/read` worked through the gateway. Build A or C
   in this phase; B is an experiment in phase 4. Record the choice and the evidence in
   the decision log's next revision.
2. Agent side, path C only. In `agent/src/guppi_agent/agent.py`, after a tool call whose
   result carries the MCP Apps `_meta` resource reference, read that resource through
   the same MCP client and emit an AG-UI `CUSTOM` event named `mcp-app/resource` with
   `{ toolCallId, uri, mimeType, text }`. Path A needs no agent change beyond making sure
   the embedded resource survives into the result the adapter emits; if the adapter
   drops it, emit the same `CUSTOM` event from the tool result instead and say so.
   Tests use a fake tool result; nothing reaches Bedrock or the gateway.
3. Sandbox route. An MCP App's HTML runs in an iframe with `sandbox="allow-scripts"`
   (no `allow-same-origin`, so it has an opaque origin and cannot reach the page's
   IndexedDB or tokens). A `srcdoc` frame inherits the page's CSP, which forbids inline
   scripts, so the frame loads `/sandbox/frame.html` instead: a new page in
   `web/src/sandbox/` served from the site bucket through a CloudFront behavior
   `/sandbox/*` with its own response headers policy (`frame-ancestors 'self'`,
   `script-src 'self' 'unsafe-inline'`, `default-src 'none'`, `img-src data:`,
   `style-src 'unsafe-inline'`, no `connect-src`), and the main page's CSP gains
   `frame-src 'self'`. `frame.html` holds only the inner half of the bridge: it waits for
   one `postMessage` from its parent carrying the HTML, writes it into a nested
   `srcdoc` iframe (sandboxed the same way), and relays JSON-RPC messages between that
   frame and the parent. Add a synth test for the behavior and both policies. Note in the
   decision log that a separate origin for the sandbox is the production answer and this
   path-plus-headers arrangement is the POC one.
4. Host renderer. `web/src/mcp-apps/host.js`, registered by `extensions.js` when the
   manifest's `capabilities` includes `mcp-apps`, on the platform bundle (no `ext.js`).
   It claims tool calls whose result or `CUSTOM` event carries a UI resource, creates
   the iframe in the reply's `reply-attachments` slot at a default height, and runs the
   host half of the bridge per the MCP Apps extension: answer `ui/initialize` with the
   host's capabilities, push the tool result to the app, resize on the app's
   size-changed notification, open links with `noopener`, and refuse `tools/call` from
   the app with a JSON-RPC error in this phase (phase 4 takes it up). Read the message
   names and shapes from the `ext-apps` repository and record the version used. Never
   put model or user text through `innerHTML`; the only HTML written anywhere is the
   app's own document, inside the sandbox. `node:test` coverage for the message
   dispatch with a fake `postMessage`.
5. Build. `web/package.json` bundles `sandbox/frame.js` as its own entry and copies
   `frame.html` into `dist/sandbox/`. `npm test` and `npm run build` pass.
6. Docs. `AGENTS.md` (the sandbox route, the host renderer, the `CUSTOM` event name),
   `docs/proposals/platform.md` (the delivery path chosen and why), the decision log.
7. Deploy. `scripts/deploy.sh --reuse-parameters` (the stack changed) and follow the log
   to `deploy exit=0`.
8. Checks. With curl: `/sandbox/frame.html` returns 200 with the sandbox CSP and
   `frame-ancestors 'self'`; `/` still carries the original CSP plus `frame-src 'self'`.
   With a token from `scripts/test-token.sh`, post the phase 2 AG-UI run again and confirm
   the event that carries the resource arrives. Then a headless browser check: in
   `web/`, add Playwright as a dev dependency and install Chromium
   (`npx playwright install chromium`); `scripts/browser-check.mjs` seeds the page's
   session before load with `page.addInitScript` (open IndexedDB `guppigpt-session`
   version 1, create object store `session` keyed by `id`, put
   `{ id: "current", refreshToken, claims: { email, name, given_name, family_name, sub },
   savedAt }` with the refresh token read from `$HOME/.config/guppi/test-session.json`
   and claims decoded from a fresh id token), opens `https://chat.dengler.io/p/mcp-app/`,
   waits for the chat screen (the silent refresh path), types "Show me a card titled
   Hello with the body It works", waits for an iframe inside `.reply-attachments`, and
   saves `.deploy/phase-3-card.png`. Also load `/` the same way and save
   `.deploy/phase-3-root.png` to show it unchanged. If Playwright cannot be installed
   or the check fails, record exactly what happened and leave the screenshots as Sam's
   morning check.
9. Report. `docs/proposals/platform-phase-3-report.md`: what landed, the path built and
   why, each check with its result, screenshots' paths, decisions, blockers, and the
   manual checks left for Sam. Commit and push.

Done when steps 1 to 9 hold and the report is committed.
