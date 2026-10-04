# Feature flags for dark-shipped features

Three features are landing on other branches: a chat history panel, a thumbs up or
down feedback control on each reply, and Dynatrace RUM on the page. Each needs to sit
in `main` before it is ready for every visitor, so each needs a way to ship in the
bundle but stay off by default, with a way to turn one on without a code change or a
redeploy of the stack. This is that layer: a committed defaults file, a merge step in
`scripts/deploy.sh`, and a page provider built on the OpenFeature web SDK.

Observability that reads what already reaches the browser is never behind a flag:
tracing, the per-run log record, and the data attributes the reply element carries stay
on regardless of what `web/features.json` says. RUM is the exception, because turning
it on inserts a script element and starts a vendor library running on every visitor's
page, not just reads state that already exists; it stays behind a flag the same way
`history` and `feedback` do, and it depends on tenant details Sam supplies later
(`docs/proposals/dynatrace.md`).

## The defaults file

`web/features.json`, committed, is the source of truth for flag defaults:

```json
{
  "history": false,
  "feedback": false,
  "rum": false
}
```

| Key | Feature |
| --- | --- |
| `history` | The chat history panel |
| `feedback` | The thumbs up/down control on a reply |
| `rum` | Dynatrace RUM on the page (`docs/proposals/dynatrace.md`) |
| `debug` | Timings under each reply, the page's and the agent's (`guppi.timing`; `docs/proposals/platform.md`, "Debug mode") |
| `connect-bridge` | Rollback for the Connect chat transport: a project page that uses it goes back to the bridge's warm start and `HttpAgent` (`docs/proposals/platform.md`, "Connect chat transport"). Off, so the transport is the default; `?ff=connect-bridge` turns the old path on for a browser |

Plain JSON, no comments; this document is where the keys are explained. A new flagged
feature adds a key here with a `false` default.

## Flipping a flag

Edit `web/features.json` and run:

```sh
scripts/deploy.sh --site-only
```

`scripts/deploy.sh` already writes `web/dist/config.json` from the stack outputs with
`jq`; it now merges `web/features.json` into that file as a `features` object, keeping
the existing keys (`region`, `userPoolClientId`, `authDomain`, `siteUrl`). No `cdk
deploy`, no image build, no CloudFormation parameter: `--site-only` rebuilds the page
and syncs it to the site bucket, and the new `config.json` is live after the CloudFront
invalidation the script already runs.

## Why static config plus OpenFeature, not a flag service

The flags here are two booleans with one reader (this page) and no need to change
without a deploy: no per-user targeting, no gradual rollout percentage, no audience
rules. A file merged into `config.json` at deploy time covers that with nothing new to
run and nothing new to authenticate to.

AWS AppConfig was the natural first candidate, since the repository's stated preference
is AWS native services. It was set aside: its data plane, `GetLatestConfiguration`, is a
SigV4 signed API call, and the browser has no credentials to sign it with. Cognito
issues OIDC tokens for the user pool, not AWS credentials; adding Identity Pool
federation just to read two booleans would be more stack, more IAM, and another round
trip on every page load for a value that already sits in `config.json`.

The OpenFeature web SDK is the part that is not strictly required for two booleans read
from one static file: `config.features.history` would do. It is here because the
backlog already names the next step (a Dynatrace OpenFeature hook, see below), and
OpenFeature is the vendor-neutral interface that hook expects. Writing the page against
`OpenFeature.getClient().getBooleanValue()` now means the provider underneath can change
later (a remote flag service, a hook that reports evaluations to Dynatrace) without
touching `app.js` again.

## The provider

`web/src/features.js` implements the SDK's `Provider` interface as a static, in-memory
provider: `resolveBooleanEvaluation` and friends read from a plain object computed once,
never a network call. This matches the web SDK's evaluation model, which is
synchronous by design (`getBooleanValue` returns a value, not a promise); a provider
that needed a round trip per evaluation would not fit it.

`initFeatures(config)` builds that object from `config.features` overlaid with the
tab's overrides (below), registers the provider with `OpenFeature.setProviderAndWait`,
and returns the merged flags. `app.js` calls it right after `config.json` loads and
before the first render, then sets `data-features` on `<body>` to the space separated
list of flag names that resolved true. That attribute exists for support (reading a
session's active flags from the browser inspector, the same pattern `data-run-id` and
`data-trace-id` use on the reply element, see `docs/proposals/traceability.md`) and for
the Dynatrace RUM hook below; nothing renders flag state as visible text.

`isEnabled(name)` wraps `OpenFeature.getClient().getBooleanValue(name, false)` for the
two features to call once they land.

The override parsing and the default and override merge are pure functions in
`web/src/flags-core.js`, apart from the SDK import, so they run under `node:test`
without a browser or a bundler (`web/test/features.test.mjs`).

