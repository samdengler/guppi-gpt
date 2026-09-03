# Local chat history (backlog item 5)

This proposal covers browser-local chat history: the thread text a person has typed and
received, kept on the device that typed it, and gone the moment they sign out or clear it.
Tokens are not part of this change. They stay in page memory only, as the design document
requires.

## The AG-UI client and persistence

`@ag-ui/client` was checked (`node_modules/@ag-ui/client/dist/index.d.ts` after `npm ci`,
version 0.0.59, the version pinned in `web/package.json`). `AbstractAgent`, the base class
`HttpAgent` extends, exposes `messages: Message[]` and `threadId: string` as public fields,
plus `setMessages()`, `addMessage()`, and `addMessages()` to update them. That is the whole
surface relevant to history: an in-memory array and a thread identifier the caller supplies.

The package has no storage adapter, no `localStorage` or `IndexedDB` integration, and no
save or load call. `@ag-ui/core`'s `AgentCapabilities` type carries a `persistentState`
boolean, but that is a flag an agent *server* can advertise about its own run state; it is
not a client-side history feature and GuppiGPT's agent does not set it. Persistence is left
entirely to the application, which is what this proposal implements.

The one thing the client does give history for free is the `threadId` constructor option:
`HttpAgent` already takes a `threadId`, so the stored thread's id can be handed straight to
it with no translation layer.

## IndexedDB versus localStorage

IndexedDB is the choice.

`localStorage` is synchronous, string-only, and capped at about 5MB per origin in most
browsers. A chat history is a growing list of messages with newlines and no natural size
limit until the 4,000-character-per-message rule kicks in; even a modest history could sit
close to that cap, and every write would serialize the entire history to JSON and block the
main thread while doing it. IndexedDB stores structured objects directly, its per-origin
quota is a share of disk space (typically hundreds of MB to low GB, browser-dependent) far
past what a personal chat log needs, and every operation is asynchronous, so a write during
a streaming reply does not stall the paint loop.

Privacy is the same for both: same-origin storage, invisible to any other site, cleared by
the same "clear site data" controls, and unaffected by the page's Content Security Policy
(`default-src 'self'`, which governs what the page fetches and executes, not what it stores).
Neither option sends anything anywhere; both are pure client-side state. The distinction is
capacity and write behavior, and IndexedDB wins both.

The wrapper in `web/src/history.js` is built on `idb`, a small `Promise`-based layer over
the raw IndexedDB API, rather than hand-written `Promise` wrapping around
`indexedDB.open`, `.transaction`, `.put`, `.delete`, `.clear`, and `.getAll`. The exported
functions (`putThread`, `deleteThread`, `clearAll`, `listThreads`, `newestThread`) and the
`threads` object store's shape are unchanged from the dependency-free version; only what
sits underneath them moved. See "The idb dependency" below for why and for the size cost.

## What gets stored

One object store, `threads`, keyed by `id`. Each record:

```
{
  id: string,          // equals the AG-UI threadId for that conversation
  title: string,        // the first user message, collapsed and truncated to 60 characters
  createdAt: number,     // epoch ms, set once
  updatedAt: number,      // epoch ms, set on every write
  messages: [{ id, role, content }, ...]
}
```

`role` is `"user"` or `"assistant"`; `content` is plain text, matching the shape already
sent to the agent. Nothing else goes in: no bearer token, no session id, no runtime id, no
account claim. The thread record carries no field that ties it to a signed-in identity,
which is what makes "sign out clears it" a complete answer rather than a partial one.

An empty thread (no messages) is never written. New chat generates a fresh thread id and
switches the page to it, but nothing reaches the store until the first message is sent, so
closing the tab without typing anything leaves no empty record behind.

## The Chats control

A "Chats" text button sits in the header, next to New chat, in the same style as the
existing header controls. It opens a dropdown panel anchored under itself, matching the
existing account menu's pattern rather than adding a permanent sidebar, since the page
design has no room for one and none of the mockups in section 3 show one.

The control is labeled "Chats," not "History": history reads as a record of everything
that ever happened, while what this panel holds is the current set of saved
conversations, so "Chats" says what a person opens it to find. "Sessions" was also
considered and set aside for a plainer reason: "session" already names the AgentCore
Runtime session (the `x-amzn-bedrock-agentcore-runtime-session-id` header) and will name
the Dynatrace RUM session once backlog item 1 lands, and a third meaning in the same
header would confuse a reader of the code or the support playbook before it confuses a
user of the page. The identifiers underneath (`history-wrap`, `history-btn`,
`history.js`, `chatHistory`, the `guppigpt-history` database name) keep the older word;
only the text a person reads changed.

