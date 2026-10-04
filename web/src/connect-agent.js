// ConnectChatAgent: one question as one Connect chat turn, in the browser (guppi-hr D55).
// It speaks AG-UI to the page exactly as the Connect bridge does over HTTP
// (guppi-hr connect/agent/src/connect_bridge/turn.py, turn_events): RUN_STARTED,
// STEP_STARTED "Amazon Connect", the reply as text messages, connect/<kind> CUSTOM events
// for a closing event, STEP_FINISHED, then on a debug run a guppi.timing CUSTOM event,
// and RUN_FINISHED. While it waits it sends a CUSTOM `ping` every 15 s, as the agent kit
// does, so the page's 30 s stall timer behaves the same on both paths.
//
// The agent never sees a credential: it gets a chat from the transport's own start path
// (web/src/connect-chat.js) and calls send and listen on it.
//
// A turn that ends on the turn limit with no reply leaves its question awaiting a late
// answer (a LateWait on the chat) until the next question: a reply that arrives later, on
// the socket or from a catch-up, goes to `onLate` to replace the no-reply line, and the run
// gets a second report marked late.

import { AbstractAgent } from "@ag-ui/client";
import { Observable } from "rxjs";
import { PING_MS, STEP_NAME, createTurnAssembler, isRefused } from "./connect-chat.js";
import { TIMING_EVENT_NAME } from "./debug.js";

const randomId = () => (globalThis.crypto?.randomUUID ? globalThis.crypto.randomUUID() : `${Date.now()}-${Math.random()}`);

function lastUserText(messages) {
  for (let i = (messages || []).length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message && message.role === "user" && typeof message.content === "string") return message.content;
  }
  return "";
}

function abortError() {
  const error = new Error("signal is aborted without reason");
  error.name = "AbortError";
  return error;
}

// Notes for the timing event, as the bridge's STAT_NOTES.
const NOTES = {
  waited: "waited for the chat start",
  restarted: "a new chat replaced the thread's chat",
  reconnected: "a fresh connection for the same contact",
  stale: "a previous turn's replies arrived late and were not shown",
  noReply: "no reply within the turn limit",
  late: "a late reply replaced the no-reply line",
  designerError: "the designer reported an error",
  tooLong: "message too long: nothing sent",
  signin: "the chat start could not confirm the sign-in",
};

export class ConnectChatAgent extends AbstractAgent {
  /**
   * `ready` is the transport's answer for this thread: `{ ok: true, chat, waitedMs }` or
   * `{ ok: false, reason: "signin" }`. `recover(chat)` handles a refused send (one
   * reconnect, then a new chat). `onReport(record)` gets the turn's report fields when the
   * run ends. `onLate({ text, done })`, when given, gets a late answer to a turn that
   * ended with the no-reply line: the answer so far as plain text, and `done` once it
   * ended. `clock` is a millisecond clock, `timers` the setTimeout family.
   */
  constructor({ rules, ready, recover = async () => ({ ok: false, reason: "unavailable" }), onReport = () => {}, onLate = null, clock, timers, pingMs = PING_MS, startedAt, ...config }) {
    super(config);
    this.startedAt = startedAt;
    this.onLate = onLate;
    this.lateWait = null;
    this.rules = rules;
    this.ready = ready;
    this.recover = recover;
    this.onReport = onReport;
    this.clock = clock || (() => (globalThis.performance ? performance.now() : Date.now()));
    this.timers = timers || globalThis;
    this.pingMs = pingMs;
    this.abortController = new AbortController();
  }

  // As HttpAgent does: the caller's controller is the run's, so the page's stall timer
  // and Retry stop this turn.
  runAgent(parameters, subscriber) {
    this.abortController = parameters?.abortController ?? new AbortController();
    return super.runAgent(parameters, subscriber);
  }

  abortRun() {
    this.abortController.abort();
    super.abortRun();
  }

  /** True while the run's question, which got the no-reply line, awaits a late answer. */
  get awaitingLate() {
    return Boolean(this.lateWait && !this.lateWait.stopped);
  }

  /** The next question was sent (or the page left the thread): a late answer is dropped. */
  cancelLate() {
    if (this.lateWait) this.lateWait.cancel();
  }

  run(input) {
    const signal = this.abortController.signal;
    return new Observable((observer) => {
      const turn = new Turn(this, input, observer, signal);
      turn.begin();
      return () => turn.stop();
    });
  }
}

