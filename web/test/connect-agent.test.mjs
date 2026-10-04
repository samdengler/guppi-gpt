import { test } from "node:test";
import assert from "node:assert/strict";

import { ConnectChatAgent } from "../src/connect-agent.js";
import { connectChatFor } from "../src/project.js";

const END_MARK = "⁣";
const CLOSED_MARK = "⁤";
const RULES = connectChatFor({
  name: "hr",
  label: "HR Assistant",
  agent: "/api/hr/invocations",
  capabilities: ["connect-chat"],
  connectChat: {
    start: "/api/hr/chat/start",
    report: "/api/hr/chat/report",
    endMark: END_MARK,
    closedMark: CLOSED_MARK,
    hiddenPrefix: "[flow]",
    escalationPrefix: "[flow] Escalation",
    errorPrefix: "[flow] The Agentic CX block returned an error",
    quietAfterMs: 30,
    turnLimitMs: 1000,
    maxChars: 20,
    lines: { tooLong: "Too long.", noReply: "No answer came back.", restarted: "(Restarted.)", signin: "Sign-in failed." },
  },
});

const T0 = Date.parse("2026-10-04T12:00:00.000Z");
const at = (ms) => new Date(T0 + ms).toISOString();
let ids = 0;
const bot = (text, ms = 100) => ({ Id: `i${++ids}`, Type: "MESSAGE", ParticipantRole: "SYSTEM", Content: text, AbsoluteTime: at(ms) });

/**
 * A fake chat from the transport: `script[text]` lists the items Connect pushes after the
 * message is sent (each delivered on its own timer), and `before` items arrive before the
 * send resolves.
 */
function fakeChat({ script = {}, refuse = 0, failAfterSend = false, restartLine = false, contactId = "c-1" } = {}) {
  const listeners = new Set();
  const chat = {
    contactId,
    info: { contactId, warmed: 3 },
    sent: [],
    closed: false,
    disconnected: 0,
    restart: restartLine,
    takeRestartLine() {
      const value = chat.restart;
      chat.restart = false;
      return value;
    },
    markClosed() {
      chat.closed = true;
    },
    async disconnect() {
      chat.disconnected += 1;
    },
    listen(onItem, onFailed) {
      const entry = { onItem, onFailed };
      listeners.add(entry);
      return () => listeners.delete(entry);
    },
    get listeners() {
      return listeners.size;
    },
    async send(text) {
      chat.sent.push(text);
      if (refuse > 0) {
        refuse -= 1;
        throw { type: "AccessDeniedException" };
      }
      const own = { Id: `own-${chat.sent.length}`, AbsoluteTime: at(0) };
      let delay = 1;
      for (const item of script[text] || []) {
        setTimeout(() => {
          for (const { onItem } of [...listeners]) onItem(item);
        }, (delay += 2));
      }
      if (failAfterSend) {
        setTimeout(() => {
          for (const { onFailed } of [...listeners]) onFailed();
        }, 5);
      }
      return own;
    },
  };
  return chat;
}

async function run(agent, { text = "hello", debug = true, abortController, onEvent } = {}) {
  const events = [];
  agent.messages = [{ id: "m1", role: "user", content: text }];
  const result = await agent.runAgent(
    { runId: "r1", forwardedProps: debug ? { debug: true } : {}, abortController },
    {
      onEvent: ({ event }) => {
        events.push(event);
        if (onEvent) onEvent(event, events);
      },
    },
  );
  return { events, result };
}

const types = (events) => events.map((e) => (e.type === "CUSTOM" ? `CUSTOM:${e.name}` : e.type));
const deltas = (events) => events.filter((e) => e.type === "TEXT_MESSAGE_CONTENT").map((e) => e.delta);

function agentFor(chat, extra = {}) {
  const reports = [];
  const agent = new ConnectChatAgent({
    threadId: "t1",
    rules: RULES,
    ready: { ok: true, chat, waitedMs: 0 },
    onReport: (record) => reports.push(record),
    ...extra,
  });
  return { agent, reports };
}