The panel lists every stored thread, newest first: a title, a short date/time, and a delete
control per row. A "Clear all" action sits in the panel header. Clicking a row's title loads
that thread into the visible page (replacing the current one, no confirmation, matching how
New chat and Retry already behave without confirmation dialogs). Clicking a row's delete
control removes only that thread; if it was the open one, the page returns to the empty
state. Clicking outside the panel, or opening Chats again, closes it, the same way the
account menu already closes.

On page load, after sign-in completes, the newest stored thread opens automatically instead
of the composer sitting empty. The AG-UI `threadId` used for the run is the stored thread's
own id, so a reply sent into a resumed thread lands in the same record it came from. The
runtime session id (`sessionId`, the header used for the AgentCore Runtime session) is
untouched by this feature: it is still generated once per page load and only regenerated by
Retry, exactly as the design document specifies.

One gap worth naming: if a run is interrupted mid-reply (a dropped connection, a closed
tab) before the assistant's message is persisted, the resumed thread shows the last user
question with an empty reply area and no Retry link, since Retry state lives in memory and
does not survive a reload. This is a narrow edge case with no data loss, since the
committed exchanges before it are intact; a fix, if wanted, would persist a placeholder
"interrupted" marker per turn, which was left out to keep the storage schema and the UI both
small.

## Shipping dark behind the history flag

This feature lands in `main` behind the `history` flag from `docs/proposals/feature-flags.md`,
default `false` in `web/features.json`. `app.js` reads `isEnabled("history")` once, right
after `initFeatures(config)` resolves, into a `historyEnabled` constant; nothing in this
feature re-checks the flag later in the page's life, matching the flag layer's own model
(a static value read once at load, no live toggling).

Everything this proposal adds to the visible page or to the store sits behind that
constant: the header control stays hidden (`historyWrap.hidden = !historyEnabled`), the
panel's click handlers are never attached, `resumeHistory` and `persistCurrentThread`
return immediately without touching `history.js`, and the composer hint and empty-state
copy keep the sentences the page already showed before this feature existed. The one
exception is sign out: `signOut()` still calls `chatHistory.clearAll()` regardless of the
flag, since clearing an empty (or never-opened) store is a harmless no-op and a future
flip from off to on should not find a stale IndexedDB database from a session before the
flag existed.

With `history` on (`web/features.json` set to `true`, or a tab's own `?ff=history`
override), the page's behavior is everything described above: the Chats control, the
panel, resume on load, and the writes to `web/src/history.js`.

## Hint and empty-state copy

The product rule when the flag is on changes from "nothing is saved" to "saved on this
device only." Both places that state the rule carry both versions, chosen by
`historyEnabled` at load:

- Composer hint, `web/src/index.html`: flag off, unchanged, "Enter to send, Shift+Enter
  for a new line. Nothing is saved." Flag on: "Enter to send, Shift+Enter for a new line.
  Chats are saved on this device only."
- Empty-state copy, `web/src/index.html`: flag off, unchanged, "Ask anything. This
  conversation is not saved." Flag on: "Ask anything. Chats are saved on this device
  only."

The HTML carries the flag-off wording as the default markup, since that is what a visitor
without the flag sees; `app.js` overwrites both paragraphs' `textContent` when
`historyEnabled` is true, before the first render. Both sentences stay short enough to
read at a glance and both stay accurate for the state they describe: nothing leaves the
browser, and nothing is tied to the signed-in account once sign out runs.

## Sign out and stored history

Sign out clears every stored thread. The recommendation, and what is implemented: `signOut()`
calls `history.clearAll()` before redirecting to the Cognito logout endpoint, and the
redirect waits for that call to settle.

The reasoning: thread records carry no account identifier, so there is no way to keep one
person's history separate from another's on the same browser profile. The risk this closes
is a shared or public machine, where the next person to sign in would otherwise see the
previous person's questions and answers sitting in the history panel. Since nothing in this
feature is meant to survive past the current signed-in session in the first place (the
product's whole premise is that a reload starts over unless the person is still signed in
on the same device), clearing on sign out costs nothing a returning user would miss: signing
back in on the same device without an intervening sign out still finds their history,
because sign-in with valid Cognito/Google sessions does not go through `signOut()`.

## The idb dependency

