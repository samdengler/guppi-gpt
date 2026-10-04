import { test } from "node:test";
import assert from "node:assert/strict";

import {
  classify,
  createTurnAssembler,
  createConnectChat,
  createConnectChatClient,
  readStartStream,
  reportBody,
  startResult,
  stripMark,
} from "../src/connect-chat.js";
import { connectChatFor } from "../src/project.js";

// The HR manifest's block (guppi-hr connect/web/manifest.json), as the page reads it.
const END_MARK = "⁣";
const CLOSED_MARK = "⁤";
const RULES = connectChatFor({
  name: "hr",
  label: "HR Assistant",
  agent: "/api/hr/invocations",
  capabilities: ["warm-start", "connect-chat"],
  connectChat: {
    start: "/api/hr/chat/start",
    report: "/api/hr/chat/report",
    endMark: END_MARK,
    closedMark: CLOSED_MARK,
    hiddenPrefix: "[flow]",
    endLine: "[flow] end",
    closedLine: "[flow] closed",
    escalationPrefix: "[flow] Escalation",
    errorPrefix: "[flow] The Agentic CX block returned an error",
    quietAfterMs: 800,
    turnLimitMs: 28000,
    maxChars: 1024,
    lines: {
      noReply: "No answer came back from the HR assistant. Try again in a moment.",
      escalated: "The HR service desk queue has this conversation now. A new message here starts over with the assistant.",
      error: "The HR assistant ran into an error and ended this conversation. A new message here starts a new one.",
      ended: "The conversation ended. A new message here starts a new one.",
    },
  },
});

const T0 = Date.parse("2026-10-04T12:00:00.000Z");
const at = (ms) => new Date(T0 + ms).toISOString();
let ids = 0;
const bot = (text, ms = 100, extra = {}) => ({
  Id: `i${++ids}`,
  Type: "MESSAGE",
  ParticipantRole: "SYSTEM",
  ContentType: "text/plain",
  Content: text,
  AbsoluteTime: at(ms),
  ...extra,
});
const customer = (text, ms = 0) => ({ ...bot(text, ms), ParticipantRole: "CUSTOMER" });
const event = (contentType, ms = 100) => ({ Id: `i${++ids}`, Type: "EVENT", ContentType: contentType, AbsoluteTime: at(ms) });
const ENDED = "application/vnd.amazonaws.connect.event.chat.ended";
const LEFT = "application/vnd.amazonaws.connect.event.participant.left";

function clock(start = 0) {
  let now = start;
  const fn = () => now;
  fn.advance = (ms) => {
    now += ms;
  };
  return fn;
}

// A turn whose own message is `own` at T0; returns the assembler and its clock.
function turn() {
  const now = clock();
  const assembler = createTurnAssembler({ rules: RULES, now });
  return { now, assembler, own: customer("hello", 0) };
}

const texts = (outputs) => outputs.filter((o) => o.type === "text").map((o) => o.text);
const customs = (outputs) => outputs.filter((o) => o.type === "custom").map((o) => o.name);

// ---- classify: the bridge's cases ----

test("classify keeps the designer's text and drops the customer's own message and hidden lines", () => {
  assert.equal(classify(customer("hi"), RULES), null);
  assert.equal(classify(bot("[flow] The conversation ended."), RULES), null);
  assert.equal(classify(bot("Done."), RULES).text, "Done.");
  assert.equal(classify(bot("[flow] Escalation: transferring you."), RULES).kind, "escalated");
  assert.equal(classify(event(ENDED), RULES).kind, "ended");
  assert.equal(classify(event(LEFT), RULES).kind, "ended");
  assert.equal(classify(event("application/vnd.amazonaws.connect.event.participant.joined"), RULES), null);
});

test("classify strips the end and closed marks and names them", () => {
  assert.deepEqual(classify(bot(`Your address is 1 Main St.${END_MARK}`), RULES), {
    kind: "text",
    text: "Your address is 1 Main St.",
    mark: "end",
  });
  assert.deepEqual(classify(bot(`Have a good day.${CLOSED_MARK}`), RULES), { kind: "text", text: "Have a good day.", mark: "closed" });
  assert.equal(stripMark(`a${END_MARK}${END_MARK}`, END_MARK), "a");
});

