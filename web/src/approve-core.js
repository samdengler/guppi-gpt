// Pure functions behind the invite approval page (web/src/approve.html,
// docs/proposals/invites.md): reading the link from Sam's email, the two API calls'
// URLs and bodies, the request's rows, and what each answer means. No DOM here, so this
// file runs under node:test (web/test/approve.test.mjs).

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/** The email and token from the page's query string, or null when either is missing or
 * the token is not the UUID the invite API issued. */
export function readLink(search) {
  const params = new URLSearchParams(search);
  const email = (params.get("email") || "").trim().toLowerCase();
  const token = (params.get("token") || "").trim();
  if (!email || !UUID.test(token)) return null;
  return { email, token };
}

/** The lookup call for the request the link names. */
export function lookupUrl(link) {
  const query = new URLSearchParams({ email: link.email, token: link.token });
  return `/api/invite/request?${query}`;
}

/** The approve call's JSON body. */
export function approveBody(link) {
  return JSON.stringify({ email: link.email, token: link.token });
}

function defaultTime(ms) {
  return new Date(ms).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** Label and value pairs for the request, in display order. */
export function requestRows(item, formatTime = defaultTime) {
  return [
    ["Name", item.name],
    ["Email", item.email],
    ["Note", item.note ? item.note : "None"],
    ["Requested", formatTime(item.requestedAt)],
  ];
}

/** What to say instead of the Approve button when the request is no longer pending. */
export function decidedText(status) {
  if (status === "approved") return "Already approved.";
  if (status === "revoked") return "Revoked. Approve again with scripts/invite.sh if needed.";
  return null;
}

/** The page's message after the approve call answers with `status`. */
export function outcome(status, email) {
  if (status === 200) {
    return { ok: true, text: `Approved. Add ${email} to Okta's chat-users group so they can sign in.` };
  }
  if (status === 409) {
    return { ok: false, text: "This link can't approve: the request was already decided, or the link is wrong." };
  }
  return { ok: false, text: "The approval didn't go through. Try again, or use scripts/invite.sh." };
}
