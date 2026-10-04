// The Connect chat transport (guppi-hr D55; docs/proposals/platform.md, "Connect chat
// transport"). A project whose manifest lists the `connect-chat` capability and carries a
// `connectChat` block sends each question straight to Amazon Connect's participant
// service and reads the reply on its own WebSocket through amazon-connect-chatjs. The
// project's chat-start route (same origin) starts the chat and answers with the
// participant credentials; this module holds them in memory only, inside the chat
// session, and never hands them to the extension host, history, the debug block or RUM.
//
// Three layers, so the rules are tested with no network:
// - classify() and createTurnAssembler(): pure functions over raw Connect items. The
//   turn rules come from the manifest as data (marks, hidden prefix, lines, limits).
// - ConnectChat: one chatjs customer session for one contact, with catch-up and one
//   reconnect, built around an injected chatjs object.
// - createConnectChatClient(): the page's chats by thread: the start request and its
//   NDJSON stream, the in-flight start a question waits for, restarts, the bridge
//   fallback, and sign-out.

export const CONNECT_CHAT_CAPABILITY = "connect-chat";
// With this flag on, the page behaves as before D55: the bridge's warm start and HttpAgent.
export const CONNECT_BRIDGE_FLAG = "connect-bridge";
export const STEP_NAME = "Amazon Connect";
export const PING_MS = 15_000;
// A chat with less than this left before expiresAt is replaced before the next question.
export const EXPIRY_MARGIN_MS = 5 * 60 * 1000;
// The start route answers its first line after the token exchanges, StartChatContact and
// the greeting; past this the page stops waiting and the thread uses the bridge.
export const START_TIMEOUT_MS = 25_000;
export const SIGN_OUT_CAP_MS = 2_000;
// CreateParticipantConnection and the socket; past this the thread uses the bridge.
export const CONNECT_TIMEOUT_MS = 15_000;

function withTimeout(promise, ms) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("timed out")), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

export const ENDED_CONTENT_TYPES = [
  "application/vnd.amazonaws.connect.event.chat.ended",
  "application/vnd.amazonaws.connect.event.participant.left",
];
const CHAT_ENDED = ENDED_CONTENT_TYPES[0];

// The errors SendMessage gives for a connection that is no longer the participant's.
const REFUSED_TYPES = ["AccessDeniedException"];

/** Text with every trailing copy of `mark` removed, as Python's rstrip does for one character. */
export function stripMark(text, mark) {
  if (!mark) return text;
  let out = text;
  while (out.endsWith(mark)) out = out.slice(0, -mark.length);
  return out;
}

/**
 * A transcript item as a reply to show, the end of a turn, or null for the customer's own
 * message, a hidden line and every other event (the bridge's classify, turn.py).
 * Kinds: "text" (with `mark` "end" or "closed" when the item carried one), "end" (the
 * legacy end line), "closed" (the legacy closed line), "escalated", "error" and "ended".
 */
export function classify(item, rules) {
  if (!item || typeof item !== "object") return null;
  if (item.Type === "EVENT" && ENDED_CONTENT_TYPES.includes(item.ContentType)) {
    return { kind: "ended", text: rules.lines.ended };
  }
  if (item.Type !== "MESSAGE" || item.ParticipantRole === "CUSTOMER") return null;
  const content = typeof item.Content === "string" ? item.Content : "";
  const trimmed = content.trim();
  if (rules.endLine && trimmed === rules.endLine) return { kind: "end", text: "" };
  if (rules.closedLine && trimmed === rules.closedLine) return { kind: "closed", text: "" };
  if (rules.escalationPrefix && content.startsWith(rules.escalationPrefix)) {
    return { kind: "escalated", text: rules.lines.escalated };
  }
  if (rules.errorPrefix && content.startsWith(rules.errorPrefix)) {
    return { kind: "error", text: rules.lines.error };
  }
  if (rules.hiddenPrefix && content.startsWith(rules.hiddenPrefix)) return null;
  if (rules.endMark && content.endsWith(rules.endMark)) {
    return { kind: "text", text: stripMark(content, rules.endMark), mark: "end" };
  }
  if (rules.closedMark && content.endsWith(rules.closedMark)) {
    return { kind: "text", text: stripMark(content, rules.closedMark), mark: "closed" };
  }
  return { kind: "text", text: content };
}

