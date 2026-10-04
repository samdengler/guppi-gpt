import { test } from "node:test";
import assert from "node:assert/strict";

import {
  createDebugBlock,
  createDebugRun,
  formatMs,
  isTimingEvent,
  layoutWaterfall,
  MAX_STEPS,
  pageLines,
  renderDebugBlock,
  sanitizeTiming,
  summaryText,
  TIMING_EVENT_NAME,
  withDebugProp,
} from "../src/debug.js";

// The sample from the guppi-hr bridge: a contact made ready, the message sent, the
// designer's routing, and the first reply as a point in time.
const SAMPLE = {
  steps: [
    { name: "bridge receives", start_ms: 0, end_ms: null },
    { name: "contact ready", start_ms: 0, end_ms: 1905, lane: "bridge" },
    { name: "SendMessage", start_ms: 1905, end_ms: 2100, lane: "connect" },
    { name: "designer routing", start_ms: 2593, end_ms: 2978, lane: "connect" },
    { name: "first reply", start_ms: 3311, end_ms: null, lane: "bridge" },
  ],
  total_ms: 3324,
  ids: { contact: "c-123", trace: "68f1abc" },
  notes: ["warm contact reused"],
};

// Elements as plain objects: enough of the DOM for the block. `text()` reads an element's
// text the way textContent would.
function fakeElement(tag) {
  return {
    tag,
    className: "",
    children: [],
    style: {},
    _text: "",
    set textContent(value) {
      this._text = String(value);
      this.children = [];
    },
    get textContent() {
      return this._text + this.children.map((child) => child.textContent).join("");
    },
    appendChild(child) {
      this.children.push(child);
    },
    append(...children) {
      this.children.push(...children);
    },
    replaceChildren(...children) {
      this._text = "";
      this.children = children;
    },
  };
}
const doc = {
  createElement: fakeElement,
  createTextNode: (text) => ({ tag: "#text", textContent: String(text), children: [] }),
};
const find = (node, className, found = []) => {
  if (node.className && node.className.split(" ").includes(className)) found.push(node);
  for (const child of node.children) find(child, className, found);
  return found;
};

test("the flag adds debug: true to forwardedProps and leaves them alone when off", () => {
  assert.deepEqual(withDebugProp({ project: "hr" }, true), { project: "hr", debug: true });
  assert.deepEqual(withDebugProp({}, true), { debug: true });
  assert.deepEqual(withDebugProp(undefined, true), { debug: true });
  const props = { project: "hr" };
  assert.equal(withDebugProp(props, false), props);
});

test("only a CUSTOM event named guppi.timing is the timing event", () => {
  assert.equal(TIMING_EVENT_NAME, "guppi.timing");
  assert.equal(isTimingEvent({ type: "CUSTOM", name: "guppi.timing", value: {} }), true);
  assert.equal(isTimingEvent({ type: "CUSTOM", name: "ping" }), false);
  assert.equal(isTimingEvent({ type: "STEP_STARTED", name: "guppi.timing" }), false);
  assert.equal(isTimingEvent(null), false);
});

test("times read in milliseconds under a second and seconds from a second up", () => {
  assert.equal(formatMs(0), "0 ms");
  assert.equal(formatMs(195.4), "195 ms");
  assert.equal(formatMs(1905), "1.91 s");
  assert.equal(formatMs(4130), "4.13 s");
});

test("the sample timing lays out in start order on the larger of total and latest end", () => {
  const { scale, rows } = layoutWaterfall(sanitizeTiming(SAMPLE));
  assert.equal(scale, 3324);
  assert.deepEqual(
    rows.map(({ lane, name, startText, durationText, left, width, point }) => [lane, name, startText, durationText, left, width, point]),
    [
      ["", "bridge receives", "0 ms", "point", 0, 0, true],
      ["bridge", "contact ready", "0 ms", "1.91 s", 0, 57.31, false],
      ["connect", "SendMessage", "1.91 s", "195 ms", 57.31, 5.87, false],
      ["connect", "designer routing", "2.59 s", "385 ms", 78.01, 11.58, false],
      ["bridge", "first reply", "3.31 s", "point", 99.61, 0, true],
    ],
  );
});

