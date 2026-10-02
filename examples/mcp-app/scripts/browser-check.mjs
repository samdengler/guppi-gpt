// Headless browser check of the phase 4 experiments on the live project page
// (docs/phase-4.md, step 7), built on guppi-gpt's phase 3 check (scripts/browser-check.mjs
// on its platform branch). Signs the page in without Google by seeding its IndexedDB
// session before load, then for each experiment opens https://chat.dengler.io/p/mcp-app/
// in a fresh context, sends the experiment's prompts, waits for each run to finish, does
// what the experiment needs inside the app (press the card's button, fill the form), and
// records the frames, what each app shows, the replies, and the AG-UI events of each run.
// Saves .deploy/phase-4-E<n>.png per experiment and prints the results as JSON.
//
//   node scripts/browser-check.mjs            every experiment
//   node scripts/browser-check.mjs E3 E6      only these
//
// Playwright and the platform's stack outputs are read from guppi-gpt (GUPPI_GPT_DIR,
// default ../guppi-gpt); nothing there is written. The refresh token comes from
// $HOME/.config/guppi/test-session.json, as for guppi-gpt's test-token.sh. The user pool
// client rotates refresh tokens on every use, so each rotation is written back to that
// file, mode 600, under the same lock test-token.sh takes. No token is printed or logged.

