import { HttpAgent } from "@ag-ui/client";
import { initFeatures, isEnabled } from "./features.js";
import { enabledFlagNames } from "./flags-core.js";
import * as chatHistory from "./history.js";
import { renderFeedbackControls, initFeedbackSink, FEEDBACK_EVENT } from "./feedback.js";
import { hintText, emptyStateText, toolStatus } from "./copy.js";
import { initRum, identifyRumUser } from "./rum.js";
import { saveSession, loadSession, clearSession, classifyRefreshFailure, decideOnLoad, newestRefreshToken } from "./session.js";
import { createExtensionHost, EXTENSION_EVENT_TYPES } from "./extensions.js";
import { inviteBody, inviteResult, signInRefusal } from "./invite-core.js";
import { oidcEndpoints, logoutUrl } from "./oidc.js";
import { resolveProject as projectFromPath, projectPath, acceptedReturnPath, manifestUrl, checkManifest, mergeFeatures, brandFor, agentUrlFor, suggestionsFor, themeFor, wantsWarmStart, warmRunInput, THEME_KEYS, PROJECTS_URL, projectNames, projectCard, switcherEntries } from "./project.js";

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

  const brandEl = $("brand");
  const switcherWrap = $("switcher");
  const switcherMenu = $("switcher-menu");
  const switcherList = $("switcher-list");
  const signinTitle = $("signin-title");
  const signinScreen = $("signin-screen");
  const googleBtn = $("google-signin-btn");
  const signinNotice = $("signin-notice");
  const inviteForm = $("invite-form");
  const inviteError = $("invite-error");
  const inviteBtn = $("invite-btn");
  const inviteSent = $("invite-sent");
  const inviteSentCopy = $("invite-sent-copy");

  const chatScreen = $("chat-screen");
  const threadWrap = $("thread-wrap");
  const emptyState = $("empty-state");
  const threadEl = $("thread");
  const jumpBtn = $("jump-latest-btn");

  const form = $("composer-form");
  const input = $("composer-input");
  const sendBtn = $("send-btn");
  const emptyCopy = $("empty-copy");
  const suggestionsEl = $("suggestions");
  const projectCardsEl = $("project-cards");
  const projectCardsList = $("project-cards-list");
  const composerHint = $("composer-hint");

  // ---- Project: /p/<name>/ selects a project by its manifest; / is the default ----

  // The Cognito redirect URI is the root, so a sign-in started on /p/<name>/ comes back
  // to / with the code and the path in `state`; that page load is already the project's.
  function resolveProject() {
    const params = new URLSearchParams(location.search);
    if (params.has("code")) return projectFromPath(acceptedReturnPath(params.get("state")));
    return projectFromPath(location.pathname);
  }

  // A missing or unusable manifest falls back to the default project with one warning,
  // so a project that has not published yet still gets a working page.
  async function loadManifest(name) {
    if (!name) return null;
    try {
      const response = await fetch(manifestUrl(name), { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const manifest = checkManifest(await response.json(), name);
      if (!manifest) throw new Error("not a usable manifest for this project");
      return manifest;
    } catch (error) {
      console.warn(`guppigpt: no manifest for project "${name}" (${error.message}); using the default project`);
      return null;
    }
  }

  // A project's colors (manifest.theme, web/src/project.js): custom properties set on the
  // root element for the scheme in effect, swapped when the scheme changes. The default
  // project sets none, so the stylesheet's own values stand.
  function applyTheme(theme) {
    if (!theme) return;
    const root = document.documentElement;
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      for (const property of Object.values(THEME_KEYS)) root.style.removeProperty(property);
      const values = scheme.matches ? theme.dark : theme.light;
      for (const [property, value] of Object.entries(values)) root.style.setProperty(property, value);
    };
    apply();
    scheme.addEventListener("change", apply);
  }

  function applyBrand() {
    document.title = brand.label;
    brandEl.textContent = brand.label;
    signinTitle.textContent = brand.label;
    input.placeholder = `Ask ${brand.assistant}`;
  }

  const project = resolveProject();
  const [manifest, baseConfig] = await Promise.all([
    loadManifest(project),
    // Absolute, so a project page under /p/<name>/ reads the same file.
    fetch("/config.json", { cache: "no-store" }).then((response) => response.json()),
  ]);
  const config = mergeFeatures(baseConfig, manifest);
  const brand = brandFor(manifest);
  const agentUrl = agentUrlFor(manifest);
  applyBrand();
  applyTheme(themeFor(manifest));
  const flags = await initFeatures(config);
  document.body.dataset.features = enabledFlagNames(flags).join(" ");
  // Registers the OpenFeature hook (when the rum flag and config.rum.scriptPath are
  // both set) before the isEnabled calls below, so it is in place for the flag
  // evaluations those calls trigger. A no-op otherwise: no script element, no listener,
  // no behavior change (docs/proposals/dynatrace.md).
  initRum(flags, config);
  // Read once at load; the flag layer has no live toggling within a page load.
  const historyEnabled = isEnabled("history");
  const feedbackEnabled = isEnabled("feedback");
  const loggingEnabled = isEnabled("logging");
  // The privacy notice states what the switches actually allow.
  emptyCopy.textContent = emptyStateText(historyEnabled, loggingEnabled);
  // A project's suggested prompts sit under the empty state copy and go with it: one
  // click sends the prompt as if typed, and the empty state (pills included) hides once
  // the thread has a message. Plain text only; the labels never become markup.
  const suggestions = suggestionsFor(manifest);
  suggestionsEl.hidden = suggestions.length === 0;
  for (const suggestion of suggestions) {
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "suggestion";
    pill.textContent = suggestion.label;
    // Pressing a pill starts the warm start a moment before its click sends.
    pill.addEventListener("pointerdown", () => engage());
    pill.addEventListener("click", () => {
      if (status !== "idle-empty" && status !== "idle") return;
      input.value = "";
      autosize();
      send(suggestion.prompt);
    });
    suggestionsEl.appendChild(pill);
  }
  // The home page lists the projects under its empty state ("Try a project"), so they go
  // with it once the thread has a message. Each card's text comes from the project's own
  // manifest; a project whose manifest is missing or unusable is left out, and any
  // failure leaves the section hidden. Plain text only.
  // One fetch of the list and the manifests serves the cards and the header's switcher.
  async function loadProjects() {
    try {
      const listResponse = await fetch(PROJECTS_URL, { cache: "no-store" });
      if (!listResponse.ok) return [];
      const names = projectNames(await listResponse.json());
      const cards = await Promise.all(
        names.map(async (name) => {
          try {
            const response = await fetch(manifestUrl(name), { cache: "no-store" });
            return response.ok ? projectCard(await response.json(), name) : null;
          } catch {
            return null;
          }
        }),
      );
      return cards.filter(Boolean);
    } catch {
      return [];
    }
  }

  function renderProjectCards(cards) {
    for (const card of cards) {
      const link = document.createElement("a");
      link.className = "project-card";
      link.href = card.href;
      const label = document.createElement("span");
      label.className = "project-card-label";
      label.textContent = card.label;
      const description = document.createElement("span");
      description.className = "project-card-description";
      description.textContent = card.description;
      const open = document.createElement("span");
      open.className = "project-card-open";
      open.textContent = `Open ${card.href}`;
      link.append(label, description, open);
      projectCardsList.appendChild(link);
    }
    projectCardsEl.hidden = projectCardsList.childElementCount === 0;
  }

  // The header's project switcher: the brand opens a menu of every project, GuppiGPT
  // first, the current one highlighted. A failed load still lists GuppiGPT.
  function renderSwitcher(cards) {
    switcherList.replaceChildren();
    for (const entry of switcherEntries(cards, project)) {
      const item = document.createElement("a");
      item.className = "switcher-item";
      item.href = entry.href;
      item.setAttribute("role", "menuitem");
      if (entry.current) item.setAttribute("aria-current", "page");
      const text = document.createElement("span");
      text.className = "switcher-text";
      const label = document.createElement("span");
      label.className = "switcher-label";
      label.textContent = entry.label;
      text.append(label);
      if (entry.description) {
        const description = document.createElement("span");
        description.className = "switcher-description";
        description.textContent = entry.description;
        text.append(description);
      }
      const path = document.createElement("span");
      path.className = "switcher-path";
      path.textContent = entry.href;
      item.append(text, path);
      switcherList.appendChild(item);
    }
  }

  const projectsLoaded = loadProjects();
  projectsLoaded.then((cards) => {
    if (!project) renderProjectCards(cards);
    renderSwitcher(cards);
  });
  composerHint.textContent = hintText(historyEnabled, loggingEnabled);
  if (feedbackEnabled) {
    // Keeps the in-memory thread in sync with a vote so a later persistCurrentThread
    // call (the next send) does not overwrite it; the store write itself already
    // happened inside recordFeedback (web/src/feedback.js).
    document.addEventListener(FEEDBACK_EVENT, (event) => {
      const message = messages.find((m) => m.id === event.detail.messageId);
      if (message) message.feedback = event.detail.vote;
    });
  }
  // Cognito's hosted UI, or a standard OIDC issuer (Okta) when config.json has `oidc`.
  const signin = oidcEndpoints(config);
  const redirectUri = config.siteUrl;

  const tokens = {};        // access_token, id_token, refresh_token; access/id token: memory only
  let tokenExpiresAt = 0;    // epoch ms when access_token expires

  // The `guppi` object a project's extension module receives (web/src/extensions.js).
  const extensions = createExtensionHost({ project: manifest, getToken: () => tokens.access_token });

  // A project's own module, same origin (the CSP is script-src 'self'). esbuild leaves a
  // dynamic import of a runtime URL native in this IIFE bundle. A module that fails to
  // load or throws leaves the page on its built-in behavior.
  async function installExtension(url) {
    try {
      const module = await import(url);
      if (typeof module.default !== "function") throw new Error("no default export function");
      await module.default(extensions.guppi);
    } catch (error) {
      console.warn(`guppigpt: extension ${url} did not install; continuing without it`, error);
    }
  }

  if (feedbackEnabled) {
    // The second subscriber to the same event: a vote also goes to the feedback API on
    // this origin, with the same bearer runTurn sends. Fire and forget, so a vote never
    // delays or breaks the page (docs/proposals/feedback.md).
    initFeedbackSink(() => tokens.access_token);
  }

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
    extensions.notifyThread(threadId);
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

  // ---- Sign-in: PKCE authorization code flow against the issuer in config.json ----

  async function startSignIn() {
    const verifier = randomString();
    sessionStorage.setItem("pkce_verifier", verifier);
    const params = new URLSearchParams({
      client_id: signin.clientId,
      response_type: "code",
      scope: signin.scope,
      redirect_uri: redirectUri,
      ...signin.authorizeExtra,
      code_challenge_method: "S256",
      code_challenge: b64url(await sha256(verifier)),
      // Where to come back to; finishSignIn accepts only / and /p/<name>/ from it.
      state: projectPath(project),
    });
    location.assign(`${signin.authorize}?${params}`);
  }

  async function finishSignIn(code) {
    const verifier = sessionStorage.getItem("pkce_verifier") || "";
    sessionStorage.removeItem("pkce_verifier");
    const body = new URLSearchParams({
      grant_type: "authorization_code",
      client_id: signin.clientId,
      code,
      redirect_uri: redirectUri,
      code_verifier: verifier,
    });
    const response = await fetch(signin.token, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
    if (!response.ok) throw new Error(`token exchange failed: ${response.status}`);
    applyTokens(await response.json());
    await persistSession();
    history.replaceState(null, "", acceptedReturnPath(new URLSearchParams(location.search).get("state")));
  }

  function applyTokens(payload) {
    Object.assign(tokens, payload);
    tokenExpiresAt = Date.now() + (payload.expires_in || 3600) * 1000;
  }

  // Saves the refresh token now in `tokens` plus the header claims from the current id
  // token. Cognito issues a new refresh_token on every rotated use; when a response omits
  // one, applyTokens leaves the previous value in `tokens.refresh_token` in place, which is
  // what ends up saved here, so the old token is kept only when no new one arrived.
  async function persistSession() {
    if (!tokens.refresh_token || !tokens.id_token) return;
    await saveSession(tokens.refresh_token, decodeJwt(tokens.id_token));
  }

  // One refresh at a time: the refresh token rotates on every use, so a warm start and a
  // send that both refresh would spend it twice and the second would be refused.
  let refreshing = null;
  function refreshTokenIfNeeded(minimumLeft = 5 * 60 * 1000) {
    if (Date.now() < tokenExpiresAt - minimumLeft) return Promise.resolve();
    if (!refreshing) refreshing = refreshTokens().finally(() => { refreshing = null; });
    return refreshing;
  }

  async function refreshTokens() {
    // Cognito rotates the refresh token on every use, and another tab of the same browser
    // may have used it since this tab last did (each tab keeps its own copy in memory). The
    // stored record always holds the newest one, so it wins over this tab's copy; a stale
    // copy would be refused with invalid_grant and the send after it would fail.
    tokens.refresh_token = newestRefreshToken(tokens.refresh_token, await loadSession());
    if (!tokens.refresh_token) return;
    const body = new URLSearchParams({
      grant_type: "refresh_token",
      client_id: signin.clientId,
      refresh_token: tokens.refresh_token,
    });
    // A failed refresh, refused or offline, leaves the old token in place; the send below
    // fails on its own and lands in the error state with Retry. Nothing escapes from here,
    // since the warm start does not handle a rejection.
    let response;
    try {
      response = await fetch(signin.token, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body,
      });
    } catch {
      return;
    }
    if (!response.ok) return;
    applyTokens(await response.json());
    await persistSession();
  }

  // Attempts a silent refresh against a stored session on startup. Never throws: a network
  // failure and an OAuth error both come back as a classified result for decideOnLoad.
  async function silentRefresh(session) {
    const body = new URLSearchParams({
      grant_type: "refresh_token",
      client_id: signin.clientId,
      refresh_token: session.refreshToken,
    });
    let response;
    try {
      response = await fetch(signin.token, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body,
      });
    } catch {
      return { ok: false, kind: classifyRefreshFailure({ networkError: true }) };
    }
    if (!response.ok) {
      let errorBody = null;
      try {
        errorBody = await response.json();
      } catch {
        // Not a JSON body; classifyRefreshFailure treats that conservatively as a network error.
      }
      return { ok: false, kind: classifyRefreshFailure({ status: response.status, body: errorBody }) };
    }
    applyTokens(await response.json());
    await persistSession();
    return { ok: true };
  }

  function accountInitials(claims) {
    const given = (claims.given_name || "").trim();
    const family = (claims.family_name || "").trim();
    if (given || family) return `${given.slice(0, 1)}${family.slice(0, 1)}`.toUpperCase();
    // An issuer may give only the full name claim; take the first and last words.
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
    // A no-op unless RUM is active and config.rum.identifyUser asks for it; the subject
    // is hashed before it reaches dtrum (web/src/rum.js).
    identifyRumUser(config, claims.sub);
  }

  async function signOut() {
    // An OIDC issuer's logout takes the id token as a hint, so it is kept until the URL is built.
    const idToken = tokens.id_token;
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
    await clearSession();
    location.assign(logoutUrl(signin, { siteUrl: config.siteUrl, idToken }));
  }

  // ---- Render / state ----

  function render() {
    const canSend = (status === "idle-empty" || status === "idle") && input.value.trim().length > 0;
    sendBtn.disabled = !canSend;
    emptyState.hidden = messages.length > 0 || status === "running" || status === "error";
    input.placeholder = messages.length > 0 ? `Reply to ${brand.assistant}` : `Ask ${brand.assistant}`;
  }

  function clearThreadState() {
    messages = [];
    threadId = crypto.randomUUID();
    extensions.notifyThread(threadId);
    armWarmStart();
    threadCreatedAt = Date.now();
    threadTitle = null;
    status = "idle-empty";
    lastFailedTurn = null;
    threadEl.textContent = "";
    input.value = "";
    autosize();
    render();
  }

  // A project whose manifest lists `warm-start` gets a run with no messages once the
  // employee engages with a new thread (focus on the composer, a first keystroke, or a
  // suggestion), on the runtime session its runs use, so the agent is running and has
  // opened what the first message needs by the time it is sent (docs/proposals/platform.md,
  // "Warm start"). Engagement rather than page load, so a reader who never writes opens
  // nothing (guppi-hr critique findings 1 and 2). The run names the thread the page left,
  // when that one may hold something open, so the agent can release it. Fire and forget: a
  // failed warm start only means the first message does that work itself. The token is
  // refreshed first unless it has 50 minutes left: an agent may start something that lasts
  // an hour on it (guppi-hr's Connect contact, whose hop tokens expire with this token, D47).
  const warmStart = wantsWarmStart(manifest);
  let warmArmed = false;     // the current thread has not been warmed yet
  let warmedThreadId = null; // the last thread a warm start went out for
  let leftThreadId = null;   // a thread the page left that may still hold something open
  function armWarmStart() {
    warmArmed = warmStart;
  }
  function engage() {
    if (!warmArmed || auth !== "signed-in" || messages.length > 0) return;
    warmArmed = false;
    warmThread();
  }
  async function warmThread() {
    if (!warmStart || auth !== "signed-in" || messages.length > 0) return;
    await refreshTokenIfNeeded(50 * 60 * 1000);
    if (Date.now() >= tokenExpiresAt - 5 * 60 * 1000) return;
    warmedThreadId = threadId;
    const previousThreadId = leftThreadId;
    leftThreadId = null;
    try {
      const response = await fetch(agentUrl, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          accept: "text/event-stream",
          authorization: `Bearer ${tokens.access_token}`,
          "x-amzn-bedrock-agentcore-runtime-session-id": sessionId,
          traceparent: newTraceparent().traceparent,
        },
        body: JSON.stringify(warmRunInput({ threadId, runId: crypto.randomUUID(), manifest, previousThreadId })),
      });
      await response.text();
    } catch (error) {
      console.warn("guppigpt: warm start failed", error);
    }
  }

  function resetThread() {
    if (warmedThreadId === threadId || messages.length > 0) leftThreadId = threadId;
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
    label.textContent = brand.assistant;
    reply.appendChild(label);

    const statusLine = document.createElement("p");
    statusLine.className = "reply-status";
    statusLine.hidden = true;
    reply.appendChild(statusLine);

    const text = document.createElement("div");
    text.className = "reply-text";
    reply.appendChild(text);

    // Where extension renderers draw for this reply; the page never writes here itself.
    const attachments = document.createElement("div");
    attachments.className = "reply-attachments";
    reply.appendChild(attachments);

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

    return { reply, label, statusLine, text, attachments, errorLine, errorText, retryLink };
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

  input.addEventListener("focus", () => engage());
  input.addEventListener("input", () => {
    engage();
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

  function setSwitcher(open) {
    switcherMenu.hidden = !open;
    brandEl.setAttribute("aria-expanded", String(open));
  }
  brandEl.addEventListener("click", () => {
    const open = switcherMenu.hidden;
    setSwitcher(open);
    if (open) switcherList.querySelector('[aria-current="page"], .switcher-item')?.focus();
  });
  switcherMenu.addEventListener("keydown", (event) => {
    const items = [...switcherList.querySelectorAll(".switcher-item")];
    const at = items.indexOf(document.activeElement);
    if (event.key === "Escape") {
      setSwitcher(false);
      brandEl.focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      items[(at + step + items.length) % items.length]?.focus();
    }
  });

  accountBtn.addEventListener("click", () => {
    const open = accountMenu.hidden;
    accountMenu.hidden = !open;
    accountBtn.setAttribute("aria-expanded", String(open));
  });

  document.addEventListener("click", (event) => {
    if (!switcherWrap.contains(event.target)) setSwitcher(false);
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

  // ---- Invite requests (docs/proposals/invites.md) ----
  // A visitor without access asks for an invite; the request goes to /api/invite, which
  // mails Sam. Every value shown back is set with textContent.
  inviteForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(inviteForm));
    const checked = inviteBody(values);
    if (!checked.ok) {
      inviteError.textContent = checked.error;
      inviteError.hidden = false;
      return;
    }
    inviteError.hidden = true;
    inviteBtn.disabled = true;
    let status = 0;
    try {
      const response = await fetch("/api/invite", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(checked.body),
      });
      status = response.status;
    } catch {
      status = 0;
    }
    const result = inviteResult(status);
    inviteBtn.disabled = false;
    if (!result.ok) {
      inviteError.textContent = result.text;
      inviteError.hidden = false;
      return;
    }
    inviteSentCopy.textContent =
      `Thanks, ${checked.body.name}. Sam will look at your request and email ` +
      `${checked.body.email} when you're in.`;
    inviteForm.hidden = true;
    inviteSent.hidden = false;
  });

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
    refs.attachments.textContent = "";
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
    extensions.setStatusSink(setStatusLine);
    // Tool calls of this run by id, for the renderer an extension registered by name.
    const toolCalls = new Map();
    // Tool calls whose start or end event an extension renderer took; the page then
    // leaves their status line to the extension.
    const statusClaimed = new Set();
    // setLabel replaces the running reply's label, "Guppi" by default (for example an
    // agent name per delegation); the page's history keeps text only, not the label.
    const renderContext = (event) => ({
      event,
      runId,
      threadId,
      setLabel: (text) => {
        refs.label.textContent = String(text);
      },
    });
    const showError = (refused, empty = false) => {
      status = "error";
      // A 403 comes from the gateway's front door, which rejects any body containing a
      // localhost or loopback URL; resending the same thread cannot succeed.
      lastFailedTurn = refused ? null : { messageList, refs };
      refs.errorText.textContent = refused
        ? "The gateway refused this message. Messages that contain a localhost or loopback address are rejected; start a new chat."
        : empty
          ? "No answer came back."
          : "The reply was interrupted.";
      refs.retryLink.hidden = Boolean(refused);
      refs.errorLine.hidden = false;
      render();
    };

    // Extension onSend hooks may add forwardedProps or state; the thread, the run id
    // and the messages are the page's and are not read back from them.
    const runInput = extensions.applySendHooks({
      threadId,
      runId,
      // Strip any bookkeeping field (feedback included) that does not belong on the
      // wire; the agent's validation only expects id, role, and content per message.
      messages: messageList.map(({ id, role, content }) => ({ id, role, content })),
      // A project page names its project, which gives the platform agent that project's
      // gateway tools beside the knowledge base search (agent/src/guppi_agent/agent.py).
      forwardedProps: manifest ? { project: manifest.name } : {},
      state: {},
    });

    // One agent per turn: the page owns the thread and resends it whole, so nothing is
    // kept on the client object between turns. The custom fetch turns a non-2xx answer
    // into a failure, which the client would otherwise read as an empty stream.
    const agent = new HttpAgent({
      url: agentUrl,
      threadId,
      initialMessages: messageList.map(({ id, role, content }) => ({ id, role, content })),
      initialState: runInput.state,
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
      onEvent: ({ event }) => {
        resetStallTimer(); // every event counts, the CUSTOM ping included
        if (EXTENSION_EVENT_TYPES.includes(event.type)) {
          const claimed = extensions.renderEvent(event, refs.attachments, renderContext(event));
          if (claimed && event.toolCallId) statusClaimed.add(event.toolCallId);
        }
      },
      onToolCallStartEvent: ({ event }) => {
        toolCalls.set(event.toolCallId, { id: event.toolCallId, name: event.toolCallName });
        // Text streamed before a search is the model narrating its plan ("Let me correct
        // that:"); the status line records the search, so only what follows the last
        // search is kept as the reply.
        draft = "";
        schedulePaint();
        if (!extensions.claimsTool(event.toolCallName) && !statusClaimed.has(event.toolCallId)) {
          setStatusLine(toolStatus(event.toolCallName, false));
        }
      },
      onToolCallEndEvent: ({ event, toolCallName, toolCallArgs }) => {
        const toolCall = toolCalls.get(event.toolCallId) || { id: event.toolCallId, name: toolCallName };
        toolCall.args = toolCallArgs;
        toolCalls.set(event.toolCallId, toolCall);
        const renderedByTool = extensions.renderTool(toolCall, refs.attachments, renderContext(event));
        if (!renderedByTool && !statusClaimed.has(event.toolCallId)) {
          setStatusLine(toolStatus(toolCall.name, true));
        }
      },
      onToolCallResultEvent: ({ event }) => {
        const toolCall = toolCalls.get(event.toolCallId);
        if (!toolCall) return;
        toolCall.result = event.content;
        extensions.renderTool(toolCall, refs.attachments, renderContext(event));
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
      await agent.runAgent(
        { runId, forwardedProps: runInput.forwardedProps, abortController: controller },
        subscriber,
      );
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
    // A run that finished without text is kept out of the thread: an empty assistant turn
    // makes every later run of the thread fail the agent's validation (guppi-hr critique
    // finding 3). It shows as an error with Retry instead.
    if (!draft.trim()) {
      showError(false, true);
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

  if (manifest && manifest.extension) await installExtension(manifest.extension);
  // The first thread of this page load; a resumed thread notifies again in switchToThread.
  extensions.notifyThread(threadId);

  // ---- Boot ----
  //
  // A URL carrying an OAuth code always wins: finish that sign-in as before, whatever a
  // stored session might say. Otherwise, a stored session gets a silent refresh: success
  // shows the chat with no redirect; an OAuth error (the refresh token is no longer good)
  // clears the stored session; a network error leaves the stored session in place so a
  // later reload can try again, and shows the sign-in screen with its usual button either
  // way. No stored session is the plain no-session case, unchanged.

  const code = new URLSearchParams(location.search).get("code");
  // Cognito sends the browser back with an error instead of a code when the pre sign-up
  // gate refuses an account (Okta: not assigned to the app), or a sign-in fails some other way.
  const refusal = code ? null : signInRefusal(location.search);
  if (refusal) {
    signinNotice.textContent =
      refusal === "not-invited"
        ? "This account doesn't have access yet. Request an invite below, and sign in again once Sam approves it."
        : "Sign-in didn't complete. Try again.";
    signinNotice.hidden = false;
    history.replaceState(null, "", location.pathname);
    signinScreen.hidden = false;
    return;
  }
  if (code) {
    try {
      await finishSignIn(code);
      showChat();
      await resumeHistory();
      render();
      armWarmStart();
      return;
    } catch (error) {
      // The exchange failed; fall back to the sign-in screen, where it can be retried.
    }
  } else {
    const storedSession = await loadSession();
    const refreshResult = storedSession ? await silentRefresh(storedSession) : undefined;
    switch (decideOnLoad(storedSession, refreshResult)) {
      case "show-chat":
        showChat();
        render();
        armWarmStart();
        return;
      case "clear-and-show-sign-in":
        await clearSession();
        break;
      default:
      // "show-sign-in" (nothing stored) and "keep-and-show-sign-in" (network error) both
      // fall through to the sign-in screen with the stored session untouched.
    }
  }

  signinScreen.hidden = false;
})();
