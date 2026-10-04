// Debug mode (README backlog item 11, docs/proposals/platform.md "Debug mode"), behind the
// `debug` flag. A run the page sends carries `forwardedProps.debug: true`, and the reply
// gets a collapsed block under it: the page's own timings from the send, the ids the page
// holds for the run, and, when the agent answers with a CUSTOM `guppi.timing` event, a
// waterfall of the agent's steps. The agent's value is untrusted: numbers are coerced,
// strings are cut, and the step count is capped before anything reaches the DOM. Every
// element is built with createElement and every value set with textContent.

export const DEBUG_FLAG = "debug";
export const TIMING_EVENT_NAME = "guppi.timing";
export const MAX_STEPS = 60;
export const MAX_IDS = 12;
export const MAX_NOTES = 12;
const MAX_NAME = 80;
const MAX_LANE = 40;
const MAX_ID_KEY = 40;
const MAX_ID_VALUE = 200;
const MAX_NOTE = 300;
// An hour in milliseconds; a time past it is clamped so one bad value cannot make every
// other bar a sliver.
const MAX_MS = 3_600_000;

// The page's own marks, in the order a run reaches them, with the label each shows.
export const PAGE_MARKS = [
  ["refreshed", "token refreshed"],
  ["chatReady", "chat ready"],
  ["sent", "request sent"],
  ["started", "run started"],
  ["firstText", "first text"],
  ["finished", "run finished"],
  ["failed", "run failed"],
];

/** The run's forwardedProps with `debug: true` added when the flag is on; else unchanged. */
export function withDebugProp(forwardedProps, enabled) {
  return enabled ? { ...(forwardedProps || {}), debug: true } : forwardedProps;
}

/** True for the agent's timing event. */
export function isTimingEvent(event) {
  return Boolean(event) && event.type === "CUSTOM" && event.name === TIMING_EVENT_NAME;
}

/** "195 ms" under a second, "4.13 s" from a second up. */
export function formatMs(ms) {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

// A finite number of milliseconds clamped to 0 to MAX_MS, or null. Only numbers and
// numeric strings count, so null, true and "" never become 0 or 1.
function toMs(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !value.trim()) return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  return Math.min(Math.max(number, 0), MAX_MS);
}

// One line of plain text from a string, number or boolean, whitespace collapsed and cut
// at `max` characters; anything else is empty.
function toText(value, max) {
  if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") return "";
  const line = String(value).replace(/\s+/g, " ").trim();
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);

/**
 * The `guppi.timing` value reduced to what the block shows: at most MAX_STEPS steps with a
 * name, a start, an end (null for a point in time, never before the start) and a lane ("" when
 * absent); the total or null; at most MAX_IDS ids as [key, value] pairs; at most MAX_NOTES
 * notes; and how many steps were dropped. Null when the value is not an object.
 */
export function sanitizeTiming(value) {
  if (!isObject(value)) return null;
  const rawSteps = Array.isArray(value.steps) ? value.steps : [];
  const steps = [];
  for (const step of rawSteps.slice(0, MAX_STEPS)) {
    if (!isObject(step)) continue;
    const start = toMs(step.start_ms);
    if (start === null) continue;
    const end = toMs(step.end_ms);
    steps.push({
      name: toText(step.name, MAX_NAME) || "(unnamed)",
      start,
      end: end === null ? null : Math.max(end, start),
      lane: toText(step.lane, MAX_LANE),
    });
  }
  const ids = isObject(value.ids)
    ? Object.entries(value.ids)
        .map(([key, id]) => [toText(key, MAX_ID_KEY), toText(id, MAX_ID_VALUE)])
        .filter(([key, id]) => key && id)
        .slice(0, MAX_IDS)
    : [];
  const notes = Array.isArray(value.notes)
    ? value.notes.map((note) => toText(note, MAX_NOTE)).filter(Boolean).slice(0, MAX_NOTES)
    : [];
  return {
    steps,
    total: toMs(value.total_ms),
    ids,
    notes,
    dropped: Math.max(rawSteps.length - MAX_STEPS, 0),
  };
}

const percent = (value) => Math.round(value * 100) / 100;

/**
 * Waterfall rows from a sanitized timing value, in start order (ties keep the agent's
 * order). Each bar's left offset and width are percentages of the scale, the larger of
 * the total and the latest end or point; a point has width 0.
 */
export function layoutWaterfall(timing) {
  const steps = timing ? timing.steps : [];
  const latest = steps.reduce((max, step) => Math.max(max, step.end ?? step.start), 0);
  const scale = Math.max(timing?.total ?? 0, latest);
  const rows = [...steps]
    .sort((a, b) => a.start - b.start)
    .map((step) => {
      const point = step.end === null;
      const left = scale > 0 ? Math.min((step.start / scale) * 100, 100) : 0;
      const width = scale > 0 && !point ? Math.min(((step.end - step.start) / scale) * 100, 100 - left) : 0;
      return {
        name: step.name,
        lane: step.lane,
        point,
        startText: formatMs(step.start),
        durationText: point ? "point" : formatMs(step.end - step.start),
        left: percent(left),
        width: percent(width),
      };
    });
  return { scale, rows };
}