`web/src/history.js` opens the database through `idb`'s `openDB` instead of hand-wrapping
`indexedDB.open` and every request. `idb` is pinned to its exact current version in
`web/package.json` (checked with `npm view idb version`, no caret or tilde range), the
same convention `@ag-ui/client` and `@openfeature/web-sdk` already follow. It added about
3 KB to the minified `dist/app.js`: 258.9 KB before, 261.9 KB after.

The dependency-free version this proposal originally shipped worked and stayed small; the
swap trades roughly 70 lines of raw `IDBRequest` event wiring, and the risk of getting an
edge case in that wiring wrong, for a well-exercised implementation of the same handful of
operations, at a size cost small enough next to the `@ag-ui/client` and
`@openfeature/web-sdk` bundles already in `dist/app.js` to not change the CSP or the
loading story.

## Files changed

- `web/src/history.js`: new. The `idb`-backed store wrapper (`putThread`, `deleteThread`,
  `clearAll`, `listThreads`, `newestThread`).
- `web/package.json`, `web/package-lock.json`: the `idb` dependency, pinned exact.
- `web/src/features.js`, `web/src/flags-core.js`: unchanged, from
  `docs/proposals/feature-flags.md`; this feature is the first to call `isEnabled`.
- `web/src/app.js`: a `historyEnabled` constant read once from `isEnabled("history")`;
  thread persistence wired into `send`, the successful tail of `runTurn` (covers both send
  and Retry, since Retry calls `runTurn` directly), `resetThread`, `signOut` (unconditional);
  the Chats panel's rendering and click handling, gated; `resumeHistory`, called once at
  boot after sign-in completes, gated; the hint and empty-state copy swap, gated.
- `web/src/index.html`: the Chats button and panel markup, `hidden` by default; the
  flag-off hint and empty-state copy as the default markup, with `id`s (`composer-hint`,
  `empty-copy`) for `app.js` to update when the flag is on.
- `web/src/app.css`: styles for the Chats control and panel, reusing the existing color
  tokens and the account menu's visual pattern.

Rendering keeps the existing rule: stored titles and message text reach the DOM only
through `textContent`, never `innerHTML`, so a stored message cannot inject markup any more
than a live one can.

## Testing

`cd web && npm ci && npm run build` completes with no errors or warnings (the one warning
seen mid-change, an accidental shadowing of the global `history` object by this feature's
own module of the same name, was caught by esbuild and fixed by importing it as
`chatHistory` instead). `node --check web/src/app.js` and `node --check web/src/history.js`
both pass.

An automated test for the store wrapper was left out. Node has no built-in `indexedDB`
(checked directly: `node -e "console.log(typeof indexedDB)"` prints `undefined` on Node 24,
the version this repository targets), so a dependency-free Node test is not possible;
`idb` does not change this, since it wraps the browser's IndexedDB API rather than
providing its own. The only path to a Node test would be adding a fake-IndexedDB package
on top of `idb`, left out for the same reason the original wrapper left it out. Manual
verification instead, done with the flag on (`web/features.json`'s `history` set to
`true`, or `?ff=history` on the URL for one tab):

1. Build the page (above), serve `web/dist/` locally, sign in.
2. Send a message, wait for the reply, open the browser's IndexedDB inspector (DevTools →
   Application → IndexedDB → `guppigpt-history` → `threads`) and confirm one record with
   both messages and no token fields anywhere in it.
3. Reload the page and sign in again (tokens are memory-only, so this repeats the redirect
   as it already does today): confirm the same thread reopens with both messages intact.
4. Open Chats, confirm the thread is listed with a title drawn from the first message.
5. Send a second message in a new chat, confirm a second row appears, newest first.
6. Delete one thread from the panel, confirm it disappears from IndexedDB and, if it was
   the open thread, the page returns to the empty state.
7. Use Clear all, confirm the `threads` store is empty and the panel shows "No saved chats
   yet."
8. Sign out, confirm the `threads` store is empty even before signing back in.
9. With DevTools set to block all storage for the site (or a private window with storage
   disabled), repeat sending a message: confirm the page still works, just without a
   Chats panel entry, since every store call is wrapped in a try/catch that treats a
   storage failure as "no history available" rather than an error the user sees.
10. With the flag left at its `web/features.json` default (`false`) and no `?ff=history`
    override, confirm the header shows no Chats control, the hint and empty-state copy
    read as they did before this feature, and no `guppigpt-history` database appears in
    the IndexedDB inspector after sending a message.
