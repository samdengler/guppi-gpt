import { HttpAgent } from "@ag-ui/client";

import { notice } from "./features.js";

(async () => {
  const $ = (id) => document.getElementById(id);

  const newChatBtn = $("new-chat-btn");
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

  // The privacy notice states what the switches in features.json actually allow. The HTML
  // carries the wording for both switches off, so the page reads correctly before this runs.
  const copy = notice();
  $("hint").textContent = copy.hint;
  $("empty-copy").textContent = copy.empty;

  const config = await (await fetch("config.json", { cache: "no-store" })).json();
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

  function newSessionId() {
    // The runtime session id header must be at least 33 characters.
    return `${crypto.randomUUID()}-${crypto.randomUUID()}`;
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
    signinScreen.hidden = true;
    chatScreen.hidden = false;
  }

  function signOut() {
    Object.keys(tokens).forEach((key) => delete tokens[key]);
    tokenExpiresAt = 0;
    auth = "anonymous";
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

  function resetThread() {
    messages = [];
    threadId = crypto.randomUUID();
    status = "idle-empty";
    lastFailedTurn = null;
    threadEl.textContent = "";
    input.value = "";
    autosize();
    accountMenu.hidden = true;
    render();
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

    return { statusLine, text, errorLine, errorText, retryLink };
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
  });

  signOutLink.addEventListener("click", (event) => {
    event.preventDefault();
    signOut();
  });

  googleBtn.addEventListener("click", startSignIn);

  // ---- Send / retry ----

  async function send(text) {
    const userMessage = { id: crypto.randomUUID(), role: "user", content: text };
    messages.push(userMessage);
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
      initialMessages: messageList,
      headers: {
        authorization: `Bearer ${tokens.access_token}`,
        "x-amzn-bedrock-agentcore-runtime-session-id": sessionId,
      },
      fetch: async (url, init) => {
        const response = await fetch(url, init);
        if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
        return response;
      },
    });
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
      await agent.runAgent({ runId: crypto.randomUUID(), abortController: controller }, subscriber);
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
    messages.push({ id: crypto.randomUUID(), role: "assistant", content: draft });
    status = "idle";
    render();
  }

  // ---- Boot ----

  const code = new URLSearchParams(location.search).get("code");
  if (code) {
    try {
      await finishSignIn(code);
      showChat();
      render();
      return;
    } catch (error) {
      // The exchange failed; fall back to the sign-in screen, where it can be retried.
    }
  }

  signinScreen.hidden = false;
})();
