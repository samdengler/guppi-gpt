// Dynatrace RUM on the page, dark behind the `rum` flag (web/features.json). Nothing in
// this file runs unless isEnabled("rum") and config.rum.scriptPath are both set: no
// script element, no listener, no OpenFeature hook, no network call. See
// docs/proposals/dynatrace.md for the flip procedure and what Sam has to supply first.
//
// The script is self-hosted from the site bucket at config.rum.scriptPath (same origin,
// so the CSP's script-src 'self' does not need to change) and inserted the way a static
// <script src> tag would be. Once it defines window.dtrum, this module:
//   - registers an OpenFeature hook that reports each flag evaluation as a RUM session
//     property (docs/proposals/feature-flags.md, "Attaching Dynatrace later")
//   - listens for "guppi:feedback" and reports it as a custom action named
//     "reply-feedback" (docs/proposals/feedback.md, "The Dynatrace RUM mapping")
//   - optionally calls dtrum.identifyUser with a hashed subject, only when
//     config.rum.identifyUser is true (default false)
//
// The dtrum methods used here (enterAction, addActionProperties, leaveAction,
// sendSessionProperties, identifyUser) are confirmed against Dynatrace's published
// TypeScript declarations, @dynatrace/dtrum-api-types
// (https://unpkg.com/@dynatrace/dtrum-api-types/dtrum.d.ts); the prose page named in the
// task, docs.dynatrace.com's RUM JavaScript API reference, 404s as of 3 Sep 2026, and
// its shortlink resolves to a different page (the RUM configuration REST API, not the
// browser dtrum object). The type declarations are the same API surface Dynatrace's own
// examples use, so the method names and argument shapes below are not a guess.

import { OpenFeature } from "@openfeature/web-sdk";
import { FEEDBACK_EVENT } from "./feedback.js";

const DTRUM_POLL_MS = 200;
const DTRUM_POLL_ATTEMPTS = 25; // ~5 seconds; a slow or missing script gives up quietly

// Set once window.dtrum resolves after a real script load; stays null when the flag is
// off, the script path is unset, or the script never defines window.dtrum in time.
let dtrumInstance = null;

/**
 * True only when the flag is on and a script path is configured: the one gate every
 * side effect in this module passes through before touching the DOM or window.dtrum.
 */
export function rumActive(flags, config) {
  return Boolean(flags?.rum) && Boolean(config?.rum?.scriptPath);
}

/**
 * The RUM custom-action property groups for one "guppi:feedback" event detail. Pure: no
 * dtrum call, no DOM, so this is what web/test/rum.test.mjs checks directly.
 * addActionProperties has no null representation, so a withdrawn vote (vote: null) is
 * reported as the string "withdrawn" rather than dropped, and every identifier is
 * normalized to "-" when absent so a masked property does not silently disappear from
 * the action.
 */
export function buildFeedbackActionProperties(detail) {
  const str = (value) => (value === null || value === undefined ? "-" : String(value));
  return {
    vote: str(detail?.vote === null ? "withdrawn" : detail?.vote),
    runId: str(detail?.runId),
    traceId: str(detail?.traceId),
    requestId: str(detail?.requestId),
    threadId: str(detail?.threadId),
  };
}

/**
 * The session-property map for one OpenFeature evaluation. Pure. The key is the flag
 * name (already lower case in web/features.json); the value is "true" or "false" as a
 * short string, since sendSessionProperties has no boolean property type.
 */
export function buildFlagSessionProperty(flagKey, value) {
  return { [String(flagKey).toLowerCase()]: String(Boolean(value)) };
}

function loadScript(path) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = path;
    script.async = true;
    script.addEventListener("load", () => resolve(), { once: true });
    script.addEventListener("error", () => reject(new Error(`failed to load ${path}`)), { once: true });
    document.head.appendChild(script);
  });
}

function waitForDtrum() {
  return new Promise((resolve) => {
    let attempts = 0;
    const check = () => {
      if (window.dtrum) return resolve(window.dtrum);
      attempts += 1;
      if (attempts >= DTRUM_POLL_ATTEMPTS) return resolve(null);
      setTimeout(check, DTRUM_POLL_MS);
    };
    check();
  });
}

function reportFeedbackAction(dtrum, detail) {
  const properties = buildFeedbackActionProperties(detail);
  const actionId = dtrum.enterAction("reply-feedback");
  if (!actionId) return; // 0 means dtrum did not create the action
  dtrum.addActionProperties(actionId, undefined, undefined, properties);
  dtrum.leaveAction(actionId);
}

// A no-op until window.dtrum exists, so it is safe to register immediately (before the
// script has finished loading) and catch evaluations from the very first one. Reads
// window.dtrum directly rather than closing over dtrumInstance so it also works for any
// evaluation that lands between the script settling and this module noticing.
const flagSessionPropertyHook = {
  after(_hookContext, evaluationDetails) {
    if (!window.dtrum) return;
    try {
      window.dtrum.sendSessionProperties(
        undefined,
        undefined,
        buildFlagSessionProperty(evaluationDetails.flagKey, evaluationDetails.value),
      );
    } catch {
      // A session property not predefined in Dynatrace's application settings must not
      // break flag evaluation for the rest of the page.
    }
  },
};

/**
 * Wires Dynatrace RUM onto the page: a no-op unless rumActive(flags, config). Call once,
 * right after initFeatures resolves, before the page reads any other flag, so the
 * OpenFeature hook is registered in time to catch every evaluation that follows (the
 * hook itself stays inert until window.dtrum exists, so evaluations before the script
 * finishes loading are simply not reported, rather than queued).
 */
export function initRum(flags, config) {
  if (!rumActive(flags, config)) return;
  OpenFeature.addHooks(flagSessionPropertyHook);
  loadScript(config.rum.scriptPath)
    .then(waitForDtrum)
    .then((dtrum) => {
      if (!dtrum) return;
      dtrumInstance = dtrum;
      document.addEventListener(FEEDBACK_EVENT, (event) => reportFeedbackAction(dtrum, event.detail));
    })
    .catch(() => {
      // Same-origin fetch failed, most likely because the script has not been uploaded
      // to the site bucket yet (docs/proposals/dynatrace.md); the page must not break.
    });
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * Calls dtrum.identifyUser with a hashed subject, only when config.rum.identifyUser is
 * true (default false). identifyUser ties a RUM session to a real person, so the page
 * asks for it explicitly rather than doing it just because RUM happens to be on.
 * subject is the Cognito sub claim; hashed with SHA-256 (the same primitive app.js
 * already uses for the PKCE code challenge) before it ever reaches Dynatrace. A no-op
 * when RUM never finished loading (dtrumInstance is still null): there is nothing to
 * call, and rumActive was already false or the script has not settled yet.
 */
export async function identifyRumUser(config, subject) {
  if (!dtrumInstance || !config?.rum?.identifyUser || !subject) return;
  dtrumInstance.identifyUser(await sha256Hex(subject));
}