const timeOf = (item) => {
  const value = Date.parse(item && item.AbsoluteTime);
  return Number.isFinite(value) ? value : null;
};

/**
 * One turn's replies from raw Connect items, with no timers of its own: the caller feeds
 * items with push(), says when SendMessage resolved with sent(), and calls tick() at
 * deadline(). Each call returns the outputs to show, in order: `{ type: "text", text }`
 * or `{ type: "custom", name: "connect/<kind>", text }`. The rules:
 * - items are buffered until sent() gives the message's Id and AbsoluteTime; then items
 *   older than the message, and the message itself, are dropped (a previous turn's late
 *   replies), and the rest are taken in AbsoluteTime order;
 * - an Id is taken once (a catch-up read repeats what the socket delivered);
 * - the end mark or the end line ends the turn; the closed mark, the closed line, an
 *   escalation, the designer's error line, chat.ended and participant.left end it and
 *   close the conversation;
 * - a reply without a mark ends after quietAfterMs of quiet;
 * - no reply within turnLimitMs of the start gives the no-reply line.
 * `now` is a millisecond clock; the turn limit counts from the assembler's creation,
 * which is the send. With `late`, the assembler reads a question's late answer after its
 * turn ended with the no-reply line: there is no turn limit, and only the end of the
 * answer (a mark, a closing event, or quiet after a reply) ends it.
 */
export function createTurnAssembler({ rules, now, previousSentAt = null, late = false }) {
  // Only a reply newer than the previous question counts as that turn's late reply; the
  // greeting and older items a catch-up repeats are not.
  const previousTime = timeOf({ AbsoluteTime: previousSentAt });
  const startedAt = now();
  const seen = new Set();
  const buffer = [];
  let own = null;
  let ownTime = null;
  let replied = false;
  let quietUntil = null;
  let stale = 0;
  let done = null;
  const marks = { firstItemAt: null, lastItemAt: null, firstItemMs: null, endMs: null, endName: null };

  function finish(reason, extra = {}, endName) {
    done = { reason, closed: false, error: null, ...extra };
    marks.endMs = now() - startedAt;
    marks.endName = endName;
  }

  function take(item) {
    if (done) return [];
    if (item.Id === own.Id) return [];
    const reply = classify(item, rules);
    const time = timeOf(item);
    if (ownTime !== null && time !== null && time < ownTime) {
      if (reply && previousTime !== null && time > previousTime) stale += 1;
      return [];
    }
    if (!reply) return [];
    if (reply.kind !== "end" && marks.firstItemMs === null) {
      marks.firstItemMs = now() - startedAt;
      marks.firstItemAt = item.AbsoluteTime || null;
    }
    if (item.AbsoluteTime) marks.lastItemAt = item.AbsoluteTime;
    if (reply.kind === "end") {
      finish("end_mark", {}, "end mark");
      return [];
    }
    if (reply.kind === "text") {
      replied = true;
      const outputs = reply.text ? [{ type: "text", text: reply.text }] : [];
      if (reply.mark === "end") {
        finish("end_mark", {}, "end mark");
      } else if (reply.mark === "closed") {
        outputs.push({ type: "custom", name: "connect/closed", text: "" });
        finish("closed", { closed: true }, "closing event");
      } else {
        quietUntil = now() + rules.quietAfterMs;
      }
      return outputs;
    }
    // A closing event: the custom event first, then its line as text, as the bridge sends
    // them (the page renders no CUSTOM event itself).
    const outputs = [{ type: "custom", name: `connect/${reply.kind}`, text: reply.text || "" }];
    if (reply.text) outputs.push({ type: "text", text: reply.text });
    if (reply.kind === "ended") finish("ended", { closed: true }, "closing event");
    else if (reply.kind === "error") finish("error", { closed: true, error: "designer_error" }, "closing event");
    else finish("closed", { closed: true }, "closing event");
    return outputs;
  }

  return {
    push(item) {
      if (done || !item || typeof item !== "object" || !item.Id) return [];
      if (seen.has(item.Id)) return [];
      seen.add(item.Id);
      if (!own) {
        buffer.push(item);
        return [];
      }
      return take(item);
    },
    sent(result) {
      if (own || done) return [];
      own = { Id: result && result.Id ? result.Id : null, AbsoluteTime: (result && result.AbsoluteTime) || null };
      ownTime = timeOf(own);
      if (own.Id) seen.add(own.Id);
      const ordered = buffer
        .map((item, index) => ({ item, index, time: timeOf(item) }))
        .sort((a, b) => (a.time ?? 0) - (b.time ?? 0) || a.index - b.index)
        .map(({ item }) => item);
      buffer.length = 0;
      const outputs = [];
      for (const item of ordered) outputs.push(...take(item));
      return outputs;
    },
    tick() {
      if (done) return [];
      const at = now();
      if (quietUntil !== null && at >= quietUntil) {
        finish("quiet", {}, "quiet after the reply");
        return [];
      }
      if (!late && at - startedAt >= rules.turnLimitMs) {
        if (replied) {
          finish("quiet", {}, "turn limit");
          return [];
        }
        finish("no_reply", {}, "turn limit");
        return [{ type: "text", text: rules.lines.noReply }];
      }
      return [];
    },
    /** The clock time of the next tick that can change anything, or null when done. */
    deadline() {
      if (done) return null;
      if (late) return quietUntil;
      const limit = startedAt + rules.turnLimitMs;
      return quietUntil === null ? limit : Math.min(limit, quietUntil);
    },
    get done() {
      return done;
    },
    summary() {
      return {
        reason: done ? done.reason : null,
        closed: Boolean(done && done.closed),
        error: done ? done.error : null,
        replied,
        stale,
        sentAt: own ? own.AbsoluteTime : null,
        ...marks,
      };
    },
  };
}