/** The collapsed line: "debug · first words 4.13 s · done 4.95 s · 12 steps". */
export function summaryText(marks, timing) {
  const parts = ["debug"];
  if (marks.firstText !== undefined) parts.push(`first words ${formatMs(marks.firstText)}`);
  if (marks.failed !== undefined) parts.push(`failed ${formatMs(marks.failed)}`);
  else if (marks.finished !== undefined) parts.push(`done ${formatMs(marks.finished)}`);
  else if (marks.firstText === undefined) parts.push("waiting");
  if (timing) parts.push(`${timing.steps.length} ${timing.steps.length === 1 ? "step" : "steps"}`);
  return parts.join(" · ");
}

/** The page's lines as [label, value]: each mark reached, then the page's ids. */
export function pageLines(marks, ids) {
  const lines = PAGE_MARKS.filter(([key]) => marks[key] !== undefined).map(([key, label]) => [label, formatMs(marks[key])]);
  if (ids.traceId) lines.push(["trace", ids.traceId]);
  if (ids.requestId) lines.push(["request", ids.requestId]);
  if (ids.runId) lines.push(["run", ids.runId]);
  return lines;
}

// ---- DOM: plain text only ----

function el(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function lineList(doc, lines) {
  const list = el(doc, "div", "reply-debug-lines");
  for (const [label, value] of lines) {
    const line = el(doc, "div", "reply-debug-line");
    line.append(el(doc, "span", "reply-debug-key", label), el(doc, "span", "reply-debug-value", value));
    list.appendChild(line);
  }
  return list;
}

function waterfall(doc, timing) {
  const wrap = el(doc, "div", "reply-debug-waterfall");
  for (const row of layoutWaterfall(timing).rows) {
    const line = el(doc, "div", row.point ? "reply-debug-step point" : "reply-debug-step");
    const name = el(doc, "span", "reply-debug-step-name");
    if (row.lane) name.appendChild(el(doc, "span", "reply-debug-lane", row.lane));
    name.appendChild(doc.createTextNode(row.name));
    const track = el(doc, "span", "reply-debug-track");
    const bar = el(doc, "span", "reply-debug-bar");
    // CSSOM properties, which the page's CSP allows; no style attribute is written.
    bar.style.left = `${row.left}%`;
    bar.style.width = `${row.width}%`;
    track.appendChild(bar);
    line.append(
      name,
      el(doc, "span", "reply-debug-num", row.startText),
      el(doc, "span", "reply-debug-num", row.durationText),
      track,
    );
    wrap.appendChild(line);
  }
  return wrap;
}

/** The block: a details element, collapsed, whose summary is the one line. */
export function createDebugBlock(doc) {
  const block = el(doc, "details", "reply-debug");
  block.append(el(doc, "summary", "reply-debug-summary", "debug"), el(doc, "div", "reply-debug-body"));
  return block;
}

/** Redraws the block's summary and body from the run's marks, ids and timing. */
export function renderDebugBlock(doc, block, { marks, ids, timing }) {
  const [summary, body] = block.children;
  summary.textContent = summaryText(marks, timing);
  const sections = [el(doc, "p", "reply-debug-heading", "page"), lineList(doc, pageLines(marks, ids))];
  if (timing) {
    const heading = ["agent"];
    if (timing.total !== null) heading.push(`total ${formatMs(timing.total)}`);
    if (timing.dropped) heading.push(`${timing.dropped} more steps not shown`);
    sections.push(el(doc, "p", "reply-debug-heading", heading.join(" · ")), waterfall(doc, timing));
    if (timing.ids.length) sections.push(lineList(doc, timing.ids));
    for (const note of timing.notes) sections.push(el(doc, "p", "reply-debug-note", note));
  }
  body.replaceChildren(...sections);
}

/**
 * One run's debug state: the block, marks in milliseconds from creation (the send), the
 * page's ids, and the agent's timing. A mark keeps its first time, so a second event of
 * the same kind does not move it.
 */
export function createDebugRun(doc, now = () => performance.now()) {
  const sentAt = now();
  const marks = {};
  const ids = {};
  let timing = null;
  const element = createDebugBlock(doc);
  const update = () => renderDebugBlock(doc, element, { marks, ids, timing });
  update();
  return {
    element,
    mark(name) {
      if (marks[name] !== undefined) return;
      marks[name] = now() - sentAt;
      update();
    },
    setIds(next) {
      Object.assign(ids, next);
      update();
    },
    timing(value) {
      timing = sanitizeTiming(value);
      update();
    },
  };
}
