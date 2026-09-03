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

No library is used. The wrapper in `web/src/history.js` is about 70 lines of `Promise`
wrapping around `indexedDB.open`, `.transaction`, `.put`, `.delete`, `.clear`, and
`.getAll`.

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

## The History control

A "History" text button sits in the header, next to New chat, in the same style as the
existing header controls. It opens a dropdown panel anchored under itself, matching the
existing account menu's pattern rather than adding a permanent sidebar, since the page
design has no room for one and none of the mockups in section 3 show one.

The panel lists every stored thread, newest first: a title, a short date/time, and a delete
control per row. A "Clear all" action sits in the panel header. Clicking a row's title loads
that thread into the visible page (replacing the current one, no confirmation, matching how
New chat and Retry already behave without confirmation dialogs). Clicking a row's delete
control removes only that thread; if it was the open one, the page returns to the empty
state. Clicking outside the panel, or opening History again, closes it, the same way the
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

## Hint and empty-state copy

The product rule changes from "nothing is saved" to "saved on this device only," and both
places that state the rule are updated to say that plainly:

- Composer hint, `web/src/index.html`: was "Enter to send, Shift+Enter for a new line.
  Nothing is saved." Now: "Enter to send, Shift+Enter for a new line. Chats are saved on
  this device only."
- Empty-state copy, `web/src/index.html`: was "Ask anything. This conversation is not
  saved." Now: "Ask anything. Chats are saved on this device only."

Both keep the sentence short enough to read at a glance and both stay accurate: nothing
leaves the browser, and nothing is tied to the signed-in account once sign out runs.

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

## Files changed

- `web/src/history.js`: new. The IndexedDB wrapper (`putThread`, `deleteThread`,
  `clearAll`, `listThreads`, `newestThread`).
- `web/src/app.js`: thread persistence wired into `send`, the successful tail of `runTurn`
  (covers both send and Retry, since Retry calls `runTurn` directly), `resetThread`,
  `signOut`; the History panel's rendering and click handling; `resumeHistory`, called once
  at boot after sign-in completes.
- `web/src/index.html`: the History button and panel markup; the updated hint and
  empty-state copy.
- `web/src/app.css`: styles for the History control and panel, reusing the existing color
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
the only path to one would be adding a fake-IndexedDB package, which the task asked to avoid
unless a dependency-free approach exists. Manual verification instead:

1. Build the page (above), serve `web/dist/` locally, sign in.
2. Send a message, wait for the reply, open the browser's IndexedDB inspector (DevTools →
   Application → IndexedDB → `guppigpt-history` → `threads`) and confirm one record with
   both messages and no token fields anywhere in it.
3. Reload the page and sign in again (tokens are memory-only, so this repeats the redirect
   as it already does today): confirm the same thread reopens with both messages intact.
4. Open History, confirm the thread is listed with a title drawn from the first message.
5. Send a second message in a new chat, confirm a second row appears, newest first.
6. Delete one thread from the panel, confirm it disappears from IndexedDB and, if it was
   the open thread, the page returns to the empty state.
7. Use Clear all, confirm the `threads` store is empty and the panel shows "No saved chats
   yet."
8. Sign out, confirm the `threads` store is empty even before signing back in.
9. With DevTools set to block all storage for the site (or a private window with storage
   disabled), repeat sending a message: confirm the page still works, just without a
   history panel entry, since every store call is wrapped in a try/catch that treats a
   storage failure as "no history available" rather than an error the user sees.