class Turn {
  constructor(agent, input, observer, signal) {
    this.agent = agent;
    this.rules = agent.rules;
    this.input = input;
    this.observer = observer;
    this.signal = signal;
    this.clock = agent.clock;
    this.timers = agent.timers;
    // The page's clock at the start of the turn, before it waited for the chat, when given.
    this.t0 = Number.isFinite(agent.startedAt) ? agent.startedAt : this.clock();
    this.steps = [];
    this.notes = new Set();
    this.stopped = false;
    this.deadlineTimer = null;
    this.pingTimer = null;
    this.unlisten = null;
    this.assembler = null;
    this.chat = null;
    this.onAbort = () => this.abort();
    this.debug = Boolean(input.forwardedProps && input.forwardedProps.debug);
  }

  ms() {
    return Math.round(this.clock() - this.t0);
  }

  emit(event) {
    if (!this.stopped) this.observer.next(event);
  }

  text(text) {
    const messageId = randomId();
    this.emit({ type: "TEXT_MESSAGE_START", messageId, role: "assistant" });
    this.emit({ type: "TEXT_MESSAGE_CONTENT", messageId, delta: text });
    this.emit({ type: "TEXT_MESSAGE_END", messageId });
  }

  outputs(list) {
    for (const output of list) {
      if (output.type === "text") this.text(output.text);
      else this.emit({ type: "CUSTOM", name: output.name, value: { text: output.text } });
    }
  }

  async begin() {
    const { threadId, runId } = this.input;
    if (this.signal.aborted) return this.abort();
    this.signal.addEventListener("abort", this.onAbort, { once: true });
    this.emit({ type: "RUN_STARTED", threadId, runId });
    const text = lastUserText(this.input.messages);
    if (!text.trim()) {
      this.emit({ type: "RUN_ERROR", message: "no user message", code: "BAD_INPUT" });
      return this.close();
    }
    this.emit({ type: "STEP_STARTED", stepName: STEP_NAME });
    const ready = this.agent.ready || { ok: false, reason: "unavailable" };
    if (ready.waitedMs >= 10) {
      this.steps.push({ name: "waiting for the chat start", start_ms: 0, end_ms: Math.round(ready.waitedMs), lane: "page" });
      this.notes.add(NOTES.waited);
    }
    if (!ready.ok) {
      this.notes.add(NOTES.signin);
      this.text(this.rules.lines.signin);
      return this.finish(null);
    }
    if (text.length > this.rules.maxChars) {
      this.notes.add(NOTES.tooLong);
      this.text(this.rules.lines.tooLong);
      return this.finish(null);
    }
    this.chat = ready.chat;
    // A new question on this chat ends the previous question's wait for a late answer.
    const waiting = lateWaits.get(this.chat);
    if (waiting) waiting.cancel();
    if (this.chat.takeRestartLine()) {
      this.notes.add(NOTES.restarted);
      this.text(this.rules.lines.restarted);
    }
    this.pingTimer = this.timers.setInterval(() => this.emit({ type: "CUSTOM", name: "ping", value: { t: Date.now() } }), this.agent.pingMs);
    try {
      await this.deliver(text);
    } catch (error) {
      if (this.stopped) return undefined;
      return this.fail(isRefused(error) ? "send_refused" : "send_failed");
    }
    return undefined;
  }

  listen() {
    if (this.unlisten) this.unlisten();
    const previousSentAt = this.chat.info ? this.chat.info.lastSentAt : null;
    this.assembler = createTurnAssembler({ rules: this.rules, now: this.clock, previousSentAt });
    this.unlisten = this.chat.listen(
      (item) => this.take(this.assembler.push(item)),
      () => this.fail("socket_failed"),
    );
  }

  async deliver(text) {
    this.listen();
    const sendStart = this.ms();
    let sent;
    try {
      sent = await this.chat.send(text);
    } catch (error) {
      if (!isRefused(error) || this.stopped) throw error;
      // The bridge's deliver order: one fresh connection on the same contact, then a new chat.
      const recovered = await this.agent.recover(this.chat);
      if (this.stopped) return;
      if (!recovered || !recovered.ok) {
        if (recovered && recovered.reason === "signin") {
          this.notes.add(NOTES.signin);
          this.text(this.rules.lines.signin);
          this.finish(null);
          return;
        }
        throw error;
      }
      if (recovered.reconnected) this.notes.add(NOTES.reconnected);
      this.chat = recovered.chat;
      if (this.chat.takeRestartLine()) {
        this.notes.add(NOTES.restarted);
        this.text(this.rules.lines.restarted);
      }
      this.listen();
      sent = await this.chat.send(text);
    }
    if (this.stopped) return;
    this.sent = sent;
    this.steps.push({ name: "SendMessage", start_ms: sendStart, end_ms: this.ms(), lane: "connect" });
    this.take(this.assembler.sent(sent));
  }

