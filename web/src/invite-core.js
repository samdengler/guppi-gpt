// Pure functions behind the sign-in screen's invite request (docs/proposals/invites.md):
// reading Cognito's refusal when the pre sign-up gate turns a Google account away,
// checking and shaping the form, and what each answer from /api/invite means. No DOM
// here, so this file runs under node:test (web/test/invite.test.mjs).

// The pre sign-up trigger raises with this marker; Cognito sends the browser back with it
// in error_description ("PreSignUp failed with error not-invited.").
const NOT_INVITED_MARKER = "not-invited";
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
export const NAME_MAX = 100;
export const EMAIL_MAX = 254;
export const NOTE_MAX = 500;

/** "not-invited" when the gate refused this Google account, "failed" for any other sign-in
 * error Cognito sent back, or null when the URL carries none. */
export function signInRefusal(search) {
  const params = new URLSearchParams(search);
  if (!params.has("error")) return null;
  const description = params.get("error_description") || "";
  return description.includes(NOT_INVITED_MARKER) ? "not-invited" : "failed";
}

/** The JSON body for /api/invite from the form's values, or the first problem with them. */
export function inviteBody({ name = "", email = "", note = "" }) {
  const cleanName = name.trim();
  const cleanEmail = email.trim().toLowerCase();
  const cleanNote = note.trim();
  if (!cleanName) return { ok: false, error: "Add your name." };
  if (cleanName.length > NAME_MAX) return { ok: false, error: `Keep the name under ${NAME_MAX} characters.` };
  if (!EMAIL.test(cleanEmail) || cleanEmail.length > EMAIL_MAX) {
    return { ok: false, error: "Enter the email address of your Google account." };
  }
  if (cleanNote.length > NOTE_MAX) return { ok: false, error: `Keep the note under ${NOTE_MAX} characters.` };
  const body = { name: cleanName, email: cleanEmail };
  if (cleanNote) body.note = cleanNote;
  return { ok: true, body };
}

/** What the page says after /api/invite answers with `status` (0 for a network failure). */
export function inviteResult(status) {
  if (status === 202) return { ok: true };
  if (status === 429) {
    return { ok: false, text: "Too many requests just now. Try again in a few minutes." };
  }
  if (status === 400) return { ok: false, text: "That request didn't go through. Check the fields and try again." };
  return { ok: false, text: "The request didn't go through. Try again in a moment." };
}