## Browser-wide overrides

A `ff` query parameter overrides the defaults for every tab of the browser, read once
on load:

| Value | Effect |
| --- | --- |
| `?ff=history,feedback` | Turns both flags on |
| `?ff=-history` | Turns `history` off |
| `?ff=` | Clears the browser's override set back to `web/features.json`'s defaults |

A `ff` value, present or empty, replaces the whole stored override set; a name it does
not mention keeps its `config.features` default. Leaving `ff` off the URL entirely
leaves whatever is already stored untouched.

The override set is written to `localStorage` under one key, `guppigpt_ff_overrides`,
and the `ff` parameter is then stripped from the URL with `history.replaceState`, the
same way `finishSignIn` already removes `code` after the OAuth exchange. `localStorage`
was chosen for two reasons. First, Sam wants one override setting that follows the
person, not the tab: opening a link with `?ff=history` should turn history on
everywhere, not just in the tab that opened it. Second, `localStorage` still survives
the sign-in redirect the way the earlier `sessionStorage` choice was chosen for:
sign-in is a full navigation to Cognito and back (`startSignIn` in `app.js`), which
drops any query string the page does not preserve itself, so the override still has to
be stored before that redirect in a form that survives it. A visitor who opens
`?ff=history` before signing in still sees history on after the redirect returns with
only `?code=...` on the URL, and now every other open tab sees it too, once each
reloads.

The provider stays static: flags are computed once, at `initFeatures` time, from
whatever the override set held at that moment. A change written by another tab (a `ff`
link opened there, or a choice made on the flags page below) does not reach an
already-loaded tab; that tab picks it up the next time it reloads, the same way it
picks up a new committed default. `web/src/features.js` listens for the `storage` event
the browser fires in other tabs when the key changes, but only to note the moment for a
session working from the browser console; it does not re-resolve anything on the page.

## The flags page

`web/src/flags.html`, bundled as `web/src/flags.js` into `dist/flags.js`, is a settings
page at `/flags.html`. It is reachable only by typing or bookmarking that URL: nothing
on the chat page or anywhere else links to it. It has no sign-in requirement, since it
reads `config.json`, a public file the chat page already fetches unauthenticated, and
otherwise only reads and writes `localStorage` in the visitor's own browser.

The page lists one row per flag name in `config.features`: the name, the committed
default, this browser's override (none, on, or off), and the value the flag currently
resolves to. A three-way control per row (a `fieldset` of radio inputs, keyboard
operable) writes the override through `setOverride` in `web/src/flags-core.js`, the same
pure helper the page's tests exercise directly. A "Reset all" control clears the stored
key outright. `logging` and `rum` are grouped under their own heading, which says
observability depends on them and they are not meant to be turned off; the control still
allows it, since a tester may need to see the page with one of them off.

The page does not change what a default is. A row's "committed default" column always
reads from `config.features`, which only changes by editing `web/features.json` and
running `scripts/deploy.sh --site-only`; the flags page only ever writes the override
column.

## Attaching Dynatrace later

Backlog item 1 is Dynatrace RUM on the page. OpenFeature's hook interface is where that
would attach: a hook implementing `after` (and `error`) fires on every flag evaluation
with the flag key, the resolved value, and the evaluation reason, and can be registered
globally with `OpenFeature.addHooks(...)` or passed to `setProviderAndWait` alongside
the provider. A Dynatrace hook would report each evaluation as a RUM custom event or a
session property, which is how a Dynatrace session for a page load ties back to which
flags that session saw. None of the current provider or `initFeatures` code would need
to change; the hook is additive. The `data-features` attribute on `<body>` covers the
same need without RUM, for a support session working from the browser inspector alone.

## Bundle size

`@openfeature/web-sdk` 1.10.0 (with its peer, `@openfeature/core` 1.12.0) is bundled by
esbuild the same way `@ag-ui/client` is, with no separate `<script>` tag and no network
fetch at runtime; the CSP (`default-src 'self'`) is unaffected. It added about 26 KB to
the minified `dist/app.js` (roughly 235 KB before, 261 KB after).

## What is not done

- No remote flag service or targeting: flags are read once from a static file, the same
  for every visitor, with the query-parameter or flags-page override for one browser at
  a time.
- AWS AppConfig: set aside above, because its data plane needs SigV4 credentials the
  browser does not have without adding Cognito Identity Pool federation.
- The Dynatrace hook itself: the interface it would use is described above; nothing is
  wired up until Dynatrace RUM lands.
- The two features this layer exists for (chat history, feedback): they ship on their
  own branches and call `isEnabled("history")` / `isEnabled("feedback")` once they
  land. This change touches `app.js` only enough to initialize the provider and set
  `data-features`.