test("without a total the latest end or point sets the scale", () => {
  const timing = sanitizeTiming({ steps: [{ name: "a", start_ms: 500, end_ms: 1000 }, { name: "b", start_ms: 2000 }] });
  const { scale, rows } = layoutWaterfall(timing);
  assert.equal(scale, 2000);
  assert.deepEqual(rows.map((row) => [row.left, row.width]), [[25, 25], [100, 0]]);
  // Nothing at all lays out to nothing, without dividing by zero.
  assert.deepEqual(layoutWaterfall(sanitizeTiming({ steps: [{ name: "x", start_ms: 0, end_ms: 0 }] })).rows[0].width, 0);
  assert.deepEqual(layoutWaterfall(null).rows, []);
});

test("sanitizing coerces numbers, drops unusable steps and keeps ends at or after starts", () => {
  const timing = sanitizeTiming({
    steps: [
      { name: "numeric strings", start_ms: "10", end_ms: "20" },
      { name: "no start", end_ms: 5 },
      { name: "null start", start_ms: null },
      { name: "true start", start_ms: true },
      { name: "end before start", start_ms: 50, end_ms: 40 },
      { name: "garbage end is a point", start_ms: 60, end_ms: "soon" },
      { name: "negative start", start_ms: -5, end_ms: 3 },
      { name: "huge", start_ms: 1e12 },
      "not an object",
      { start_ms: 1, lane: { nested: true } },
    ],
    total_ms: "nope",
  });
  assert.deepEqual(timing.steps, [
    { name: "numeric strings", start: 10, end: 20, lane: "" },
    { name: "end before start", start: 50, end: 50, lane: "" },
    { name: "garbage end is a point", start: 60, end: null, lane: "" },
    { name: "negative start", start: 0, end: 3, lane: "" },
    { name: "huge", start: 3_600_000, end: null, lane: "" },
    { name: "(unnamed)", start: 1, end: null, lane: "" },
  ]);
  assert.equal(timing.total, null);
});

test("sanitizing caps steps, ids, notes and string lengths and flattens whitespace", () => {
  const steps = Array.from({ length: MAX_STEPS + 15 }, (_, i) => ({ name: `s${i}`, start_ms: i }));
  const ids = Object.fromEntries(Array.from({ length: 20 }, (_, i) => [`k${i}`, `v${i}`]));
  const timing = sanitizeTiming({
    steps: [{ name: `${"x".repeat(500)}\n<b>`, start_ms: 0 }, ...steps],
    ids: { ...ids, object: { a: 1 }, empty: "" },
    notes: ["one\n\ttwo", "", { not: "text" }, ...Array.from({ length: 20 }, (_, i) => `n${i}`)],
  });
  assert.equal(timing.steps.length, MAX_STEPS);
  assert.equal(timing.dropped, 16);
  assert.equal(timing.steps[0].name.length, 80);
  assert.ok(timing.steps[0].name.endsWith("…"));
  assert.equal(timing.ids.length, 12);
  assert.deepEqual(timing.ids[0], ["k0", "v0"]);
  assert.equal(timing.notes.length, 12);
  assert.equal(timing.notes[0], "one two");
});

test("a value that is not an object is no timing at all", () => {
  for (const value of [null, undefined, "steps", 42, [], true]) assert.equal(sanitizeTiming(value), null);
  assert.deepEqual(sanitizeTiming({}), { steps: [], total: null, ids: [], notes: [], dropped: 0 });
});

