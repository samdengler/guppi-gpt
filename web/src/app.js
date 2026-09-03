import { HttpAgent } from "@ag-ui/client";
import { initFeatures, isEnabled } from "./features.js";
import { enabledFlagNames } from "./flags-core.js";
import * as chatHistory from "./history.js";
import { renderFeedbackControls, FEEDBACK_EVENT } from "./feedback.js";

(async () => {
  const $ = (id) => document.getElementById(id);

  const newChatBtn = $("new-chat-btn");
  const historyWrap = $("history-wrap");
  const historyBtn = $("history-btn");
  const historyPanel = $("history-panel");
  const historyList = $("history-list");
  const historyEmpty = $("history-empty");
  const clearHistoryBtn = $("clear-history-btn");
  const accountWrap = $("account-wrap");
  const accountBtn = $("account-btn");
  const accountMenu = $("account-menu");
  const accountEmail = $("account-email");
  const signOutLink = $("sign-out-link");

  const signinScreen = $("signin-screen");
  const googleBtn = $("google-signin-btn");

  const chatScreen = $("chat-screen");
  const threadWrap = $("thread-wrap");
  const emptyState = $("empty-state");
  const threadEl = $("thread");
  const jumpBtn = $("jump-latest-btn");

  const form = $("composer-form");
  const input = $("composer-input");
  const sendBtn = $("send-btn");
  const emptyCopy = $("empty-copy");
  const composerHint = $("composer-hint");

  const config = await (await fetch("config.json", { cache: "no-store" })).json();
  const flags = await initFeatures(config);
  document.body.dataset.features = enabledFlagNames(flags).join(" ");
  // Read once at load; the flag layer has no live toggling within a page load.
  const historyEnabled = isEnabled("history");
  const feedbackEnabled = isEnabled("feedback");
  if (historyEnabled) {
    emptyCopy.textContent = "Ask anything. Chats are saved on this device only.";
    composerHint.textContent = "Enter to send, Shift+Enter for a new line. Chats are saved on this device only.";
  }
  if (feedbackEnabled) {
    // Keeps the in-memory thread in sync with a vote so a later persistCurrentThread
    // call (the next send) does not overwrite it; the store write itself already
    // happened inside recordFeedback (web/src/feedback.js).
    document.addEventListener(FEEDBACK_EVENT, (event) => {
      const message = messages.find((m) => m.id === event.detail.messageId);
      if (message) message.feedback = event.detail.vote;
    });
  }
  const authBase = `https://${config.authDomain}`;
  const redirectUri = config.siteUrl;

  const tokens = {};        // access_token, id_token, refresh_token; memory only, never persisted
  let tokenExpiresAt = 0;    // epoch ms when access_token expires

  let sessionId = newSessionId();
  let threadId = crypto.randomUUID();
  let messages = [];         // {id, role, content}, the full thread as sent to the agent

  let auth = "anonymous";    // anonymous | signed-in
  let status = "idle-empty"; // idle-empty | idle | running | error
  let userScrolledUp = false;
  let lastFailedTurn = null; // {messageList, refs}, set on error, used by Retry

  // ---- Local history: thread text only, stored in IndexedDB, never the tokens above ----
  let threadCreatedAt = Date.now();
  let threadTitle = null;
  let historyThreads = [];   // cache of the list shown in the history panel

  function titleFor(text) {
    const collapsed = text.replace(/\s+/g, " ").trim();
    if (!collapsed) return "New chat";
    return collapsed.length > 60 ? `${collapsed.slice(0, 59)}…` : collapsed;
  }

  function formatWhen(epochMs) {
    return new Date(epochMs).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  }

  async function persistCurrentThread() {
    if (!historyEnabled) return;
    if (messages.length === 0) return; // an empty thread is not worth a record
    if (!threadTitle) threadTitle = titleFor(messages[0].content);
    const thread = {
      id: threadId,
      title: threadTitle,
      createdAt: threadCreatedAt,
      updatedAt: Date.now(),
      messages: messages.map(({ id, role, content, feedback }) =>
        feedback !== undefined ? { id, role, content, feedback } : { id, role, content },
      ),
    };
    try {
      await chatHistory.putThread(thread);
    } catch (error) {
      // IndexedDB unavailable (private mode, quota, disabled storage); the thread still
      // works for this page load, it just will not resume next time.
    }
  }

  function hydrateThread() {
    threadEl.textContent = "";
    for (let i = 0; i < messages.length; i++) {
      const message = messages[i];
      if (message.role !== "user") continue;
      const refs = addTurn(message.content);
      const next = messages[i + 1];
      if (next && next.role === "assistant") {
        refs.text.textContent = next.content;
        i++;
      }
    }
  }

  function switchToThread(thread) {
    threadId = thread.id;
    threadCreatedAt = thread.createdAt;
    threadTitle = thread.title;
    // A stored feedback field carries through in memory so a later send does not wipe
    // it out of the record on the next persistCurrentThread write, even though the
    // control itself is not redrawn for a resumed reply (see docs/proposals/feedback.md).
    messages = thread.messages.map(({ id, role, content, feedback }) =>
      feedback !== undefined ? { id, role, content, feedback } : { id, role, content },
    );
    status = messages.length > 0 ? "idle" : "idle-empty";
    lastFailedTurn = null;
    hydrateThread();
    historyPanel.hidden = true;
    render();
    scrollToBottom();
  }

  async function renderHistoryList() {
    try {
      historyThreads = await chatHistory.listThreads();
    } catch (error) {
      historyThreads = [];
    }
    historyList.textContent = "";
    historyEmpty.hidden = historyThreads.length > 0;
    for (const thread of historyThreads) {
      const item = document.createElement("li");
      item.className = "history-item";
      if (thread.id === threadId) item.classList.add("active");
      item.dataset.id = thread.id;

      const openBtn = document.createElement("button");
      openBtn.type = "button";
      openBtn.className = "history-item-open";
      const titleSpan = document.createElement("span");
      titleSpan.className = "history-item-title";
      titleSpan.textContent = thread.title || "New chat";
      const dateSpan = document.createElement("span");
      dateSpan.className = "history-item-date";
      dateSpan.textContent = formatWhen(thread.updatedAt);
      openBtn.append(titleSpan, dateSpan);

      const deleteBtn = document.createElement("button");
      deleteBtn.type = "button";
      deleteBtn.className = "history-item-delete";
      deleteBtn.setAttribute("aria-label", "Delete this chat");
      deleteBtn.textContent = "×";

      item.append(openBtn, deleteBtn);
      historyList.appendChild(item);
    }
  }

  async function resumeHistory() {
    if (!historyEnabled) return;
    try {
      const newest = await chatHistory.newestThread();
      if (newest && newest.messages && newest.messages.length > 0) {
        switchToThread(newest);
      }
    } catch (error) {
      // No stored thread, or IndexedDB unavailable; start from the empty state as before.
    }
  }

  function newSessionId() {
    // The runtime session id header must be at least 33 characters.
    return `${crypto.randomUUID()}-${crypto.randomUUID()}`;
  }

  const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

  function newTraceparent() {
    // One W3C trace context per run (traceparent: version, trace id, parent id, flags).
    // The trace id opens with the epoch seconds so it also reads as an X-Ray trace id
    // (1-<8 hex seconds>-<24 hex random>), which is how CloudWatch shows it. The flags
    // byte asks for sampling; the runtime records every span regardless.
    const seconds = Math.floor(Date.now() / 1000).toString(16).padStart(8, "0");
    const traceId = seconds + hex(crypto.getRandomValues(new Uint8Array(12)));
    const parentId = hex(crypto.getRandomValues(new Uint8Array(8)));
    return { traceId, traceparent: `00-${traceId}-${parentId}-01` };
  }

  const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const randomString = () => b64url(crypto.getRandomValues(new Uint8Array(32)));
  const sha256 = async (text) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  const decodeJwt = (jwt) => JSON.parse(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));

  // ---- Sign-in: PKCE authorization code flow against Cognito, identity_provider=Google ----

  async function startSignIn() {
    const verifier = randomString();
    sessionStorage.setItem("pkce_verifier", verifier);
    const params = new URLSearchParams({
      client_id: config.userPoolClientId,
      response_type: "code",
      scope: "openid email profile",
      redirect_uri: redirectUri,
      identity_provider: "Google",
      code_challenge_method: "S256",
      code_challenge: b64url(await sha256(verifier)),
    });
    location.assign(`${authBase}/oauth2/authorize?${params}`);
  }

  async function finishSignIn(code) {
    const verifier = sessionStorage.getItem("pkce_verifier") || "";
    sessionStorage.removeItem("pkce_verifier");
    const body = new URLSearchParams({
      grant_type: "authorization_code",
      client_id: config.userPoolClientId,
      code,
      redirect_uri: redirectUri,
      code_verifier: verifier,
    });
    const response = await fetch(`${authBase}/oauth2/token`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
    if (!response.ok) throw new Error(`token exchange failed: ${response.status}`);
    applyTokens(await response.json());
    history.replaceState(null, "", location.pathname);
  }

  function applyTokens(payload) {
    Object.assign(tokens, payload);
    tokenExpiresAt = Date.now() + (payload.expires_in || 3600) * 1000;
  }

  async function refreshTokenIfNeeded() {
    const fiveMinutes = 5 * 60 * 1000;
    if (Date.now() < tokenExpiresAt - fiveMinutes) return;
    if (!tokens.refresh_token) return;
    const body = new URLSearchParams({
      grant_type: "refresh_token",
      client_id: config.userPoolClientId,
      refresh_token: tokens.refresh_token,
    });
    const response = await fetch(`${authBase}/oauth2/token`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
    // A failed refresh leaves the old token in place; the send below will fail
    // on its own and land in the error state with Retry.
    if (!response.ok) return;
    applyTokens(await response.json());
  }

  function accountInitials(claims) {
    const given = (claims.given_name || "").trim();
    const family = (claims.family_name || "").trim();
    if (given || family) return `${given.slice(0, 1)}${family.slice(0, 1)}`.toUpperCase();
    // Cognito maps the Google name to the name claim; take the first and last words.
    const words = (claims.name || "").trim().split(/\s+/).filter(Boolean);
    if (words.length) return `${words[0].slice(0, 1)}${words.length > 1 ? words[words.length - 1].slice(0, 1) : ""}`.toUpperCase();
    return (claims.email || "").slice(0, 2).toUpperCase();
  }

  function showChat() {
    const claims = decodeJwt(tokens.id_token);
    auth = "signed-in";
    accountBtn.textContent = accountInitials(claims);
    accountEmail.textContent = claims.email || claims.sub;
    accountWrap.hidden = false;
    newChatBtn.hidden = false;
    historyWrap.hidden = !historyEnabled;
    signinScreen.hidden = true;
    chatScreen.hidden = false;
  }

  async function signOut() {
    Object.keys(tokens).forEach((key) => delete tokens[key]);
    tokenExpiresAt = 0;
    auth = "anonymous";
    // The page stores nothing tied to the account, but a shared machine is the risk a
    // saved thread creates, so signing out clears every stored thread with it.
    try {
      await chatHistory.clearAll();
    } catch (error) {
      // Storage was unavailable to begin with; there is nothing to clear.
    }
    const params = new URLSearchParams({
      client_id: config.userPoolClientId,
      logout_uri: config.siteUrl,
    });
    location.assign(`${authBase}/logout?${params}`);
  }

  // ---- Render / state ----

  function render() {
    const canSend = (status === "idle-empty" || status === "idle") && input.value.trim().length > 0;
    sendBtn.disabled = !canSend;
    emptyState.hidden = messages.length > 0 || status === "running" || status === "error";
    input.placeholder = messages.length > 0 ? "Reply to GuppiGPT" : "Ask GuppiGPT";
  }

  function clearThreadState() {
    messages = [];
    threadId = crypto.randomUUID();
    threadCreatedAt = Date.now();
    threadTitle = null;
    status = "idle-empty";
    lastFailedTurn = null;
    threadEl.textContent = "";
    input.value = "";
    autosize();
    render();
  }

  function resetThread() {
    clearThreadState();
    accountMenu.hidden = true;
    if (historyEnabled) historyPanel.hidden = true;
  }

  // ---- Thread rendering ----

  function addTurn(userText) {
    const turn = document.createElement("div");
    turn.className = "turn";

    const userDiv = document.createElement("div");
    userDiv.className = "msg-user";
    userDiv.textContent = userText;
    turn.appendChild(userDiv);

    const reply = document.createElement("div");
    reply.className = "reply";

    const label = document.createElement("p");
    label.className = "reply-label";
    label.textContent = "GuppiGPT";
    reply.appendChild(label);

    const statusLine = document.createElement("p");
    statusLine.className = "reply-status";
    statusLine.hidden = true;
    reply.appendChild(statusLine);

    const text = document.createElement("div");
    text.className = "reply-text";
    reply.appendChild(text);

    const errorLine = document.createElement("p");
    errorLine.className = "reply-error";
    errorLine.hidden = true;
    const retryLink = document.createElement("a");
    retryLink.href = "#";
    retryLink.textContent = "Retry";
    const errorText = document.createElement("span");
    errorText.textContent = "The reply was interrupted.";
    errorLine.append(errorText, " ", retryLink);
    reply.appendChild(errorLine);

    turn.appendChild(reply);
    threadEl.appendChild(turn);

    retryLink.addEventListener("click", (event) => {
      event.preventDefault();
      retry();
    });

    return { reply, statusLine, text, errorLine, errorText, retryLink };
  }

  function markReply(reply, ids) {
    // Support identifiers on the reply element, invisible on the page: the run id and
    // the trace id the page minted, and the request id the gateway answered with. A
    // reader picks them up from the element's data attributes in the browser inspector
    // and searches CloudWatch by either id (docs/proposals/traceability.md).
    reply.dataset.runId = ids.runId;
    reply.dataset.traceId = ids.traceId;
    if (ids.requestId) reply.dataset.requestId = ids.requestId;
    else delete reply.dataset.requestId;
  }

  // ---- Auto-scroll ----

  function isNearBottom() {
    return threadWrap.scrollHeight - threadWrap.scrollTop - threadWrap.clientHeight < 40;
  }

  function scrollToBottom() {
    threadWrap.scrollTop = threadWrap.scrollHeight;
    userScrolledUp = false;
    jumpBtn.hidden = true;
  }

  function scrollIfFollowing() {
    if (!userScrolledUp) scrollToBottom();
  }

  threadWrap.addEventListener("scroll", () => {
    userScrolledUp = !isNearBottom();
    jumpBtn.hidden = !userScrolledUp;
  });

  jumpBtn.addEventListener("click", scrollToBottom);

  // ---- Composer ----

  function autosize() {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }

  input.addEventListener("input", () => {
    autosize();
    render();
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || sendBtn.disabled) return;
    input.value = "";
    autosize();
    send(text);
  });

  newChatBtn.addEventListener("click", resetThread);

  accountBtn.addEventListener("click", () => {
    const open = accountMenu.hidden;
    accountMenu.hidden = !open;
    accountBtn.setAttribute("aria-expanded", String(open));
  });

  document.addEventListener("click", (event) => {
    if (!accountWrap.contains(event.target)) {
      accountMenu.hidden = true;
      accountBtn.setAttribute("aria-expanded", "false");
    }
    if (historyEnabled && !historyWrap.contains(event.target)) {
      historyPanel.hidden = true;
      historyBtn.setAttribute("aria-expanded", "false");
    }
  });

  signOutLink.addEventListener("click", (event) => {
    event.preventDefault();
    signOut();
  });

  if (historyEnabled) {
    historyBtn.addEventListener("click", async () => {
      const open = historyPanel.hidden;
      historyPanel.hidden = !open;
      historyBtn.setAttribute("aria-expanded", String(open));
      if (open) await renderHistoryList();
    });

    historyList.addEventListener("click", async (event) => {
      const item = event.target.closest(".history-item");
      if (!item) return;
      const id = item.dataset.id;
      if (event.target.closest(".history-item-delete")) {
        try {
          await chatHistory.deleteThread(id);
        } catch (error) {
          // Nothing to remove; the panel refresh below reflects whatever remains.
        }
        if (id === threadId) clearThreadState();
        await renderHistoryList();
        return;
      }
      const thread = historyThreads.find((t) => t.id === id);
      if (thread) switchToThread(thread);
    });

    clearHistoryBtn.addEventListener("click", async () => {
      try {
        await chatHistory.clearAll();
      } catch (error) {
        // Nothing to clear.
      }
      clearThreadState();
      await renderHistoryList();
    });
  }

  googleBtn.addEventListener("click", startSignIn);

  // ---- Send / retry ----

  async function send(text) {
    const userMessage = { id: crypto.randomUUID(), role: "user", content: text };
    messages.push(userMessage);
    await persistCurrentThread();
    const refs = addTurn(text);
    scrollIfFollowing();
    await runTurn(messages.slice(), refs);
  }

  async function retry() {
    if (!lastFailedTurn) return;
    const { messageList, refs } = lastFailedTurn;
    lastFailedTurn = null;
    sessionId = newSessionId();
    refs.errorLine.hidden = true;
    refs.statusLine.hidden = true;
    refs.text.textContent = "";
    await runTurn(messageList, refs);
  }

  // ---- Stream handling: @ag-ui/client's HttpAgent reads the SSE stream ----
  async function runTurn(messageList, refs) {
    status = "running";
    render();
    await refreshTokenIfNeeded();
    const controller = new AbortController();
    let draft = "";
    let paintScheduled = false;
    let stallTimer = null;
    let finished = false;
    let errored = false;
    let refused = false;
    // One run id and one trace per turn; a Retry is a new run on a new trace.
    const runId = crypto.randomUUID();
    const { traceId, traceparent } = newTraceparent();
    let requestId = "";
    const resetStallTimer = () => {
      if (stallTimer) clearTimeout(stallTimer);
      stallTimer = setTimeout(() => controller.abort(), 30000);
    };
    const schedulePaint = () => {
      if (paintScheduled) return;
      paintScheduled = true;
      requestAnimationFrame(() => {
        paintScheduled = false;
        refs.text.textContent = draft;
        scrollIfFollowing();
      });
    };
    const setStatusLine = (line) => {
      refs.statusLine.textContent = line;
      refs.statusLine.hidden = false;
    };
    const showError = (refused) => {
      status = "error";
      // A 403 comes from the gateway's front door, which rejects any body containing a
      // localhost or loopback URL; resending the same thread cannot succeed.
      lastFailedTurn = refused ? null : { messageList, refs };
      refs.errorText.textContent = refused
        ? "The gateway refused this message. Messages that contain a localhost or loopback address are rejected; start a new chat."
        : "The reply was interrupted.";
      refs.retryLink.hidden = Boolean(refused);
      refs.errorLine.hidden = false;
      render();
    };

    // One agent per turn: the page owns the thread and resends it whole, so nothing is
    // kept on the client object between turns. The custom fetch turns a non-2xx answer
    // into a failure, which the client would otherwise read as an empty stream.
    const agent = new HttpAgent({
      url: "/api/invocations",
      threadId,
      // Strip any bookkeeping field (feedback included) that does not belong on the
      // wire; the agent's validation only expects id, role, and content per message.
      initialMessages: messageList.map(({ id, role, content }) => ({ id, role, content })),
      headers: {
        authorization: `Bearer ${tokens.access_token}`,
        "x-amzn-bedrock-agentcore-runtime-session-id": sessionId,
        traceparent,
      },
      fetch: async (url, init) => {
        const response = await fetch(url, init);
        // The gateway's request id, when the response carries one; the page is
        // same-origin with the API, so the header is readable without CORS exposure.
        requestId = response.headers.get("x-amzn-requestid") || "";
        markReply(refs.reply, { runId, traceId, requestId });
        if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
        return response;
      },
    });
    markReply(refs.reply, { runId, traceId, requestId });
    const subscriber = {
      onEvent: () => {
        resetStallTimer(); // every event counts, the CUSTOM ping included
      },
      onToolCallStartEvent: () => {
        setStatusLine("Searching the knowledge base\u2026");
      },
      onToolCallEndEvent: () => {
        setStatusLine("Searched the knowledge base");
      },
      onTextMessageStartEvent: () => {
        // A second message in one run (text around a tool call) starts a new paragraph.
        if (draft && !draft.endsWith("\n")) draft += "\n\n";
      },
      onTextMessageContentEvent: ({ event }) => {
        draft += event.delta || "";
        schedulePaint();
      },
      onRunFinishedEvent: () => {
        finished = true;
      },
      onRunErrorEvent: () => {
        errored = true;
      },
    };

    try {
      resetStallTimer();
      await agent.runAgent({ runId, abortController: controller }, subscriber);
    } catch (error) {
      errored = true; // transport failure, a stall abort, or an event the client refused
      refused = error && error.status === 403;
    } finally {
      if (stallTimer) clearTimeout(stallTimer);
      input.focus();
    }
    if (errored || !finished) {
      showError(refused);
      return;
    }
    refs.text.textContent = draft;
    const assistantMessage = { id: crypto.randomUUID(), role: "assistant", content: draft };
    messages.push(assistantMessage);
    await persistCurrentThread();
    // Only a committed reply gets the control: never the streaming draft above, and
    // never an interrupted one, since that path returns from showError() above instead.
    if (feedbackEnabled) {
      renderFeedbackControls(refs.reply, { threadId, messageId: assistantMessage.id });
    }
    status = "idle";
    render();
  }

  // ---- Boot ----

  const code = new URLSearchParams(location.search).get("code");
  if (code) {
    try {
      await finishSignIn(code);
      showChat();
      await resumeHistory();
      render();
      return;
    } catch (error) {
      // The exchange failed; fall back to the sign-in screen, where it can be retried.
    }
  }

  signinScreen.hidden = false;
})();
