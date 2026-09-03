// Up/down feedback on a committed reply. Ships dark behind the `feedback` flag
// (web/features.json, docs/proposals/feature-flags.md). Nothing here reaches the
// network: a vote becomes a DOM attribute, a CustomEvent on `document`, and, when local
// history is on, a field on the stored message.
//
// The CustomEvent is the integration point for a future Dynatrace RUM hook: a listener
// on "guppi:feedback" turns each detail into a RUM custom action or a session property
// without any change to the code below. See docs/proposals/feedback.md for the mapping
// this proposal picked.

import { isEnabled } from "./features.js";
import { setMessageFeedback } from "./history.js";

export const FEEDBACK_EVENT = "guppi:feedback";

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
