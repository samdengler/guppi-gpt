import { test } from "node:test";
import assert from "node:assert/strict";
import {
  approveBody,
  decidedText,
  lookupUrl,
  outcome,
  readLink,
  requestRows,
} from "../src/approve-core.js";

const TOKEN = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0";

test("the link gives a lower-cased email and the token", () => {
  assert.deepEqual(readLink(`?email=Pat%40Example.com&token=${TOKEN}`), {
    email: "pat@example.com",
    token: TOKEN,
  });
});

test("a link without a UUID token or an email is not a link", () => {
  assert.equal(readLink(`?email=pat%40example.com&token=nope`), null);
  assert.equal(readLink(`?token=${TOKEN}`), null);
  assert.equal(readLink(""), null);
});

test("the lookup carries both values, encoded", () => {
  assert.equal(
    lookupUrl({ email: "pat+x@example.com", token: TOKEN }),
    `/api/invite/request?email=pat%2Bx%40example.com&token=${TOKEN}`,
  );
});

test("the approve body is the email and the token only", () => {
  assert.deepEqual(JSON.parse(approveBody({ email: "pat@example.com", token: TOKEN })), {
    email: "pat@example.com",
    token: TOKEN,
  });
});

test("rows show every field, with a missing note said plainly", () => {
  const rows = requestRows(
    { name: "Pat", email: "pat@example.com", note: "", requestedAt: 1790995496884 },
    (ms) => `t${ms}`,
  );
  assert.deepEqual(rows, [
    ["Name", "Pat"],
    ["Email", "pat@example.com"],
    ["Note", "None"],
    ["Requested", "t1790995496884"],
  ]);
});

test("a decided request replaces the button with a sentence", () => {
  assert.equal(decidedText("approved"), "Already approved.");
  assert.match(decidedText("revoked"), /^Revoked/);
  assert.equal(decidedText("pending"), null);
});

test("each approve answer has its own message", () => {
  assert.deepEqual(outcome(200, "pat@example.com"), {
    ok: true,
    text: "Approved. pat@example.com can sign in with Google now.",
  });
  assert.equal(outcome(409, "pat@example.com").ok, false);
  assert.match(outcome(409, "x").text, /already decided/);
  assert.match(outcome(500, "x").text, /scripts\/invite\.sh/);
});
