import { test } from "node:test";
import assert from "node:assert/strict";
import { inviteBody, inviteResult, signInRefusal } from "../src/invite-core.js";

test("another sign-in error is reported as a plain failure", () => {
  assert.equal(signInRefusal("?error=access_denied&error_description=User+cancelled"), "failed");
});

test("a URL without an error is no refusal", () => {
  assert.equal(signInRefusal(""), null);
  assert.equal(signInRefusal("?code=abc&state=%2F"), null);
});

test("the request body trims, lower-cases the address and keeps an empty note out", () => {
  assert.deepEqual(inviteBody({ name: "  Pat ", email: " Pat@Example.com ", note: "  " }), {
    ok: true,
    body: { name: "Pat", email: "pat@example.com" },
  });
  assert.deepEqual(inviteBody({ name: "Pat", email: "pat@example.com", note: " hi " }).body, {
    name: "Pat",
    email: "pat@example.com",
    note: "hi",
  });
});

test("a missing name, a bad address or a long note is caught before sending", () => {
  assert.equal(inviteBody({ name: "", email: "pat@example.com" }).ok, false);
  assert.match(inviteBody({ name: "Pat", email: "pat" }).error, /email/i);
  assert.match(inviteBody({ name: "Pat", email: "pat@example.com", note: "x".repeat(501) }).error, /500/);
  assert.match(inviteBody({ name: "x".repeat(101), email: "pat@example.com" }).error, /100/);
});

test("each answer from the invite API has its own outcome", () => {
  assert.equal(inviteResult(202).ok, true);
  assert.match(inviteResult(429).text, /few minutes/);
  assert.equal(inviteResult(400).ok, false);
  assert.equal(inviteResult(0).ok, false);
});
