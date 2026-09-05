// Session persistence: keeps the refresh token in IndexedDB so a page reload restores the
// signed-in session with a silent refresh instead of a redirect through Cognito. Never
// stores the access token; the access and id tokens live in page memory only, as before.
//
// Uses its own database, separate from web/src/history.js's "guppigpt-history", so
// clearing chat history never touches the session and clearing the session never touches
// chat history.
//
// Schema: one object store "session", a single record keyed by a fixed id:
//   { id, refreshToken, claims: { email, name, given_name, family_name, sub }, savedAt }

import { openDB } from "idb";

const DB_NAME = "guppigpt-session";
const DB_VERSION = 1;
const STORE = "session";
const RECORD_ID = "current";

let dbPromise = null;

function openDb() {
  if (!dbPromise) {
    if (!("indexedDB" in globalThis)) {
      dbPromise = Promise.reject(new Error("indexedDB is not available"));
    } else {
      dbPromise = openDB(DB_NAME, DB_VERSION, {
        upgrade(db) {
          if (!db.objectStoreNames.contains(STORE)) {
            db.createObjectStore(STORE, { keyPath: "id" });
          }
        },
      });
    }
  }
  return dbPromise;
}

// Pure: shapes the record persisted to IndexedDB from a refresh token and the decoded id
// token claims. Only the header-relevant claims are kept; every other field on the claims
// object (and the access token, which this function has no parameter for at all) is
// dropped.
export function buildSessionRecord(refreshToken, claims, savedAt = Date.now()) {
  const { email, name, given_name, family_name, sub } = claims || {};
  return {
    id: RECORD_ID,
    refreshToken,
    claims: { email, name, given_name, family_name, sub },
    savedAt,
  };
}

// Persists the refresh token and the id token claims the header needs. Replaces any
// previously saved session.
export async function saveSession(refreshToken, claims) {
  const db = await openDb();
  await db.put(STORE, buildSessionRecord(refreshToken, claims));
}

// The stored session record, or null when none is saved (or storage is unavailable).
export async function loadSession() {
  try {
    const db = await openDb();
    return (await db.get(STORE, RECORD_ID)) || null;
  } catch {
    return null;
  }
}

// Removes the stored session, if any.
export async function clearSession() {
  try {
    const db = await openDb();
    await db.delete(STORE, RECORD_ID);
  } catch {
    // Nothing to clear if storage never opened.
  }
}

// The OAuth error codes the token endpoint returns for a refresh token that is no longer
// good: expired, already rotated out, or revoked at the provider. Any of these means the
// session is over and the stored token must go with it.
const GRANT_INVALID_ERRORS = new Set(["invalid_grant", "expired", "revoked"]);

// Pure: classifies a failed refresh attempt as an OAuth error (the refresh token itself is
// no longer good) or a network error (the request never got a proper answer, so the token
// might still be good). `networkError` is true when the fetch itself rejected; `status` and
// `body` describe an HTTP response that came back but was not ok.
export function classifyRefreshFailure({ networkError, status, body } = {}) {
  if (networkError) return "network-error";
  const errorCode = (body && typeof body.error === "string" ? body.error : "").toLowerCase();
  if (status >= 400 && status < 500 && GRANT_INVALID_ERRORS.has(errorCode)) {
    return "oauth-error";
  }
  return "network-error";
}

// Pure: the single decision for what the page does on load, once whether the URL carries an
// OAuth code has already been checked (that case is handled before this ever runs, since it
// wins regardless of any stored session). `storedSession` is the record from loadSession(),
// or null. `refreshResult` only matters when storedSession is not null: `{ ok: true }` after
// a successful silent refresh, or `{ ok: false, kind: "oauth-error" | "network-error" }`.
//
// Returns one of:
//   "show-sign-in"          no stored session; render the sign-in screen as today
//   "show-chat"             the silent refresh worked; render the chat, no redirect
//   "clear-and-show-sign-in" the refresh token is no longer good; drop it and sign in again
//   "keep-and-show-sign-in"  the refresh attempt could not complete; keep the token and let
//                            the person retry (their next successful load may succeed)
export function decideOnLoad(storedSession, refreshResult) {
  if (!storedSession) return "show-sign-in";
  if (refreshResult && refreshResult.ok) return "show-chat";
  if (refreshResult && refreshResult.kind === "oauth-error") return "clear-and-show-sign-in";
  return "keep-and-show-sign-in";
}