  take(outputs) {
    if (this.stopped || !this.assembler) return;
    this.outputs(outputs);
    this.schedule();
  }

  schedule() {
    if (this.deadlineTimer) this.timers.clearTimeout(this.deadlineTimer);
    this.deadlineTimer = null;
    if (this.assembler.done) {
      this.finish(this.assembler.summary());
      return;
    }
    const deadline = this.assembler.deadline();
    const wait = Math.max(0, deadline - this.clock());
    this.deadlineTimer = this.timers.setTimeout(() => {
      this.deadlineTimer = null;
      this.take(this.assembler.tick());
    }, wait);
  }

  timing(summary) {
    const steps = [...this.steps];
    if (summary) {
      if (summary.firstItemMs !== null) {
        steps.push({ name: "first reply item", start_ms: this.relative(summary.firstItemMs), end_ms: null, lane: "connect" });
      }
      if (summary.endMs !== null) {
        steps.push({ name: summary.endName || "end", start_ms: this.relative(summary.endMs), end_ms: null, lane: "connect" });
      }
      if (summary.stale) this.notes.add(NOTES.stale);
      if (summary.reason === "no_reply") this.notes.add(NOTES.noReply);
      if (summary.error === "designer_error") this.notes.add(NOTES.designerError);
      const sent = Date.parse(summary.sentAt);
      const first = Date.parse(summary.firstItemAt);
      const last = Date.parse(summary.lastItemAt);
      if (Number.isFinite(sent) && Number.isFinite(first)) {
        const lastPart = Number.isFinite(last) ? ` and the last ${last - sent} ms` : "";
        this.notes.add(`Connect time: the first reply ${first - sent} ms${lastPart} after the question`);
      }
    }
    const total = this.ms();
    steps.push({ name: "total", start_ms: 0, end_ms: total, lane: "page" });
    const info = this.chat ? this.chat.info : null;
    return {
      steps,
      total_ms: total,
      ids: { contact: info ? info.contactId : "", run: this.input.runId, transport: "connect" },
      notes: [...this.notes],
    };
  }

  // The assembler's marks count from its creation, the send.
  relative(ms) {
    const send = this.steps.find((step) => step.name === "SendMessage");
    return Math.round((send ? send.start_ms : 0) + ms);
  }

  finish(summary) {
    if (this.stopped) return;
    this.clearTimers();
    if (summary && summary.closed && this.chat) {
      this.chat.markClosed();
      // The bridge ends a contact whose conversation closed; chat.ended needs nothing.
      if (summary.reason !== "ended") this.chat.disconnect();
    }
    const timing = this.timing(summary);
    this.emit({ type: "STEP_FINISHED", stepName: STEP_NAME });
    if (this.debug) this.emit({ type: "CUSTOM", name: TIMING_EVENT_NAME, value: timing });
    this.emit({ type: "RUN_FINISHED", threadId: this.input.threadId, runId: this.input.runId });
    this.report(summary ? summary.reason : null, summary ? summary.error : null, summary, timing);
    if (summary && summary.reason === "no_reply" && this.agent.onLate && this.chat && this.sent && !this.chat.closed) {
      this.agent.lateWait = new LateWait(this);
    }
    this.close();
  }

  fail(code) {
    if (this.stopped) return;
    this.clearTimers();
    const summary = this.assembler ? this.assembler.summary() : null;
    const timing = this.timing(summary);
    if (this.debug) this.emit({ type: "CUSTOM", name: TIMING_EVENT_NAME, value: timing });
    this.emit({ type: "RUN_ERROR", message: "The Connect chat failed", code: code.toUpperCase() });
    // "error" is the designer's error line to the report route's alarm (chat_problem
    // designer_error), so a turn the transport gave up on reports "aborted" with its code.
    this.report("aborted", code, summary, timing);
    this.close();
  }

  abort() {
    if (this.stopped) return;
    this.clearTimers();
    const summary = this.assembler ? this.assembler.summary() : null;
    this.report("aborted", null, summary, this.timing(summary));
    this.stopped = true;
    this.observer.error(abortError());
  }

  report(endReason, error, summary, timing) {
    if (!endReason || !this.chat) return;
    try {
      this.agent.onReport({
        contactId: this.chat.contactId,
        runId: this.input.runId,
        threadId: this.input.threadId,
        sentAt: summary ? summary.sentAt : null,
        firstItemAt: summary ? summary.firstItemAt : null,
        lastItemAt: summary ? summary.lastItemAt : null,
        endReason,
        error,
        transport: "connect",
        timing,
      });
    } catch {
      // A report never changes the turn.
    }
  }