test("classify reads the legacy end and closed lines and the designer's error line", () => {
  assert.equal(classify(bot("[flow] end"), RULES).kind, "end");
  assert.equal(classify(bot("[flow] closed"), RULES).kind, "closed");
  assert.equal(classify(bot("[flow] The Agentic CX block returned an error."), RULES).kind, "error");
});

// ---- the assembler ----

test("an end mark ends the turn at once and is never shown", () => {
  const { assembler, own } = turn();
  assert.deepEqual(assembler.sent(own), []);
  const out = assembler.push(bot(`Your address is 1 Main St.${END_MARK}`, 900));
  assert.deepEqual(texts(out), ["Your address is 1 Main St."]);
  assert.equal(assembler.done.reason, "end_mark");
  assert.equal(assembler.deadline(), null);
});

test("the legacy end line ends the turn and is never shown", () => {
  const { assembler, own } = turn();
  assembler.sent(own);
  assert.deepEqual(texts(assembler.push(bot("Your address is 1 Main St.", 500))), ["Your address is 1 Main St."]);
  assert.deepEqual(assembler.push(bot("[flow] end", 600)), []);
  assert.equal(assembler.done.reason, "end_mark");
});

test("items are buffered until the send resolves, the own message dropped, the rest in time order", () => {
  const { assembler, own } = turn();
  const first = bot("First.", 300);
  const second = bot(`Second.${END_MARK}`, 400);
  assert.deepEqual(assembler.push(second), []);
  assert.deepEqual(assembler.push(own), []);
  assert.deepEqual(assembler.push(first), []);
  const out = assembler.sent(own);
  assert.deepEqual(texts(out), ["First.", "Second."]);
  assert.equal(assembler.done.reason, "end_mark");
});

test("a late reply from the previous turn is dropped and counted as stale", () => {
  const { assembler } = turn();
  const late = bot("A late extra line for turn one.", -2000);
  assembler.push(late);
  assembler.sent(customer("two", 0));
  assert.deepEqual(texts(assembler.push(bot(`Answer two.${END_MARK}`, 700))), ["Answer two."]);
  assert.equal(assembler.summary().stale, 1);
});

test("an item delivered twice (socket and catch-up) is taken once", () => {
  const { assembler, own } = turn();
  assembler.sent(own);
  const reply = bot("Once.", 300);
  assert.deepEqual(texts(assembler.push(reply)), ["Once."]);
  assert.deepEqual(assembler.push({ ...reply }), []);
});

test("a reply without a mark ends after quietAfterMs of quiet", () => {
  const { now, assembler, own } = turn();
  assembler.sent(own);
  now.advance(1000);
  assert.deepEqual(texts(assembler.push(bot("A journey answer.", 1000))), ["A journey answer."]);
  assert.equal(assembler.deadline(), 1800);
  now.advance(500);
  assert.deepEqual(assembler.tick(), []);
  assert.equal(assembler.done, null);
  // A second message inside the quiet window restarts it.
  assert.deepEqual(texts(assembler.push(bot("More.", 1500))), ["More."]);
  now.advance(799);
  assembler.tick();
  assert.equal(assembler.done, null);
  now.advance(1);
  assembler.tick();
  assert.equal(assembler.done.reason, "quiet");
  assert.equal(assembler.summary().endName, "quiet after the reply");
});

test("no reply within turnLimitMs gives the no-reply line", () => {
  const { now, assembler, own } = turn();
  assembler.sent(own);
  assert.equal(assembler.deadline(), 28000);
  now.advance(27999);
  assert.deepEqual(assembler.tick(), []);
  now.advance(1);
  assert.deepEqual(texts(assembler.tick()), [RULES.lines.noReply]);
  assert.equal(assembler.done.reason, "no_reply");
  assert.equal(assembler.summary().closed, false);
});

