// Up/down feedback on a committed reply. Ships dark behind the `feedback` flag
// (web/features.json, docs/proposals/feature-flags.md). A vote becomes a DOM attribute,
// a CustomEvent on `document`, and, when local history is on, a field on the stored
// message.
//
// The CustomEvent is the integration point. One subscriber is in this file:
// initFeedbackSink posts the vote to /api/feedback, a REST API that puts it on an
// EventBridge bus and from there into Dynatrace as a business event
// (docs/proposals/feedback.md). The vote does not travel with the chat request and it is
// not attached to the turn's trace.

import { isEnabled } from "./features.js";
import { setMessageFeedback } from "./history.js";

export const FEEDBACK_EVENT = "guppi:feedback";
export const FEEDBACK_ENDPOINT = "/api/feedback";
// A vote is worth nothing if it costs the page anything: one request, no retry, and a
// short deadline after which the attempt is dropped.
const FEEDBACK_TIMEOUT_MS = 3000;

/**
 * Toggles a vote: clicking the already-active choice withdraws it (null); clicking the
 * other choice replaces it. Pure, no DOM, so this is what web/test/feedback.test.mjs
 * exercises directly.
 */
export function nextVote(current, clicked) {
  return current === clicked ? null : clicked;
}

/**
 * The CustomEvent detail contract for "guppi:feedback". Pure: the same arguments
 * always produce the same plain object, which is what the test file checks in place of
 * a live DOM event.
 */
export function buildFeedbackDetail({ threadId, runId, traceId, requestId, messageId, vote }) {
  return {
    threadId: threadId ?? null,
    runId: runId ?? null,
    traceId: traceId ?? null,
    requestId: requestId ?? null,
    messageId: messageId ?? null,
    vote: vote ?? null,
  };
}

/**
 * The request body for POST /api/feedback, built from one "guppi:feedback" detail. Pure,
 * so this is what web/test/feedback.test.mjs checks. A withdrawn vote travels as "none"
 * rather than as an absent field, so a withdrawal is a record of its own. The three
 * optional identifiers are left out when they are absent: the API's request model
 * accepts no null and no unknown property, so an empty field would be a 400 rather than
 * a vote.
 */
export function buildFeedbackRequestBody(detail) {
  const body = {
    vote: detail?.vote ?? "none",
    runId: String(detail?.runId ?? ""),
    threadId: String(detail?.threadId ?? ""),
  };
  for (const field of ["traceId", "requestId", "messageId"]) {
    const value = detail?.[field];
    if (value) body[field] = String(value);
  }
  return body;
}

/**
 * Posts one vote to the feedback API with the same bearer the chat request carries, and
 * forgets about it: no retry, no reading of the response, nothing logged, and an abort
 * after FEEDBACK_TIMEOUT_MS. A vote cast without a token in hand is dropped rather than
 * queued. getToken is a function so the current access token is read at the moment of
 * the vote rather than at the moment the sink was registered.
 */
export function sendFeedback(detail, getToken) {
  const token = getToken?.();
  if (!token) return;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FEEDBACK_TIMEOUT_MS);
  fetch(FEEDBACK_ENDPOINT, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify(buildFeedbackRequestBody(detail)),
    // Lets the request outlive a page that is closing right after the click.
    keepalive: true,
    signal: controller.signal,
  })
    .catch(() => {
      // A failed vote is not worth a console entry or a second attempt.
    })
    .finally(() => clearTimeout(timer));
}

/**
 * Subscribes the feedback API to the "guppi:feedback" event. Call once, only when the
 * feedback flag is on; every other subscriber attaches the same way.
 */
export function initFeedbackSink(getToken) {
  document.addEventListener(FEEDBACK_EVENT, (event) => sendFeedback(event.detail, getToken));
}

/**
 * Records one vote: stamps (or, for a withdrawn vote, clears) data-feedback on the
 * reply element, dispatches "guppi:feedback" on document with the detail above, and,
 * when the history flag is on and the thread already has a stored record, saves the
 * vote on that message. Nothing is sent over the network.
 */
export function recordFeedback({ replyEl, threadId, runId, traceId, requestId, messageId, vote }) {
  if (replyEl) {
    if (vote) replyEl.dataset.feedback = vote;
    else delete replyEl.dataset.feedback;
  }
  const detail = buildFeedbackDetail({ threadId, runId, traceId, requestId, messageId, vote });
  document.dispatchEvent(new CustomEvent(FEEDBACK_EVENT, { detail }));
  if (isEnabled("history") && threadId) {
    // setMessageFeedback is itself a no-op when the thread has no stored record yet
    // (an unpersisted or never-saved thread), so a vote never creates a partial one.
    setMessageFeedback(threadId, messageId, vote).catch(() => {
      // IndexedDB unavailable; the vote still reached the DOM and the event.
    });
  }
  return detail;
}

/**
 * Builds and wires the up/down control under one committed assistant reply. Call once,
 * after RUN_FINISHED, never for an interrupted reply or the streaming draft. The run
 * id, trace id, and request id are read off the reply element's own data attributes
 * (set by markReply in app.js) instead of being passed in again.
 */
export function renderFeedbackControls(replyEl, { threadId, messageId }) {
  replyEl.querySelector(".feedback-controls")?.remove();

  const wrap = document.createElement("div");
  wrap.className = "feedback-controls";

  const up = document.createElement("button");
  up.type = "button";
  up.className = "feedback-btn feedback-up";
  up.textContent = "▲";
  up.setAttribute("aria-label", "Good reply");
  up.setAttribute("aria-pressed", "false");

  const down = document.createElement("button");
  down.type = "button";
  down.className = "feedback-btn feedback-down";
  down.textContent = "▼";
  down.setAttribute("aria-label", "Bad reply");
  down.setAttribute("aria-pressed", "false");

  const setPressed = (vote) => {
    up.setAttribute("aria-pressed", String(vote === "up"));
    down.setAttribute("aria-pressed", String(vote === "down"));
  };

  const castVote = (clicked) => {
    const current = replyEl.dataset.feedback || null;
    const chosen = nextVote(current, clicked);
    setPressed(chosen);
    recordFeedback({
      replyEl,
      threadId,
      runId: replyEl.dataset.runId,
      traceId: replyEl.dataset.traceId,
      requestId: replyEl.dataset.requestId,
      messageId,
      vote: chosen,
    });
  };

  up.addEventListener("click", () => castVote("up"));
  down.addEventListener("click", () => castVote("down"));

  wrap.append(up, down);
  replyEl.appendChild(wrap);
  return wrap;
}