test("the summary line follows the run", () => {
  assert.equal(summaryText({}, null), "debug · waiting");
  assert.equal(summaryText({ sent: 220 }, null), "debug · waiting");
  assert.equal(summaryText({ firstText: 4130 }, null), "debug · first words 4.13 s");
  assert.equal(
    summaryText({ firstText: 4130, finished: 4950 }, sanitizeTiming({ steps: Array.from({ length: 12 }, () => ({ name: "s", start_ms: 0 })) })),
    "debug · first words 4.13 s · done 4.95 s · 12 steps",
  );
  assert.equal(summaryText({ failed: 30500 }, sanitizeTiming({ steps: [{ name: "s", start_ms: 0 }] })), "debug · failed 30.50 s · 1 step");
  // A failure after RUN_FINISHED (a transport error) reads as the failure.
  assert.equal(summaryText({ finished: 900, failed: 950 }, null), "debug · failed 950 ms");
});

test("the page lines list the marks reached in run order, then the page's ids", () => {
  assert.deepEqual(
    pageLines({ firstText: 4130, sent: 220, started: 1100, finished: 4950 }, { traceId: "t1", runId: "r1" }),
    [
      ["request sent", "220 ms"],
      ["run started", "1.10 s"],
      ["first text", "4.13 s"],
      ["run finished", "4.95 s"],
      ["trace", "t1"],
      ["run", "r1"],
    ],
  );
  assert.deepEqual(pageLines({ refreshed: 210, sent: 215 }, { requestId: "q1" })[0], ["token refreshed", "210 ms"]);
});

test("the block is a collapsed details element whose summary is the one line", () => {
  const block = createDebugBlock(doc);
  assert.equal(block.tag, "details");
  assert.equal(block.className, "reply-debug");
  assert.equal(block.open, undefined);
  const [summary, body] = block.children;
  assert.equal(summary.tag, "summary");
  assert.equal(summary.textContent, "debug");
  assert.equal(body.className, "reply-debug-body");
});

test("rendering the sample draws the page lines, one waterfall row per step, the ids and the notes as text", () => {
  const block = createDebugBlock(doc);
  const timing = sanitizeTiming({ ...SAMPLE, notes: ["<img src=x onerror=alert(1)>"] });
  renderDebugBlock(doc, block, { marks: { sent: 220, started: 1100, firstText: 4130, finished: 4950 }, ids: { traceId: "t1" }, timing });
  assert.equal(block.children[0].textContent, "debug · first words 4.13 s · done 4.95 s · 5 steps");
  const headings = find(block, "reply-debug-heading").map((node) => node.textContent);
  assert.deepEqual(headings, ["page", "agent · total 3.32 s"]);
  const rows = find(block, "reply-debug-step");
  assert.equal(rows.length, 5);
  assert.equal(rows[1].textContent, "bridgecontact ready0 ms1.91 s");
  assert.equal(rows[0].className, "reply-debug-step point");
  const bars = find(block, "reply-debug-bar");
  assert.deepEqual(bars[2].style, { left: "57.31%", width: "5.87%" });
  const keys = find(block, "reply-debug-key").map((node) => node.textContent);
  assert.deepEqual(keys, ["request sent", "run started", "first text", "run finished", "trace", "contact", "trace"]);
  // A note that looks like markup stays a string of text.
  assert.deepEqual(find(block, "reply-debug-note").map((node) => node.textContent), ["<img src=x onerror=alert(1)>"]);
});

test("a run's marks are times from the send and keep their first value", () => {
  let clock = 1000;
  const run = createDebugRun(doc, () => clock);
  assert.equal(run.element.children[0].textContent, "debug · waiting");
  clock = 1220;
  run.mark("sent");
  clock = 5130;
  run.mark("firstText");
  clock = 5400;
  run.mark("firstText");
  clock = 5950;
  run.mark("finished");
  run.setIds({ traceId: "t1", runId: "r1" });
  run.timing(SAMPLE);
  assert.equal(run.element.children[0].textContent, "debug · first words 4.13 s · done 4.95 s · 5 steps");
  const values = find(run.element, "reply-debug-value").map((node) => node.textContent);
  assert.deepEqual(values.slice(0, 5), ["220 ms", "4.13 s", "4.95 s", "t1", "r1"]);
});