test("a reply that is still going at the turn limit ends without the no-reply line", () => {
  const now = clock();
  const assembler = createTurnAssembler({ rules: { ...RULES, quietAfterMs: 30000 }, now });
  assembler.sent(customer("hello", 0));
  assembler.push(bot("Partial.", 100));
  now.advance(28000);
  assert.deepEqual(assembler.tick(), []);
  assert.equal(assembler.done.reason, "quiet");
  assert.equal(assembler.summary().endName, "turn limit");
});

test("escalation becomes a custom event and the closing line, and closes the conversation", () => {
  const { assembler, own } = turn();
  assembler.sent(own);
  const first = assembler.push(bot("Connecting you to the HR service desk.", 300));
  const second = assembler.push(bot("[flow] Escalation: transferring you.", 400));
  assert.deepEqual(texts(first), ["Connecting you to the HR service desk."]);
  assert.deepEqual(second, [
    { type: "custom", name: "connect/escalated", text: RULES.lines.escalated },
    { type: "text", text: RULES.lines.escalated },
  ]);
  assert.equal(assembler.done.reason, "closed");
  assert.equal(assembler.summary().closed, true);
});

test("the designer's error line is shown as the error line and ends the conversation", () => {
  const { assembler, own } = turn();
  assembler.sent(own);
  const out = assembler.push(bot("[flow] The Agentic CX block returned an error.", 300));
  assert.deepEqual(texts(out), [RULES.lines.error]);
  assert.deepEqual(customs(out), ["connect/error"]);
  assert.deepEqual(assembler.push(event(LEFT, 400)), []);
  assert.equal(assembler.summary().error, "designer_error");
  assert.equal(assembler.summary().closed, true);
});

test("the closed mark shows the reply, then connect/closed, and closes the conversation", () => {
  const { assembler, own } = turn();
  assembler.sent(own);
  const out = assembler.push(bot(`I opened HR ticket HR-1. This conversation is now closed.${CLOSED_MARK}`, 300));
  assert.deepEqual(out, [
    { type: "text", text: "I opened HR ticket HR-1. This conversation is now closed." },
    { type: "custom", name: "connect/closed", text: "" },
  ]);
  assert.equal(assembler.done.reason, "closed");
  assert.equal(assembler.summary().closed, true);
});

test("the legacy closed line closes the conversation with only the custom event", () => {
  const { assembler, own } = turn();
  assembler.sent(own);
  assembler.push(bot("I opened HR ticket HR-1.", 300));
  assert.deepEqual(assembler.push(bot("[flow] closed", 400)), [{ type: "custom", name: "connect/closed", text: "" }]);
  assert.equal(assembler.summary().closed, true);
});

test("chat.ended and participant.left end the turn with the ended line", () => {
  for (const contentType of [ENDED, LEFT]) {
    const { assembler, own } = turn();
    assembler.sent(own);
    const out = assembler.push(event(contentType, 300));
    assert.deepEqual(out, [
      { type: "custom", name: "connect/ended", text: RULES.lines.ended },
      { type: "text", text: RULES.lines.ended },
    ]);
    assert.equal(assembler.done.reason, "ended");
  }
});

test("hidden lines between replies are not shown and do not end the turn", () => {
  const { assembler, own } = turn();
  assembler.sent(own);
  assert.deepEqual(assembler.push(bot("[flow] Routing to the Profile agent", 200)), []);
  assert.deepEqual(texts(assembler.push(bot(`Done.${END_MARK}`, 300))), ["Done."]);
});

test("the summary carries Connect's times for the report", () => {
  const { now, assembler, own } = turn();
  assembler.sent({ Id: own.Id, AbsoluteTime: own.AbsoluteTime });
  now.advance(1200);
  assembler.push(bot("One.", 1100));
  now.advance(300);
  assembler.push(bot(`Two.${END_MARK}`, 1400));
  const summary = assembler.summary();
  assert.equal(summary.sentAt, at(0));
  assert.equal(summary.firstItemAt, at(1100));
  assert.equal(summary.lastItemAt, at(1400));
  assert.equal(summary.firstItemMs, 1200);
  assert.equal(summary.endMs, 1500);
});

// ---- the start route's answer ----