// ---- The start route's stream ----

/**
 * The first line of the start route's answer, checked: `{ ok: true, details, region,
 * expiresAt, startedAt, restarted, timing }` with `details` the chatjs chatDetails, or
 * `{ ok: false, reason }` with reason "signin", "unavailable" or "malformed".
 */
export function startResult(line) {
  if (!line || typeof line !== "object") return { ok: false, reason: "malformed" };
  if (line.error === "signin") return { ok: false, reason: "signin" };
  if (typeof line.error === "string") return { ok: false, reason: "unavailable" };
  const result = line.data && line.data.startChatResult;
  const fields = ["ContactId", "ParticipantId", "ParticipantToken"];
  if (!result || fields.some((key) => typeof result[key] !== "string" || !result[key])) {
    return { ok: false, reason: "malformed" };
  }
  const region = typeof line.region === "string" && /^[a-z]{2}(-[a-z]+)+-\d$/.test(line.region) ? line.region : null;
  if (!region) return { ok: false, reason: "malformed" };
  return {
    ok: true,
    details: {
      contactId: result.ContactId,
      participantId: result.ParticipantId,
      participantToken: result.ParticipantToken,
    },
    region,
    expiresAt: Number.isFinite(line.expiresAt) ? line.expiresAt : null,
    startedAt: Number.isFinite(line.startedAt) ? line.startedAt : null,
    restarted: line.restarted === true,
    timing: line.timing && typeof line.timing === "object" ? line.timing : null,
  };
}

/**
 * Reads an NDJSON body: resolves with the first line parsed (null when the body ends
 * first or the line is not JSON), and gives the second line to `onSecond` when it comes.
 * The reader is released after the second line or the end of the body.
 */
export async function readStartStream(body, onSecond = () => {}) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  const nextLine = async () => {
    for (;;) {
      const at = buffered.indexOf("\n");
      if (at >= 0) {
        const line = buffered.slice(0, at);
        buffered = buffered.slice(at + 1);
        if (line.trim()) return line;
        continue;
      }
      const { value, done } = await reader.read();
      if (done) {
        const rest = buffered;
        buffered = "";
        return rest.trim() ? rest : null;
      }
      buffered += decoder.decode(value, { stream: true });
    }
  };
  const parse = (line) => {
    if (line === null) return null;
    try {
      return JSON.parse(line);
    } catch {
      return null;
    }
  };
  const first = parse(await nextLine());
  // The rest of the stream, off the answer's path: the warm-up count and its timing.
  (async () => {
    try {
      const second = parse(await nextLine());
      if (second) onSecond(second);
      await reader.cancel();
    } catch {
      // The function ended or the page left; the second line is only for the debug block.
    }
  })();
  return first;
}

