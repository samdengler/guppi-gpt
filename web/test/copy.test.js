import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { emptyStateText, hintText } from "../src/copy.js";

const flags = JSON.parse(readFileSync(new URL("../features.json", import.meta.url)));
const KEYS = "Enter to send, Shift+Enter for a new line.";

test("the product flags that have not launched ship off", () => {
  // history and feedback stay dark; logging and rum were turned on 5 Sep 2026.
  assert.equal(flags.history, false);
  assert.equal(flags.feedback, false);
});

test("with both switches off the page says nothing is saved", () => {
  assert.equal(hintText(false, false), `${KEYS} Nothing is saved.`);
  assert.equal(emptyStateText(false, false), "Ask anything. This conversation is not saved.");
});

test("logging on names the logging", () => {
  assert.equal(hintText(false, true), `${KEYS} Conversations are logged for troubleshooting.`);
  assert.equal(
    emptyStateText(false, true),
    "Ask anything. Conversations are logged for troubleshooting.",
  );
});

test("history on names the device", () => {
  assert.equal(hintText(true, false), `${KEYS} Chats are saved on this device only.`);
  assert.equal(emptyStateText(true, false), "Ask anything. Chats are saved on this device only.");
});

test("both switches on name both", () => {
  assert.equal(
    hintText(true, true),
    `${KEYS} Chats are saved on this device and logged for troubleshooting.`,
  );
  assert.equal(
    emptyStateText(true, true),
    "Ask anything. Chats are saved on this device and logged for troubleshooting.",
  );
});
