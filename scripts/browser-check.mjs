// Headless browser check of the MCP Apps host on the live site (docs/proposals/platform-phase-3.md,
// step 8). Signs the page in without Google by seeding its IndexedDB session before load, the
// way a reload after a real sign-in finds it, then:
//
//   /p/mcp-app/  asks for a card, waits for an iframe in .reply-attachments and the card's
//                title inside the sandbox, and saves .deploy/phase-3-card.png
//   /            waits for the chat screen and saves .deploy/phase-3-root.png
//
// The refresh token comes from $HOME/.config/guppi/test-session.json, as for test-token.sh.
// The issuer rotates refresh tokens on every use, so each rotation (this script's
// own refresh for the id token claims, and the page's silent refresh) is written back to that
// file, mode 600, under the same lock test-token.sh takes. No token is printed or logged.
//
//   node scripts/browser-check.mjs        (Playwright is a dev dependency of web/)

import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, renameSync, rmdirSync, writeFileSync, chmodSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const { chromium } = createRequire(join(ROOT, "web", "package.json"))("playwright");

const SITE = process.env.GUPPI_SITE_URL || "https://chat.dengler.io";
const SESSION_FILE = process.env.GUPPI_TEST_SESSION_FILE || join(homedir(), ".config", "guppi", "test-session.json");
const PROMPT = "Show me a card titled Hello with the body It works";
const OUT = join(ROOT, ".deploy");

function fail(message) {
  console.error(`browser-check: ${message}`);
  process.exit(1);
}

function outputs() {
  // Okta since guppi-hr D46: the token URL and the harness's own native app, from what
  // scripts/okta.py published, as scripts/test-token.sh reads them.
  const ssm = (name) =>
    execFileSync("aws", ["ssm", "get-parameter", "--name", name, "--query", "Parameter.Value", "--output", "text"], {
      encoding: "utf8",
    }).trim();
  const tokenUrl = process.env.GUPPI_TOKEN_URL || ssm("/guppi/okta/token-url");
  const client = process.env.GUPPI_CLIENT_ID || ssm("/guppi/okta/harness-client-id");
  return { tokenUrl, client };
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
      await new Promise((resolve) => setTimeout(resolve, 100));
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
async function freshSession({ tokenUrl, client }) {
  return withLock(async () => {
    const body = new URLSearchParams({ grant_type: "refresh_token", client_id: client, refresh_token: readRefreshToken() });
    const response = await fetch(tokenUrl, {
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

// Runs in every frame before the page's scripts; only the top frame has storage (the
// sandbox frames are opaque origins).
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

// The page rotates the refresh token on its silent refresh; this reads the newest back.
async function storedRefreshToken(page) {
  return page.evaluate(
    () =>
      new Promise((resolve) => {
        const open = indexedDB.open("guppigpt-session", 1);
        open.onsuccess = () => {
          const get = open.result.transaction("session").objectStore("session").get("current");
          get.onsuccess = () => resolve(get.result ? get.result.refreshToken : null);
          get.onerror = () => resolve(null);
        };
        open.onerror = () => resolve(null);
      }),
  );
}

async function openSignedIn(browser, session, path, log) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const record = { id: "current", refreshToken: session.refreshToken, claims: session.claims, savedAt: Date.now() };
  await context.addInitScript(seedSession, record);
  const page = await context.newPage();
  page.on("console", (message) => {
    if (message.type() === "error" || message.type() === "warning") log.push(`${path} console ${message.type()}: ${message.text()}`);
  });
  page.on("pageerror", (error) => log.push(`${path} pageerror: ${error.message}`));
  // Failed responses by origin and path only; a query string could carry an OAuth value.
  page.on("response", (response) => {
    if (response.status() < 400) return;
    const url = new URL(response.url());
    log.push(`${path} response ${response.status()}: ${url.origin}${url.pathname}`);
  });
  await page.goto(`${SITE}${path}`);
  await page.locator("#chat-screen").waitFor({ state: "visible", timeout: 30000 });
  return { context, page };
}

async function keepNewestToken(page, session) {
  const newest = await storedRefreshToken(page);
  if (newest && newest !== session.refreshToken) {
    await withLock(async () => writeRefreshToken(newest));
    session.refreshToken = newest;
  }
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  const session = await freshSession(outputs());
  const browser = await chromium.launch();
  const log = [];
  const results = {};
  try {
    // The project page: one question, one card.
    {
      const { context, page } = await openSignedIn(browser, session, "/p/mcp-app/", log);
      try {
        await keepNewestToken(page, session);
        results.cardBrand = await page.locator("#brand").textContent();
        await page.locator("#composer-input").fill(PROMPT);
        await page.locator("#composer-input").press("Enter");
        const frame = page.locator(".reply-attachments iframe");
        await frame.first().waitFor({ state: "attached", timeout: 120000 });
        results.frames = await frame.count();
        results.frameSandbox = await frame.first().getAttribute("sandbox");
        results.frameSrc = await frame.first().getAttribute("src");
        const title = page.frameLocator(".reply-attachments iframe").frameLocator("iframe").locator("#card-title");
        await title.waitFor({ state: "visible", timeout: 20000 });
        // The card falls back to its baked values after two seconds of host silence; a
        // title and height here mean the bridge answered before that.
        await page.waitForTimeout(3000);
        results.cardTitle = await title.textContent();
        results.cardBody = await page
          .frameLocator(".reply-attachments iframe")
          .frameLocator("iframe")
          .locator("#card-body")
          .textContent();
        results.frameHeight = await frame.first().getAttribute("height");
        results.replyText = (await page.locator(".reply-text").last().textContent()).slice(0, 200);
        results.status = await page.locator(".reply-status").last().textContent();
        await page.screenshot({ path: join(OUT, "phase-3-card.png"), fullPage: true });
        await keepNewestToken(page, session);
      } catch (error) {
        results.cardError = error.message.split("\n")[0];
        await page.screenshot({ path: join(OUT, "phase-3-card.png"), fullPage: true }).catch(() => {});
        await keepNewestToken(page, session).catch(() => {});
      } finally {
        await context.close();
      }
    }
    // The root page, unchanged.
    {
      const { context, page } = await openSignedIn(browser, session, "/", log);
      try {
        await keepNewestToken(page, session);
        results.rootBrand = await page.locator("#brand").textContent();
        results.rootTitle = await page.title();
        results.rootPlaceholder = await page.locator("#composer-input").getAttribute("placeholder");
        await page.screenshot({ path: join(OUT, "phase-3-root.png"), fullPage: true });
      } finally {
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(results, null, 2));
  if (log.length) console.log(log.join("\n"));
  if (results.cardError || results.cardTitle !== "Hello") process.exitCode = 1;
}

main().catch((error) => fail(error.message.split("\n")[0]));