// ---- The turn report ----

const END_REASONS = ["end_mark", "closed", "ended", "quiet", "no_reply", "error", "aborted"];
const ABSOLUTE_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/;

/**
 * The body of one turn's report: ids, Connect's AbsoluteTime marks, the end reason, an
 * error code and the transport. Never reply text, never a token. Null without a contact
 * id or a known end reason.
 */
export function reportBody({ contactId, runId, threadId, sentAt, firstItemAt, lastItemAt, endReason, error, transport, timing }) {
  if (typeof contactId !== "string" || !contactId || !END_REASONS.includes(endReason)) return null;
  const body = { contactId, runId: String(runId || ""), endReason, transport: transport === "bridge" ? "bridge" : "connect" };
  if (threadId) body.threadId = String(threadId);
  for (const [key, value] of [["sentAt", sentAt], ["firstItemAt", firstItemAt], ["lastItemAt", lastItemAt]]) {
    if (typeof value === "string" && ABSOLUTE_TIME.test(value)) body[key] = value;
  }
  if (typeof error === "string" && /^[a-z_]{1,40}$/.test(error)) body.error = error;
  if (timing && typeof timing === "object") {
    body.timing = {
      steps: Array.isArray(timing.steps) ? timing.steps.slice(0, 40) : [],
      total_ms: timing.total_ms,
      notes: Array.isArray(timing.notes) ? timing.notes.slice(0, 12) : [],
    };
  }
  return body;
}

/**
 * The page's report sender: each report goes out with `fetch(..., { keepalive: true })`.
 * A report that fails to send (a network error; an HTTP answer counts as sent) waits in a
 * memory queue of at most `cap` bodies, oldest dropped first, and goes again before the
 * next report and on flush(), which the page calls when the network comes back. The queue
 * holds report bodies only, never text or a token; the bearer is read at each send.
 */
export const REPORT_QUEUE_CAP = 20;

export function createReportSender({ url, fetch: fetchImpl, getToken, cap = REPORT_QUEUE_CAP }) {
  const queue = [];
  function keep(body) {
    queue.push(body);
    while (queue.length > cap) queue.shift();
  }
  function post(body) {
    let request;
    try {
      request = Promise.resolve(
        fetchImpl(url, {
          method: "POST",
          keepalive: true,
          headers: { authorization: `Bearer ${getToken()}`, "content-type": "application/json" },
          body: JSON.stringify(body),
        }),
      );
    } catch (error) {
      request = Promise.reject(error);
    }
    return request.then(
      () => true,
      () => {
        keep(body);
        return false;
      },
    );
  }
  function flush() {
    return Promise.all(queue.splice(0).map(post));
  }
  return {
    /** Sends one turn's report (after the queued ones); false when it was not sent. */
    send(record) {
      const body = reportBody(record);
      if (!body) return Promise.resolve(false);
      flush();
      return post(body);
    },
    flush,
    get pending() {
      return queue.length;
    },
  };
}

// A catch-up when the network comes back that fails (the first request after the event
// can still meet a dead connection) is tried once more after this.
export const ONLINE_RETRY_MS = 2_000;

/**
 * The handler for the window's `online` event: queued reports go again, and every live
 * chat reads its transcript, since a socket that stayed open through the outage (as in
 * Chromium's offline emulation) neither reconnects nor delivers what it missed.
 */
export function onNetworkBack({ chats = null, reports = null, retryMs = ONLINE_RETRY_MS, timers = globalThis }) {
  return async () => {
    if (reports) reports.flush();
    if (!chats) return;
    if (!(await chats.catchUp())) timers.setTimeout(() => chats.catchUp(), retryMs);
  };
}

// ---- One chatjs session ----

const isRefused = (error) => Boolean(error) && REFUSED_TYPES.includes(error.type);
export { isRefused };