test("a turn follows the bridge's event order, the timing event just before RUN_FINISHED", async () => {
  const chat = fakeChat({ script: { hello: [bot(`Your address is 1 Main St.${END_MARK}`, 500)] } });
  const { agent, reports } = agentFor(chat);
  const { events } = await run(agent);
  assert.deepEqual(types(events), [
    "RUN_STARTED",
    "STEP_STARTED",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "STEP_FINISHED",
    "CUSTOM:guppi.timing",
    "RUN_FINISHED",
  ]);
  assert.equal(events[1].stepName, "Amazon Connect");
  assert.deepEqual(deltas(events), ["Your address is 1 Main St."]);
  assert.deepEqual(chat.sent, ["hello"]);
  assert.equal(chat.listeners, 0);
  const timing = events[6].value;
  assert.deepEqual(timing.ids, { contact: "c-1", run: "r1", transport: "connect" });
  assert.ok(timing.steps.some((step) => step.name === "SendMessage" && step.lane === "connect"));
  assert.ok(timing.steps.some((step) => step.name === "first reply item"));
  assert.ok(timing.steps.some((step) => step.name === "end mark"));
  assert.ok(timing.notes.some((note) => note.startsWith("Connect time: the first reply 500 ms")));
  assert.equal(reports.length, 1);
  assert.equal(reports[0].endReason, "end_mark");
  assert.equal(reports[0].sentAt, at(0));
  assert.equal(reports[0].firstItemAt, at(500));
  assert.equal(JSON.stringify(reports[0]).includes("Main St."), false);
});

test("without the debug flag no timing event is sent", async () => {
  const chat = fakeChat({ script: { hello: [bot(`Hi.${END_MARK}`)] } });
  const { events } = await run(agentFor(chat).agent, { debug: false });
  assert.equal(types(events).includes("CUSTOM:guppi.timing"), false);
});

test("a closing event sends connect/<kind> before STEP_FINISHED, and the chat is ended", async () => {
  const chat = fakeChat({ script: { bye: [bot(`I opened HR ticket HR-1.${CLOSED_MARK}`)] } });
  const { agent, reports } = agentFor(chat);
  const { events } = await run(agent, { text: "bye", debug: false });
  assert.deepEqual(types(events), [
    "RUN_STARTED",
    "STEP_STARTED",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "CUSTOM:connect/closed",
    "STEP_FINISHED",
    "RUN_FINISHED",
  ]);
  assert.equal(chat.closed, true);
  assert.equal(chat.disconnected, 1);
  assert.equal(reports[0].endReason, "closed");
});

test("escalation sends the custom event, then its line as text", async () => {
  const chat = fakeChat({ script: { help: [bot("Connecting you."), bot("[flow] Escalation: transferring you.", 200)] } });
  const { events } = await run(agentFor(chat).agent, { text: "help", debug: false });
  assert.deepEqual(types(events).slice(5, 9), ["CUSTOM:connect/escalated", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]);
  assert.deepEqual(deltas(events), ["Connecting you.", RULES.lines.escalated]);
});

test("a reply without a mark ends after the quiet period", async () => {
  const chat = fakeChat({ script: { hello: [bot("A journey answer.")] } });
  const { agent, reports } = agentFor(chat);
  const { events } = await run(agent, { debug: false });
  assert.deepEqual(deltas(events), ["A journey answer."]);
  assert.equal(types(events).at(-1), "RUN_FINISHED");
  assert.equal(reports[0].endReason, "quiet");
});

test("no reply within the turn limit gives the no-reply line", async () => {
  const chat = fakeChat();
  const { agent, reports } = agentFor(chat);
  const { events } = await run(agent, { debug: false });
  assert.deepEqual(deltas(events), ["No answer came back."]);
  assert.equal(reports[0].endReason, "no_reply");
});

test("a ping goes out every pingMs while the turn waits", async () => {
  const chat = fakeChat();
  const { agent } = agentFor(chat, { pingMs: 200 });
  const { events } = await run(agent, { debug: false });
  const pings = events.filter((e) => e.type === "CUSTOM" && e.name === "ping");
  assert.ok(pings.length >= 3 && pings.length <= 5, `${pings.length} pings`);
});

test("a message over maxChars is answered with the line and nothing is sent", async () => {
  const chat = fakeChat();
  const { agent, reports } = agentFor(chat);
  const { events } = await run(agent, { text: "x".repeat(21), debug: false });
  assert.deepEqual(deltas(events), ["Too long."]);
  assert.deepEqual(chat.sent, []);
  assert.equal(reports.length, 0);
  assert.equal(types(events).at(-1), "RUN_FINISHED");
});