const LINE_1 = {
  data: { startChatResult: { ContactId: "c-1", ParticipantId: "p-1", ParticipantToken: "secret-token" } },
  region: "us-east-1",
  startedAt: T0,
  expiresAt: T0 + 3_600_000,
  restarted: false,
  timing: { steps: [] },
};

test("startResult reads the chat details and refuses anything else", () => {
  const result = startResult(LINE_1);
  assert.equal(result.ok, true);
  assert.deepEqual(result.details, { contactId: "c-1", participantId: "p-1", participantToken: "secret-token" });
  assert.equal(result.region, "us-east-1");
  assert.equal(result.expiresAt, T0 + 3_600_000);
  assert.deepEqual(startResult({ error: "signin" }), { ok: false, reason: "signin" });
  assert.deepEqual(startResult({ error: "unavailable" }), { ok: false, reason: "unavailable" });
  assert.equal(startResult({ data: { startChatResult: { ContactId: "c" } }, region: "us-east-1" }).reason, "malformed");
  assert.equal(startResult({ ...LINE_1, region: "javascript:" }).reason, "malformed");
  assert.equal(startResult(null).reason, "malformed");
});

function ndjson(lines, { split = false } = {}) {
  const text = lines.map((line) => JSON.stringify(line)).join("\n") + "\n";
  const bytes = new TextEncoder().encode(text);
  const chunks = split ? [bytes.slice(0, 7), bytes.slice(7, 40), bytes.slice(40)] : [bytes];
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

test("readStartStream answers with the first line and hands the second on later", async () => {
  let second = null;
  const first = await readStartStream(ndjson([LINE_1, { warmed: 3 }], { split: true }), (line) => {
    second = line;
  });
  assert.equal(first.data.startChatResult.ContactId, "c-1");
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(second, { warmed: 3 });
});

// ---- the report ----

test("reportBody keeps ids, Connect times and the end reason, never text or tokens", () => {
  const body = reportBody({
    contactId: "c-1",
    runId: "r-1",
    threadId: "t-1",
    sentAt: at(0),
    firstItemAt: at(100),
    lastItemAt: "not a time",
    endReason: "quiet",
    error: "designer_error",
    transport: "connect",
    timing: { steps: [{ name: "SendMessage", start_ms: 1, end_ms: 2, lane: "connect" }], total_ms: 5, notes: [] },
    text: "the reply",
    participantToken: "secret-token",
  });
  assert.deepEqual(Object.keys(body).sort(), ["contactId", "endReason", "error", "firstItemAt", "runId", "sentAt", "threadId", "timing", "transport"]);
  assert.equal(JSON.stringify(body).includes("secret-token"), false);
  assert.equal(JSON.stringify(body).includes("the reply"), false);
  assert.equal(reportBody({ contactId: "", endReason: "quiet" }), null);
  assert.equal(reportBody({ contactId: "c", endReason: "whatever" }), null);
  assert.equal(reportBody({ contactId: "c", endReason: "end_mark", transport: "bridge" }).transport, "bridge");
});

// ---- the chatjs session ----

// A fake of chatjs's ChatSession object: records every create and call.
function fakeChatjs({ connectFails = [], transcript = [] } = {}) {
  const sessions = [];
  const lib = {
    sessions,
    configs: [],
    setGlobalConfig(config) {
      lib.configs.push(config);
    },
    create(args) {
      const handlers = {};
      const index = sessions.length;
      const session = {
        args,
        calls: [],
        handlers,
        onMessage: (fn) => (handlers.message = fn),
        onEnded: (fn) => (handlers.ended = fn),
        onConnectionEstablished: (fn) => (handlers.established = fn),
        onConnectionBroken: (fn) => (handlers.broken = fn),
        async connect() {
          session.calls.push("connect");
          if (connectFails[index]) throw { connectSuccess: false };
          return { connectCalled: true, connectSuccess: true };
        },
        async getTranscript(request) {
          session.calls.push(["getTranscript", request]);
          return { data: { Transcript: transcript } };
        },
        async sendMessage(request) {
          session.calls.push(["sendMessage", request]);
          if (session.refuse) throw { type: "AccessDeniedException" };
          return { data: { Id: "own-1", AbsoluteTime: at(0) } };
        },
        async disconnectParticipant() {
          session.calls.push("disconnectParticipant");
          if (session.slowDisconnect) await new Promise(() => {});
          return { data: {} };
        },
        sendEvent() {
          session.calls.push("sendEvent");
        },
      };
      sessions.push(session);
      return session;
    },
  };
  return lib;
}

const DETAILS = { contactId: "c-1", participantId: "p-1", participantToken: "secret-token" };
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test("a chat session is a customer session with client-side metrics off and no logger", async () => {
  const lib = fakeChatjs();
  const chat = createConnectChat({ chatjs: lib, details: DETAILS, region: "us-east-1" });
  await chat.connect();
  const { args } = lib.sessions[0];
  assert.equal(args.type, "CUSTOMER");
  assert.equal(args.disableCSM, true);
  assert.deepEqual(args.options, { region: "us-east-1" });
  assert.deepEqual(args.chatDetails, DETAILS);
  assert.equal("loggerConfig" in args, false);
  assert.deepEqual(await chat.send("hello"), { Id: "own-1", AbsoluteTime: at(0) });
  assert.deepEqual(lib.sessions[0].calls[1], ["sendMessage", { contentType: "text/plain", message: "hello" }]);
  assert.equal(lib.sessions[0].calls.includes("sendEvent"), false);
  // The token is never exposed by the chat object.
  assert.equal(JSON.stringify(chat.info).includes("secret-token"), false);
});

test("catch-up after every connection event is idempotent by Id", async () => {
  const reply = bot("Missed while offline.", 300);
  const lib = fakeChatjs({ transcript: [customer("hello", 0), reply] });
  const chat = createConnectChat({ chatjs: lib, details: DETAILS, region: "us-east-1" });
  await chat.connect();
  const seen = [];
  chat.listen((item) => seen.push(item.Id));
  const session = lib.sessions[0];
  // chatjs issues 124 and 298: the event can fire twice.
  session.handlers.established();
  session.handlers.established();
  await tick();
  session.handlers.message({ data: reply });
  await chat.catchUp();
  assert.equal(seen.filter((id) => id === reply.Id).length, 1);
  assert.equal(seen.length, 2);
  const transcriptCalls = session.calls.filter((call) => Array.isArray(call) && call[0] === "getTranscript");
  assert.equal(transcriptCalls.length, 3);
  assert.deepEqual(transcriptCalls[0][1], { maxResults: 100, sortOrder: "ASCENDING", scanDirection: "BACKWARD" });
});

test("chat.ended between turns marks the chat closed", async () => {
  const lib = fakeChatjs();
  const chat = createConnectChat({ chatjs: lib, details: DETAILS, region: "us-east-1" });
  await chat.connect();
  lib.sessions[0].handlers.message({ data: event(ENDED, 500) });
  assert.equal(chat.closed, true);
});

test("a broken socket gets one reconnect on the same participant, then the chat fails", async () => {
  const lib = fakeChatjs({ connectFails: [false, false, true] });
  const chat = createConnectChat({ chatjs: lib, details: DETAILS, region: "us-east-1" });
  await chat.connect();
  let failures = 0;
  chat.listen(() => {}, () => failures++);
  lib.sessions[0].handlers.broken();
  await tick();
  await tick();
  assert.equal(lib.sessions.length, 2);
  assert.deepEqual(lib.sessions[1].args.chatDetails, DETAILS);
  assert.equal(chat.failed, false);
  assert.equal(chat.info.reconnected, true);
  // An event from the old session no longer counts.
  lib.sessions[0].handlers.broken();
  await tick();
  assert.equal(lib.sessions.length, 2);
  lib.sessions[1].handlers.broken();
  await tick();
  await tick();
  assert.equal(lib.sessions.length, 3);
  assert.equal(chat.failed, true);
  assert.equal(failures, 1);
});

test("the restart line is given once", () => {
  const chat = createConnectChat({ chatjs: fakeChatjs(), details: DETAILS, region: "us-east-1", restartLine: true });
  assert.equal(chat.takeRestartLine(), true);
  assert.equal(chat.takeRestartLine(), false);
});

// ---- the page's chats ----

function startServer(answers) {
  const requests = [];
  const fetch = async (url, init) => {
    requests.push({ url, init, body: JSON.parse(init.body) });
    const answer = answers.length > 1 ? answers.shift() : answers[0];
    if (answer.status && answer.status !== 200) return { status: answer.status, ok: false, body: null };
    if (answer.hang) return new Promise(() => {});
    const gate = answer.gate || Promise.resolve();
    await gate;
    return { status: 200, ok: true, body: ndjson(answer.lines) };
  };
  return { fetch, requests };
}

function client(server, lib = fakeChatjs(), extra = {}) {
  return createConnectChatClient({
    rules: RULES,
    fetch: server.fetch,
    getToken: () => "okta-access-token",
    loadChatjs: async () => lib,
    ...extra,
  });
}

const line = (contactId, extra = {}) => ({
  ...LINE_1,
  data: { startChatResult: { ContactId: contactId, ParticipantId: `p-${contactId}`, ParticipantToken: `pt-${contactId}` } },
  expiresAt: Date.now() + 3_600_000,
  ...extra,
});

test("the warm start posts to the start route with the page's token and connects the chat", async () => {
  const server = startServer([{ lines: [line("c-1"), { warmed: 3, timing: {} }] }]);
  const lib = fakeChatjs();
  const chats = client(server, lib);
  const outcome = await chats.start("t1");
  assert.equal(outcome.ok, true);
  assert.equal(server.requests[0].url, "/api/hr/chat/start");
  assert.equal(server.requests[0].init.method, "POST");
  assert.equal(server.requests[0].init.headers.authorization, "Bearer okta-access-token");
  assert.equal(server.requests[0].init.headers["content-type"], "application/json");
  assert.deepEqual(server.requests[0].body, {});
  assert.deepEqual(lib.configs, [{ region: "us-east-1", features: { messageReceipts: { shouldSendMessageReceipts: false } } }]);
  assert.equal(lib.sessions[0].calls[0], "connect");
  await tick();
  assert.equal(outcome.chat.info.warmed, 3);
});

test("a question sent before the first line arrives waits for that one start", async () => {
  let open;
  const gate = new Promise((resolve) => (open = resolve));
  const server = startServer([{ lines: [line("c-1")], gate }]);
  const chats = client(server);
  const warm = chats.start("t1");
  const question = chats.ready("t1");
  await tick();
  assert.equal(server.requests.length, 1);
  open();
  const [a, b] = await Promise.all([warm, question]);
  assert.equal(server.requests.length, 1);
  assert.equal(a.chat, b.chat);
  assert.equal(b.ok, true);
});

test("a refused sign-in is the sign-in line, and the next question tries the start again", async () => {
  const server = startServer([{ lines: [{ error: "signin" }] }, { status: 401 }, { lines: [line("c-2")] }]);
  const chats = client(server);
  assert.equal((await chats.ready("t1")).reason, "signin");
  assert.equal((await chats.ready("t1")).reason, "signin");
  assert.equal((await chats.ready("t1")).ok, true);
  assert.equal(chats.transportOf("t1"), "connect");
});

test("an unavailable start puts the thread on the bridge", async () => {
  const server = startServer([{ lines: [{ error: "unavailable" }] }]);
  const chats = client(server);
  const ready = await chats.ready("t1");
  assert.equal(ready.reason, "bridge");
  assert.equal(ready.fallback, "start_unavailable");
  assert.equal(chats.transportOf("t1"), "bridge");
  // A new thread tries Connect again.
  assert.equal(chats.transportOf("t2"), "connect");
});

test("a chat that cannot connect puts the thread on the bridge and keeps its contact for the report", async () => {
  const server = startServer([{ lines: [line("c-1")] }]);
  const chats = client(server, fakeChatjs({ connectFails: [true] }));
  const ready = await chats.ready("t1");
  assert.deepEqual([ready.reason, ready.fallback, ready.contactId], ["bridge", "connect_failed", "c-1"]);
});

test("a chat whose socket failed for good sends the thread to the bridge", async () => {
  const server = startServer([{ lines: [line("c-1")] }]);
  const lib = fakeChatjs({ connectFails: [false, true] });
  const chats = client(server, lib);
  await chats.start("t1");
  lib.sessions[0].handlers.broken();
  await tick();
  await tick();
  const ready = await chats.ready("t1");
  assert.deepEqual([ready.reason, ready.fallback, ready.contactId], ["bridge", "socket_failed", "c-1"]);
});

test("an ended chat is replaced with previousContactId and the restart line", async () => {
  const server = startServer([{ lines: [line("c-1")] }, { lines: [line("c-2")] }]);
  const lib = fakeChatjs();
  const chats = client(server, lib);
  const first = await chats.ready("t1");
  lib.sessions[0].handlers.message({ data: event(ENDED, 900) });
  const second = await chats.ready("t1");
  assert.equal(second.chat.contactId, "c-2");
  assert.deepEqual(server.requests[1].body, { previousContactId: "c-1" });
  assert.equal(second.chat.takeRestartLine(), true);
  assert.notEqual(first.chat, second.chat);
});

test("a chat with under five minutes left is replaced after the page refreshes its token", async () => {
  const now = clock(T0);
  const server = startServer([{ lines: [line("c-1", { expiresAt: T0 + 10 * 60 * 1000 })] }, { lines: [line("c-2")] }]);
  let refreshed = 0;
  const chats = client(server, fakeChatjs(), { now, refreshToken: async () => refreshed++ });
  assert.equal((await chats.ready("t1")).chat.contactId, "c-1");
  now.advance(4 * 60 * 1000);
  assert.equal((await chats.ready("t1")).chat.contactId, "c-1");
  now.advance(2 * 60 * 1000);
  const replaced = await chats.ready("t1");
  assert.equal(refreshed, 1);
  assert.equal(replaced.chat.contactId, "c-2");
  assert.deepEqual(server.requests[1].body, { previousContactId: "c-1" });
  assert.equal(replaced.chat.takeRestartLine(), true);
});

test("the thread the page leaves is named on the next start", async () => {
  const server = startServer([{ lines: [line("c-1")] }, { lines: [line("c-2")] }]);
  const chats = client(server);
  await chats.start("t1");
  chats.leave("t1");
  await chats.start("t2");
  assert.deepEqual(server.requests[1].body, { previousContactId: "c-1" });
});

test("a reopened thread starts a new chat that opens with the restart line", async () => {
  const server = startServer([{ lines: [line("c-1")] }]);
  const chats = client(server);
  const outcome = await chats.start("old-thread", { restartLine: true });
  assert.equal(outcome.chat.takeRestartLine(), true);
});

test("a refused send gets one reconnect first, then a new chat", async () => {
  const server = startServer([{ lines: [line("c-1")] }, { lines: [line("c-2")] }]);
  const lib = fakeChatjs();
  const chats = client(server, lib);
  const { chat } = await chats.ready("t1");
  const first = await chats.recover("t1", chat);
  assert.equal(first.reconnected, true);
  assert.equal(first.chat, chat);
  const second = await chats.recover("t1", chat, { reconnected: true });
  assert.equal(second.chat.contactId, "c-2");
  assert.deepEqual(server.requests[1].body, { previousContactId: "c-1" });
});

test("sign-out ends the chats and gives up after the cap", async () => {
  const server = startServer([{ lines: [line("c-1")] }]);
  const lib = fakeChatjs();
  const chats = client(server, lib);
  await chats.start("t1");
  lib.sessions[0].slowDisconnect = true;
  const started = Date.now();
  await chats.signOut(30);
  assert.ok(Date.now() - started < 1000);
  assert.ok(lib.sessions[0].calls.includes("disconnectParticipant"));
});

test("a start the page left before it answered ends its chat", async () => {
  let open;
  const gate = new Promise((resolve) => (open = resolve));
  const server = startServer([{ lines: [line("c-1")], gate }]);
  const lib = fakeChatjs();
  const chats = client(server, lib);
  const pending = chats.start("t1");
  chats.leave("t1");
  open();
  assert.equal((await pending).reason, "left");
  assert.ok(lib.sessions[0].calls.includes("disconnectParticipant"));
});