/**
 * One contact's customer session through chatjs. `chatjs` is the library's
 * `ChatSession` object (injected, so tests use a fake). The participant token stays in
 * this object's closure for a reconnect and is never exposed.
 */
export function createConnectChat({ chatjs, details, region, expiresAt = null, restartLine = false, warmed = null }) {
  let session = null;
  let closed = false;
  let failed = false;
  let discarded = false;
  let reconnecting = null;
  let restart = restartLine;
  const seen = new Set();
  const listeners = new Set();
  const failureListeners = new Set();
  const info = { contactId: details.contactId, expiresAt, warmed, reconnected: false, catchUps: 0, lastSentAt: null };

  function dispatch(item) {
    if (discarded || !item || typeof item !== "object" || !item.Id) return;
    if (seen.has(item.Id)) return;
    seen.add(item.Id);
    // Between turns, items are dropped; only the end of the chat is kept.
    if (item.Type === "EVENT" && item.ContentType === CHAT_ENDED) closed = true;
    for (const listener of [...listeners]) listener(item);
  }

  function fail() {
    if (failed) return;
    failed = true;
    for (const listener of [...failureListeners]) listener();
  }

  // True when the transcript was read (or there is nothing to read), false when it failed.
  async function catchUp() {
    if (discarded || !session) return true;
    try {
      const response = await session.getTranscript({ maxResults: 100, sortOrder: "ASCENDING", scanDirection: "BACKWARD" });
      const items = (response && response.data && response.data.Transcript) || [];
      info.catchUps += 1;
      for (const item of items) dispatch(item);
      return true;
    } catch {
      // A failed catch-up leaves the socket's items; the next connection event tries again.
      return false;
    }
  }

  async function open() {
    const next = chatjs.create({
      chatDetails: { ...details },
      type: "CUSTOMER",
      disableCSM: true,
      options: { region },
    });
    next.onMessage((event) => {
      if (next === session) dispatch(event && event.data);
    });
    next.onEnded(() => {
      if (next === session) closed = true;
    });
    // onConnectionEstablished can fire twice after connect() (chatjs issues 124 and 298);
    // the catch-up is idempotent by Id.
    next.onConnectionEstablished(() => {
      if (next === session) catchUp();
    });
    next.onConnectionBroken(() => {
      if (next === session) broken();
    });
    session = next;
    await next.connect();
  }

  async function reconnect() {
    if (!reconnecting) {
      reconnecting = (async () => {
        try {
          await open();
          info.reconnected = true;
          await catchUp();
          return true;
        } catch {
          return false;
        }
      })().finally(() => {
        reconnecting = null;
      });
    }
    return reconnecting;
  }

  async function broken() {
    if (discarded || closed || failed) return;
    if (!(await reconnect())) fail();
  }

  return {
    get contactId() {
      return info.contactId;
    },
    get expiresAt() {
      return info.expiresAt;
    },
    get closed() {
      return closed;
    },
    get failed() {
      return failed;
    },
    get info() {
      return { ...info };
    },
    setWarmed(count) {
      info.warmed = count;
    },
    /** Connects the first session; rejects when Connect refuses the connection. */
    connect: open,
    reconnect,
    catchUp,
    /** True once: whether the next turn opens with the restart line. */
    takeRestartLine() {
      const value = restart;
      restart = false;
      return value;
    },
    markClosed() {
      closed = true;
    },
    /** Every new item, deduplicated by Id, to `onItem`; `onFailed` when the socket is gone. */
    listen(onItem, onFailed = () => {}) {
      listeners.add(onItem);
      failureListeners.add(onFailed);
      return () => {
        listeners.delete(onItem);
        failureListeners.delete(onFailed);
      };
    },
    async send(text) {
      const response = await session.sendMessage({ contentType: "text/plain", message: text });
      const data = (response && response.data) || {};
      if (data.AbsoluteTime) info.lastSentAt = data.AbsoluteTime;
      return { Id: data.Id || null, AbsoluteTime: data.AbsoluteTime || null };
    },
    /** Ends the contact (DisconnectParticipant); never throws. */
    async disconnect() {
      closed = true;
      try {
        if (session) await session.disconnectParticipant();
      } catch {
        // Already ended, or never connected.
      }
    },
    /** Stops delivering events; the chat stays as Connect has it. */
    discard() {
      discarded = true;
      listeners.clear();
      failureListeners.clear();
    },
  };
}