test("a start that could not confirm the sign-in gives the sign-in line", async () => {
  const agent = new ConnectChatAgent({ threadId: "t1", rules: RULES, ready: { ok: false, reason: "signin" } });
  const { events } = await run(agent, { debug: false });
  assert.deepEqual(types(events), ["RUN_STARTED", "STEP_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "STEP_FINISHED", "RUN_FINISHED"]);
  assert.deepEqual(deltas(events), ["Sign-in failed."]);
});

test("a restarted chat opens the reply with the restart line", async () => {
  const chat = fakeChat({ restartLine: true, script: { hello: [bot(`Hi again.${END_MARK}`)] } });
  const { events } = await run(agentFor(chat).agent, { debug: false });
  assert.deepEqual(deltas(events), ["(Restarted.)", "Hi again."]);
});

test("a refused send recovers through the transport and resends", async () => {
  const first = fakeChat({ refuse: 1 });
  const second = fakeChat({ restartLine: true, contactId: "c-2", script: { hello: [bot(`Answer.${END_MARK}`)] } });
  const calls = [];
  const { agent, reports } = agentFor(first, {
    recover: async (chat) => {
      calls.push(chat.contactId);
      return { ok: true, chat: second };
    },
  });
  const { events } = await run(agent, { debug: false });
  assert.deepEqual(calls, ["c-1"]);
  assert.deepEqual(deltas(events), ["(Restarted.)", "Answer."]);
  assert.deepEqual(second.sent, ["hello"]);
  assert.equal(first.listeners, 0);
  assert.equal(reports[0].contactId, "c-2");
});

test("a socket that fails for good ends the run with RUN_ERROR and an error report", async () => {
  const chat = fakeChat({ failAfterSend: true });
  const { agent, reports } = agentFor(chat);
  const { events } = await run(agent, { debug: false });
  assert.equal(types(events).at(-1), "RUN_ERROR");
  assert.equal(events.at(-1).code, "SOCKET_FAILED");
  assert.deepEqual([reports[0].endReason, reports[0].error], ["aborted", "socket_failed"]);
});

test("the caller's abort controller stops the turn, unsubscribes, and reports aborted", async () => {
  const chat = fakeChat();
  const { agent, reports } = agentFor(chat);
  const abortController = new AbortController();
  setTimeout(() => abortController.abort(), 20);
  const started = Date.now();
  const { events } = await run(agent, { debug: false, abortController });
  assert.ok(Date.now() - started < 500);
  assert.equal(types(events).includes("RUN_FINISHED"), false);
  assert.equal(chat.listeners, 0);
  assert.equal(reports[0].endReason, "aborted");
});

test("the agent keeps the caller's abort controller, as HttpAgent does", async () => {
  const chat = fakeChat({ script: { hello: [bot(`Hi.${END_MARK}`)] } });
  const { agent } = agentFor(chat);
  const abortController = new AbortController();
  await run(agent, { debug: false, abortController });
  assert.equal(agent.abortController, abortController);
});

test("a wait for the chat start is a step and a note", async () => {
  const chat = fakeChat({ script: { hello: [bot(`Hi.${END_MARK}`)] } });
  const agent = new ConnectChatAgent({ threadId: "t1", rules: RULES, ready: { ok: true, chat, waitedMs: 1500 } });
  const { events } = await run(agent);
  const timing = events.find((e) => e.name === "guppi.timing").value;
  assert.deepEqual(timing.steps[0], { name: "waiting for the chat start", start_ms: 0, end_ms: 1500, lane: "page" });
  assert.ok(timing.notes.includes("waited for the chat start"));
  assert.ok(timing.notes.includes("sub-agents warmed at chat start: 3"));
});

test("the designer's error line reports endReason error with designer_error, for the report route's alarm", async () => {
  const chat = fakeChat({ script: { hello: [bot("[flow] The Agentic CX block returned an error.")] } });
  const { agent, reports } = agentFor(chat);
  const { events } = await run(agent, { debug: false });
  assert.deepEqual(deltas(events), [RULES.lines.error]);
  assert.deepEqual([reports[0].endReason, reports[0].error], ["error", "designer_error"]);
  assert.equal(chat.closed, true);
});
