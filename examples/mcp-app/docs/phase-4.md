# Phase 4: MCP Apps experiments

The standalone instruction for phase 4 of the platform design
(`../guppi-gpt/docs/proposals/platform.md`). Read that file, this repository's
`AGENTS.md`, `docs/phase-2-report.md` and
`../guppi-gpt/docs/proposals/platform-phase-3-report.md` first. The unattended rules
from `docs/phase-2.md` apply unchanged. If `docs/phase-4-report.md` already exists,
continue from its first unfinished experiment.

The purpose is a comparison Sam can read in the morning: for each way an MCP App can
reach and talk to the chat page, what it took, whether it works on this platform, and
the evidence. Each experiment is one tool on the phase 2 server (or one new server when
the experiment needs different hosting), one row in `docs/experiments.md`, and, when it
works, one screenshot from the phase 3 browser check with its own prompt. Experiments
never change the platform page or stack; when one needs a platform change, describe
the change in the row and mark the experiment blocked.

Run the experiments in this order, one commit each, and stop at any point where the
remaining ones all depend on something blocked.

1. E1, embedded resource in the tool result. Already built in phases 2 and 3; document
   it as the baseline row: the path used, the message flow, what the app receives, and
   its screenshot.
2. E2, host reads `ui://` by `resources/read`. Only when phase 2 found `resources/read`
   works through the gateway: a tool `show_chart(values: list[float])` whose result
   carries the resource reference and no embedded HTML, so the host must fetch it. This
   needs the `guppi.mcp` client in the page and the `/mcp/*` behavior, which are
   platform changes; if they are absent, write the row as blocked with the exact change
   the platform needs, and what a direct route to the Runtime endpoint would take
   instead. Do not build platform changes in this phase.
3. E3, app calls a tool. The card gains a button; pressing it sends `tools/call` to the
   host for a tool `card_clicked(card_id: str)`. With the phase 3 host refusing
   `tools/call`, the expected result is the JSON-RPC error rendered in the card; record
   that, and describe which relay (through `guppi.mcp` or a new AG-UI turn) would carry
   it. Add the server-side tool and its audit log line so the relay has something to hit.
4. E4, a later tool updates the same app. A tool `update_card(card_id: str, body: str)`
   whose result names the same `ui://mcp-app/card` resource and the same card id, so a
   host following the extension pushes the new tool result into the existing iframe
   rather than creating another. Prompt: ask for a card, then ask to change its body.
   Record whether the phase 3 host does this (it follows the extension's rule for
   subsequent tool results if it implemented one) or renders a second card.
5. E5, an app served as a static page. A resource whose content is a URL rather than
   HTML (the extension's `text/uri-list` form), pointing at
   `https://chat.dengler.io/projects/mcp-app/app/index.html`, a page this repository
   publishes beside its manifest. It loads in the sandbox only if the sandbox frame
   accepts a URL as well as HTML and the sandbox CSP's `frame-src` allows it; record what
   happened either way.
6. E6, a form that returns data to the model. A tool `ask_preferences()` whose app is a
   two-field form; on submit the app sends the values back as the app-to-host message
   the extension defines for that purpose, and the host is expected to add them to the
   next turn. Record what the phase 3 host does with the message.
7. Deploy after the server changes (`scripts/deploy.sh`), rerun the phase 2 probe to
   confirm every tool is listed through the gateway, and run the phase 3 browser check
   once per experiment with its prompt, saving `.deploy/phase-4-E<n>.png`.
8. Report. `docs/experiments.md` is the comparison: one row per experiment with columns
   experiment, path, what the app can do, works here (yes, partly, blocked), evidence,
   platform change needed. `docs/phase-4-report.md`: what landed, what was skipped,
   decisions, blockers, and the manual checks left for Sam. Commit and push.

Done when the comparison table has a row for E1 to E6 and both reports are committed.