// ---- The page's chats ----

/**
 * The chats of one page load, one per thread. `fetch` and `loadChatjs` are injected;
 * `getToken` gives the page's access token and `refreshToken` refreshes it when it has
 * less than 50 minutes left (a new chat's hop tokens expire with it). `now` is epoch ms.
 */
export function createConnectChatClient({
  rules,
  fetch: fetchImpl,
  getToken,
  refreshToken = async () => {},
  loadChatjs,
  now = () => Date.now(),
  startTimeoutMs = START_TIMEOUT_MS,
}) {
  const threads = new Map();
  // The contact of a thread the page left, for the next start's previousContactId.
  let leftContactId = null;
  let configuredRegion = null;

  async function requestStart(previousContactId) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), startTimeoutMs);
    try {
      const response = await fetchImpl(rules.start, {
        method: "POST",
        headers: { authorization: `Bearer ${getToken()}`, "content-type": "application/json" },
        body: JSON.stringify(previousContactId ? { previousContactId } : {}),
        signal: controller.signal,
      });
      if (response.status === 401) return { result: { ok: false, reason: "signin" } };
      if (!response.ok || !response.body) return { result: { ok: false, reason: "unavailable" } };
      let second = null;
      let onSecond = null;
      const first = await readStartStream(response.body, (line) => {
        second = line;
        if (onSecond) onSecond(line);
      });
      clearTimeout(timer);
      return {
        result: startResult(first),
        warmed: (callback) => {
          if (second) callback(second);
          else onSecond = callback;
        },
      };
    } catch {
      return { result: { ok: false, reason: "unavailable" } };
    } finally {
      clearTimeout(timer);
    }
  }

  async function begin(threadId, { previousContactId = null, restartLine = false } = {}) {
    const started = now();
    const { result, warmed } = await requestStart(previousContactId);
    if (!result.ok) {
      return { ok: false, reason: result.reason === "signin" ? "signin" : "unavailable", startedAt: started };
    }
    let chatjs;
    try {
      chatjs = await loadChatjs();
      if (configuredRegion !== result.region) {
        chatjs.setGlobalConfig({
          region: result.region,
          features: { messageReceipts: { shouldSendMessageReceipts: false } },
        });
        configuredRegion = result.region;
      }
    } catch {
      return { ok: false, reason: "unavailable", contactId: result.details.contactId, startedAt: started };
    }
    const chat = createConnectChat({
      chatjs,
      details: result.details,
      region: result.region,
      expiresAt: result.expiresAt,
      // The route's `restarted` says it ended the previous contact, which a new thread
      // asks for too; the restart line is for a thread whose chat was replaced.
      restartLine,
    });
    warmed((line) => {
      if (Number.isFinite(line.warmed)) chat.setWarmed(line.warmed);
    });
    try {
      await withTimeout(chat.connect(), CONNECT_TIMEOUT_MS);
    } catch {
      chat.discard();
      return { ok: false, reason: "connect_failed", contactId: result.details.contactId, startedAt: started };
    }
    return { ok: true, chat, startedAt: started, readyAt: now(), serverTiming: result.timing };
  }

  function entryFor(threadId) {
    let entry = threads.get(threadId);
    if (!entry) {
      entry = { threadId, promise: null, chat: null, transport: "connect", fallback: null, lastContactId: null };
      threads.set(threadId, entry);
    }
    return entry;
  }

  /** Starts a chat for the thread; a start already in flight for it is the one returned. */
  function start(threadId, { restartLine = false, previousContactId } = {}) {
    const entry = entryFor(threadId);
    if (entry.transport === "bridge") return Promise.resolve({ ok: false, reason: "bridge" });
    if (entry.promise && !entry.settled) return entry.promise;
    let previous = previousContactId ?? (entry.chat ? entry.chat.contactId : null);
    if (!previous && leftContactId) {
      previous = leftContactId;
      leftContactId = null;
    }
    if (entry.chat) entry.chat.discard();
    entry.chat = null;
    entry.settled = false;
    const promise = begin(threadId, { previousContactId: previous, restartLine }).then((outcome) => {
      if (threads.get(threadId) !== entry || entry.promise !== promise) {
        // The page left this thread while it started; end the chat it no longer needs.
        if (outcome.ok) {
          outcome.chat.disconnect();
          outcome.chat.discard();
        }
        return { ok: false, reason: "left" };
      }
      entry.settled = true;
      if (outcome.contactId) entry.lastContactId = outcome.contactId;
      if (outcome.ok) {
        entry.chat = outcome.chat;
        entry.lastContactId = outcome.chat.contactId;
      } else if (outcome.reason !== "signin") {
        entry.transport = "bridge";
        entry.fallback = outcome.reason === "connect_failed" ? "connect_failed" : "start_unavailable";
      }
      return outcome;
    });
    entry.promise = promise;
    return promise;
  }

  /**
   * The thread's chat for a question: the one in flight or ready, or a new one when there
   * is none, it ended, or fewer than five minutes are left before expiresAt (after the
   * page's token refresh), with the restart line when one replaces another.
   */
  async function ready(threadId) {
    const entry = entryFor(threadId);
    const bridge = () => ({ ok: false, reason: "bridge", fallback: entry.fallback, contactId: entry.lastContactId });
    if (entry.transport === "bridge") return bridge();
    const waitStart = now();
    let current = await (entry.promise && !entry.settled ? entry.promise : Promise.resolve(entry.chat ? { ok: true, chat: entry.chat } : null));
    if (!current || !current.ok) {
      if (current && current.reason === "bridge") return bridge();
      current = await start(threadId);
    } else if (current.chat.failed) {
      // The socket broke and one reconnect failed: the thread goes on through the bridge.
      entry.transport = "bridge";
      entry.fallback = "socket_failed";
      return bridge();
    } else if (current.chat.closed) {
      current = await start(threadId, { restartLine: true });
    } else if (current.chat.expiresAt !== null && current.chat.expiresAt - now() < EXPIRY_MARGIN_MS) {
      await refreshToken();
      current = await start(threadId, { restartLine: true });
    }
    if (!current.ok && current.reason !== "signin") {
      return entry.transport === "bridge" ? bridge() : { ...current, ok: false, contactId: entry.lastContactId };
    }
    return { ...current, waitedMs: now() - waitStart };
  }

  /** A refused send: one reconnect on the same contact first, then a new chat (bridge order). */
  async function recover(threadId, chat, { reconnected = false } = {}) {
    if (!reconnected && (await chat.reconnect())) return { ok: true, chat, reconnected: true };
    chat.markClosed();
    return ready(threadId);
  }

  return {
    start,
    ready,
    recover,
    /** The thread the page leaves: its contact goes on the next start as previousContactId. */
    leave(threadId) {
      const entry = threads.get(threadId);
      if (!entry) return;
      threads.delete(threadId);
      entry.promise = null;
      if (entry.chat) entry.chat.discard();
      if (entry.lastContactId) leftContactId = entry.lastContactId;
    },
    transportOf(threadId) {
      return threads.get(threadId)?.transport ?? "connect";
    },
    contactOf(threadId) {
      return threads.get(threadId)?.lastContactId ?? null;
    },
    useBridge(threadId, reason = "socket_failed") {
      const entry = entryFor(threadId);
      entry.transport = "bridge";
      entry.fallback = reason;
    },
    /**
     * A catch-up read for every live chat, for a tab that comes back into view or a network
     * that comes back; true when every read succeeded.
     */
    async catchUp() {
      const reads = [...threads.values()].filter((entry) => entry.chat).map((entry) => entry.chat.catchUp());
      const results = await Promise.all(reads);
      return results.every(Boolean);
    },
    /** Ends every live chat, each capped at SIGN_OUT_CAP_MS, before the sign-out redirect. */
    async signOut(capMs = SIGN_OUT_CAP_MS) {
      const chats = [...threads.values()].map((entry) => entry.chat).filter(Boolean);
      threads.clear();
      await Promise.race([
        Promise.all(chats.map((chat) => chat.disconnect())),
        new Promise((resolve) => setTimeout(resolve, capMs)),
      ]);
    },
  };
}