  clearTimers() {
    if (this.deadlineTimer) this.timers.clearTimeout(this.deadlineTimer);
    if (this.pingTimer) this.timers.clearInterval(this.pingTimer);
    this.deadlineTimer = null;
    this.pingTimer = null;
    if (this.unlisten) this.unlisten();
    this.unlisten = null;
    this.signal.removeEventListener("abort", this.onAbort);
  }

  close() {
    if (this.stopped) return;
    this.clearTimers();
    this.stopped = true;
    this.observer.complete();
  }

  stop() {
    this.clearTimers();
    this.stopped = true;
  }
}

// The chat's question that awaits a late answer, at most one per chat.
const lateWaits = new WeakMap();

/**
 * One question's late answer after its turn ended with the no-reply line. It reads the
 * chat with a late assembler (the same Id and time checks, no turn limit) until the answer
 * ends, the next question is sent, or the socket fails. The answer goes to the agent's
 * `onLate` as it grows; once it ends, the run gets a second report: the late answer's own
 * end reason with the error code `late_reply`, under the run id with `:late` added, since
 * the report route writes one run line per run id.
 */
class LateWait {
  constructor(turn) {
    this.turn = turn;
    this.agent = turn.agent;
    this.chat = turn.chat;
    this.clock = turn.clock;
    this.timers = turn.timers;
    this.stopped = false;
    this.text = "";
    this.timer = null;
    this.firstMs = null;
    const previous = lateWaits.get(this.chat);
    if (previous) previous.cancel();
    lateWaits.set(this.chat, this);
    this.assembler = createTurnAssembler({ rules: turn.rules, now: this.clock, late: true });
    this.assembler.sent(turn.sent);
    this.unlisten = this.chat.listen(
      (item) => this.take(this.assembler.push(item)),
      () => this.cancel(),
    );
  }

  take(outputs) {
    if (this.stopped) return;
    let grew = false;
    for (const output of outputs) {
      if (output.type !== "text" || !output.text) continue;
      if (this.text && !this.text.endsWith("\n")) this.text += "\n\n";
      this.text += output.text;
      grew = true;
    }
    if (grew && this.firstMs === null) this.firstMs = this.turn.ms();
    if (grew) this.tell(false);
    if (this.timer) this.timers.clearTimeout(this.timer);
    this.timer = null;
    if (this.assembler.done) {
      this.end();
      return;
    }
    const deadline = this.assembler.deadline();
    if (deadline === null) return;
    this.timer = this.timers.setTimeout(() => {
      this.timer = null;
      this.take(this.assembler.tick());
    }, Math.max(0, deadline - this.clock()));
  }

  tell(done) {
    try {
      this.agent.onLate({ text: this.text, done });
    } catch {
      // The page's handler never changes the wait.
    }
  }

  end() {
    const summary = this.assembler.summary();
    this.cancel();
    if (summary.closed) {
      this.chat.markClosed();
      if (summary.reason !== "ended") this.chat.disconnect();
    }
    if (!this.text) return;
    this.tell(true);
    const total = this.turn.ms();
    const notes = [NOTES.late];
    const sent = Date.parse(summary.sentAt);
    const first = Date.parse(summary.firstItemAt);
    if (Number.isFinite(sent) && Number.isFinite(first)) notes.push(`Connect time: the late reply ${first - sent} ms after the question`);
    const info = this.chat.info;
    try {
      this.agent.onReport({
        contactId: this.chat.contactId,
        runId: `${this.turn.input.runId}:late`,
        threadId: this.turn.input.threadId,
        sentAt: summary.sentAt,
        firstItemAt: summary.firstItemAt,
        lastItemAt: summary.lastItemAt,
        endReason: summary.reason,
        error: "late_reply",
        transport: "connect",
        timing: {
          steps: [
            { name: "late reply", start_ms: this.firstMs ?? total, end_ms: null, lane: "connect" },
            { name: "total", start_ms: 0, end_ms: total, lane: "page" },
          ],
          total_ms: total,
          ids: { contact: info ? info.contactId : "", run: this.turn.input.runId, transport: "connect" },
          notes,
        },
      });
    } catch {
      // A report never changes the reply.
    }
  }

  cancel() {
    if (this.stopped) return;
    this.stopped = true;
    if (this.timer) this.timers.clearTimeout(this.timer);
    this.timer = null;
    if (this.unlisten) this.unlisten();
    this.unlisten = null;
    if (lateWaits.get(this.chat) === this) lateWaits.delete(this.chat);
  }
}
