import { test } from "node:test";
import assert from "node:assert/strict";

import { createPendingIndicator, pendingShown, PENDING_LABEL, setPending } from "../src/pending.js";

// Elements as plain objects: enough of the DOM for the indicator and the reply it marks.
function fakeElement(tag) {
  return {
    tag,
    className: "",
    hidden: false,
    textContent: "",
    attributes: {},
    children: [],
    appendChild(child) {
      this.children.push(child);
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
    removeAttribute(name) {
      delete this.attributes[name];
    },
  };
}
const doc = { createElement: fakeElement };

test("the indicator starts hidden with three silent dots and one word to read", () => {
  const indicator = createPendingIndicator(doc);
  assert.equal(indicator.className, "reply-pending");
  assert.equal(indicator.hidden, true);
  const dots = indicator.children.filter((child) => child.className === "reply-pending-dot");
  assert.equal(dots.length, 3);
  for (const dot of dots) assert.equal(dot.attributes["aria-hidden"], "true");
  const [label] = indicator.children.filter((child) => child.className === "visually-hidden");
  assert.equal(label.textContent, PENDING_LABEL);
  assert.equal(PENDING_LABEL, "Working");
});

test("it shows only while the run is active and the reply has no text", () => {
  assert.equal(pendingShown({ running: true, text: "" }), true);
  assert.equal(pendingShown({ running: true, text: undefined }), true);
  assert.equal(pendingShown({ running: true, text: "\n\n" }), true);
  assert.equal(pendingShown({ running: true, text: "Hello" }), false);
  // A finished, failed or aborted run hides it, text or not.
  assert.equal(pendingShown({ running: false, text: "" }), false);
  assert.equal(pendingShown({ running: false, text: "Hello" }), false);
});

test("the reply is aria-busy exactly while the indicator shows", () => {
  const reply = fakeElement("div");
  const indicator = createPendingIndicator(doc);
  setPending(reply, indicator, true);
  assert.equal(indicator.hidden, false);
  assert.equal(reply.attributes["aria-busy"], "true");
  setPending(reply, indicator, false);
  assert.equal(indicator.hidden, true);
  assert.equal("aria-busy" in reply.attributes, false);
});

test("a run's states in order: send, first words, a tool call, more words, the end", () => {
  const reply = fakeElement("div");
  const indicator = createPendingIndicator(doc);
  const step = (running, text) => {
    setPending(reply, indicator, pendingShown({ running, text }));
    return indicator.hidden;
  };
  assert.equal(step(true, ""), false); // sent, nothing yet
  assert.equal(step(true, "Let me check"), true); // first words
  assert.equal(step(true, ""), false); // a tool call cleared the narration
  assert.equal(step(true, "Your balance is"), true);
  assert.equal(step(false, "Your balance is 12 days."), true); // finished
  assert.equal(step(true, ""), false); // Retry starts a new run on the same reply
  assert.equal(step(false, ""), true); // which failed: the error line shows instead
});
