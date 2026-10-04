// The sign that a reply is on its way (README backlog item 10). A running reply shows
// three dots under its label from the send until text arrives, and again while a tool
// call has cleared the text, so the gap before the first words is never an empty space.
// The dots are DOM elements styled by app.css (`.reply-pending`); nothing here touches
// reply text. A screen reader gets the hidden word and `aria-busy` on the reply instead.

export const PENDING_LABEL = "Working";

/** Builds the hidden indicator: three dots for the eye and one hidden word to be read. */
export function createPendingIndicator(doc) {
  const indicator = doc.createElement("p");
  indicator.className = "reply-pending";
  indicator.hidden = true;
  for (let i = 0; i < 3; i++) {
    const dot = doc.createElement("span");
    dot.className = "reply-pending-dot";
    dot.setAttribute("aria-hidden", "true");
    indicator.appendChild(dot);
  }
  const label = doc.createElement("span");
  label.className = "visually-hidden";
  label.textContent = PENDING_LABEL;
  indicator.appendChild(label);
  return indicator;
}

/** True while the run is active and the reply shows no text; whitespace is no text. */
export function pendingShown({ running, text }) {
  return Boolean(running) && !String(text ?? "").trim();
}

/** Shows or hides the indicator; the reply is `aria-busy` exactly while it shows. */
export function setPending(reply, indicator, shown) {
  indicator.hidden = !shown;
  if (shown) reply.setAttribute("aria-busy", "true");
  else reply.removeAttribute("aria-busy");
}