import { createRequire } from "node:module";
import { chmodSync, mkdirSync, readFileSync, renameSync, rmdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const GUPPI_GPT = resolve(process.env.GUPPI_GPT_DIR || join(ROOT, "..", "guppi-gpt"));
const { chromium } = createRequire(join(GUPPI_GPT, "web", "package.json"))("playwright");

const SITE = process.env.GUPPI_SITE_URL || "https://chat.dengler.io";
const PROJECT_PATH = "/p/mcp-app/";
const SESSION_FILE = process.env.GUPPI_TEST_SESSION_FILE || join(homedir(), ".config", "guppi", "test-session.json");
const OUT = join(ROOT, ".deploy");
const RUN_TIMEOUT = 180000;

const FRAMES = ".reply-attachments iframe";

// Each experiment: the prompts sent in one chat, and what to do in the apps afterwards.
const EXPERIMENTS = {
  E1: { prompts: ["Show me a card titled Hello with the body It works"] },
  E2: { prompts: ["Show me a bar chart of the values 3, 1, 4, 1, 5, 9"] },
  E3: {
    prompts: ["Show me a card titled Clicks with the body Press the button"],
    async after(page, results) {
      const app = appFrame(page, 0);
      await app.locator("#card-click").click();
      await page.waitForTimeout(3000);
      results.cardStatusAfterClick = await app.locator("#card-status").textContent();
    },
  },
  E4: {
    prompts: [
      "Show me a card titled Draft with the body First version",
      "Change the body of the Draft card to Second version",
    ],
  },
  E5: { prompts: ["Show me the MCP App Lab static page"] },
  E6: {
    prompts: ["Ask me for my preferences"],
    async after(page, results, send) {
      const app = appFrame(page, 0);
      await app.locator("#prefs-name").fill("Sam");
      await app.locator("#prefs-style").selectOption("detailed");
      await app.locator("#prefs-submit").click();
      await page.waitForTimeout(3000);
      results.formStatusAfterSend = await app.locator("#prefs-status").textContent();
      await send("What display name and reply style did I choose in the form?");
    },
  },
};

function fail(message) {
  console.error(`browser-check: ${message}`);
  process.exit(1);
}

function outputs() {
  let auth = process.env.GUPPI_AUTH_DOMAIN;
  let client = process.env.GUPPI_USER_POOL_CLIENT_ID;
  if (!auth || !client) {
    const stack = JSON.parse(readFileSync(join(GUPPI_GPT, "cdk-outputs.json"), "utf8")).GuppiGpt;
    auth = auth || stack.AuthDomain;
    client = client || stack.UserPoolClientId;
  }
  return { auth, client };
}

async function withLock(fn) {
  const lock = `${SESSION_FILE}.lock`;
  for (let i = 0; i < 100; i += 1) {
    try {
      mkdirSync(lock);
      try {
        return await fn();
      } finally {
        rmdirSync(lock);
      }
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  fail(`could not take ${lock}`);
}

function readRefreshToken() {
  const token = JSON.parse(readFileSync(SESSION_FILE, "utf8")).refreshToken;
  if (typeof token !== "string" || !token) fail(`${SESSION_FILE} has no refreshToken`);
  return token;
}

function writeRefreshToken(token) {
  const tmp = `${SESSION_FILE}.${process.pid}`;
  writeFileSync(tmp, JSON.stringify({ refreshToken: token }), { mode: 0o600 });
  chmodSync(tmp, 0o600);
  renameSync(tmp, SESSION_FILE);
}

function decodeClaims(idToken) {
  const payload = JSON.parse(Buffer.from(idToken.split(".")[1], "base64url").toString("utf8"));
  const { email, name, given_name, family_name, sub } = payload;
  return { email, name, given_name, family_name, sub };
}

// One refresh grant, for a fresh id token's claims; the rotated refresh token is stored at once.
async function freshSession({ auth, client }) {
  return withLock(async () => {
    const body = new URLSearchParams({ grant_type: "refresh_token", client_id: client, refresh_token: readRefreshToken() });
    const response = await fetch(`https://${auth}/oauth2/token`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body,
    });
    const json = await response.json().catch(() => ({}));
    if (!response.ok || !json.id_token) fail(`refresh refused: ${json.error || response.status}`);
    const refreshToken = json.refresh_token || body.get("refresh_token");
    if (json.refresh_token) writeRefreshToken(json.refresh_token);
    return { refreshToken, claims: decodeClaims(json.id_token) };
  });
}

// Runs in every frame before the page's scripts; only the top frame has storage.
function seedSession(record) {
  if (window !== window.top) return;
  const open = indexedDB.open("guppigpt-session", 1);
  open.onupgradeneeded = () => {
    if (!open.result.objectStoreNames.contains("session")) {
      open.result.createObjectStore("session", { keyPath: "id" });
    }
  };
  open.onsuccess = () => {
    const tx = open.result.transaction("session", "readwrite");
    tx.objectStore("session").put(record);
    tx.oncomplete = () => open.result.close();
  };
}

async function storedRefreshToken(page) {
  return page.evaluate(
    () =>
      new Promise((done) => {
        const open = indexedDB.open("guppigpt-session", 1);
        open.onsuccess = () => {
          const get = open.result.transaction("session").objectStore("session").get("current");
          get.onsuccess = () => done(get.result ? get.result.refreshToken : null);
          get.onerror = () => done(null);
        };
        open.onerror = () => done(null);
      }),
  );
}

async function keepNewestToken(page, session) {
  const newest = await storedRefreshToken(page);
  if (newest && newest !== session.refreshToken) {
    await withLock(async () => writeRefreshToken(newest));
    session.refreshToken = newest;
  }
}

// The AG-UI events of one run, reduced to what the report needs: types in order, tool
// call names, and CUSTOM event names with the resource uri and mime type they carry.
function summarizeRun(text) {
  const events = [];
  for (const chunk of text.replace(/\r\n/g, "\n").split("\n\n")) {
    const data = chunk
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) continue;
    try {
      events.push(JSON.parse(data));
    } catch {
      // A keepalive comment or a partial chunk.
    }
  }
  const types = [];
  for (const event of events) {
    if (types[types.length - 1] !== event.type) types.push(event.type);
  }
  return {
    types,
    toolCalls: events.filter((e) => e.type === "TOOL_CALL_START").map((e) => e.toolCallName),
    custom: events
      .filter((e) => e.type === "CUSTOM")
      .map((e) => ({ name: e.name, uri: e.value && e.value.uri, mimeType: e.value && e.value.mimeType })),
    outcome: (events.find((e) => e.type === "RUN_FINISHED") || {}).outcome || null,
  };
}

function appFrame(page, index) {
  return page.frameLocator(FRAMES).nth(index).frameLocator("iframe");
}

async function describeFrames(page) {
  const frames = page.locator(FRAMES);
  const count = await frames.count();
  const described = [];
  for (let i = 0; i < count; i += 1) {
    const frame = frames.nth(i);
    const text = await appFrame(page, i)
      .locator("body")
      .innerText({ timeout: 5000 })
      .catch((error) => `(unreadable: ${error.message.split("\n")[0]})`);
    described.push({
      src: await frame.getAttribute("src"),
      sandbox: await frame.getAttribute("sandbox"),
      height: await frame.getAttribute("height"),
      text: text.replace(/\s+/g, " ").trim().slice(0, 300),
    });
  }
  return described;
}

async function runExperiment(browser, session, name, spec, log) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 1000 } });
  const record = { id: "current", refreshToken: session.refreshToken, claims: session.claims, savedAt: Date.now() };
  await context.addInitScript(seedSession, record);
  const page = await context.newPage();
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") log.push(`${name} console ${m.type()}: ${m.text()}`);
  });
  page.on("pageerror", (error) => log.push(`${name} pageerror: ${error.message}`));
  page.on("response", (response) => {
    if (response.status() < 400) return;
    const url = new URL(response.url());
    log.push(`${name} response ${response.status()}: ${url.origin}${url.pathname}`);
  });
  const results = { experiment: name, runs: [] };
  const input = page.locator("#composer-input");

  async function send(prompt) {
    const run = page.waitForResponse((r) => r.url().includes("/invocations") && r.request().method() === "POST", {
      timeout: RUN_TIMEOUT,
    });
    await input.fill(prompt);
    await input.press("Enter");
    const response = await run;
    const body = await response.text().catch(() => "");
    // The page is idle again when the send button enables for a non-empty draft.
    await input.fill(".");
    await page.locator("#send-btn:enabled").waitFor({ timeout: RUN_TIMEOUT });
    await input.fill("");
    // Give an app the time to finish its handshake, or to fall back after two seconds.
    await page.waitForTimeout(3500);
    const reply = page.locator(".reply-text").last();
    results.runs.push({
      prompt,
      status: response.status(),
      events: summarizeRun(body),
      reply: ((await reply.textContent()) || "").slice(0, 400),
      frames: await page.locator(FRAMES).count(),
    });
  }

  try {
    await page.goto(`${SITE}${PROJECT_PATH}`);
    await page.locator("#chat-screen").waitFor({ state: "visible", timeout: 30000 });
    await keepNewestToken(page, session);
    results.brand = await page.locator("#brand").textContent();
    for (const prompt of spec.prompts) await send(prompt);
    if (spec.after) await spec.after(page, results, send);
    results.frames = await describeFrames(page);
  } catch (error) {
    results.error = error.message.split("\n")[0];
  } finally {
    await page.screenshot({ path: join(OUT, `phase-4-${name}.png`), fullPage: true }).catch(() => {});
    await keepNewestToken(page, session).catch(() => {});
    await context.close();
  }
  return results;
}

async function main() {
  const wanted = process.argv.slice(2).map((a) => a.toUpperCase());
  const names = wanted.length ? wanted : Object.keys(EXPERIMENTS);
  for (const name of names) if (!EXPERIMENTS[name]) fail(`no experiment ${name}`);
  mkdirSync(OUT, { recursive: true });
  const session = await freshSession(outputs());
  const browser = await chromium.launch();
  const log = [];
  const all = [];
  try {
    for (const name of names) all.push(await runExperiment(browser, session, name, EXPERIMENTS[name], log));
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(all, null, 2));
  if (log.length) console.log(log.join("\n"));
  if (all.some((r) => r.error)) process.exitCode = 1;
}

main().catch((error) => fail(error.message.split("\n")[0]));
