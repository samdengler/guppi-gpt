import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildSessionRecord,
  classifyRefreshFailure,
  decideOnLoad,
} from "../src/session.js";

// ---- buildSessionRecord: the shape of the saved record ----

test("buildSessionRecord keeps only the header claims and the refresh token", () => {
  const record = buildSessionRecord(
    "the-refresh-token",
    {
      email: "sam@example.com",
      name: "Sam Dengler",
      given_name: "Sam",
      family_name: "Dengler",
      sub: "abc-123",
    },
    1000,
  );
  assert.deepEqual(record, {
    id: "current",
    refreshToken: "the-refresh-token",
    claims: {
      email: "sam@example.com",
      name: "Sam Dengler",
      given_name: "Sam",
      family_name: "Dengler",
      sub: "abc-123",
    },
    savedAt: 1000,
  });
});

test("buildSessionRecord drops every claim it was not told to keep", () => {
  const record = buildSessionRecord("rt", {
    email: "sam@example.com",
    name: "Sam Dengler",
    given_name: "Sam",
    family_name: "Dengler",
    sub: "abc-123",
    "cognito:groups": ["admins"],
    email_verified: true,
    aud: "some-client-id",
    iss: "https://cognito-idp.us-east-1.amazonaws.com/pool",
  });
  assert.deepEqual(Object.keys(record.claims).sort(), [
    "email",
    "family_name",
    "given_name",
    "name",
    "sub",
  ]);
});

test("buildSessionRecord never carries an access token: the function has no way to accept one", () => {
  // There is no parameter for it, so even an attempt to smuggle one through the claims
  // object cannot end up on the record.
  const record = buildSessionRecord("rt", { access_token: "should-not-appear", sub: "u1" });
  assert.equal(JSON.stringify(record).includes("should-not-appear"), false);
  assert.equal("accessToken" in record, false);
  assert.equal("access_token" in record, false);
  assert.equal("access_token" in record.claims, false);
});

test("buildSessionRecord defaults savedAt to the current time", () => {
  const before = Date.now();
  const record = buildSessionRecord("rt", { sub: "u1" });
  const after = Date.now();
  assert.ok(record.savedAt >= before && record.savedAt <= after);
});

// ---- classifyRefreshFailure ----

test("classifyRefreshFailure treats a rejected fetch as a network error", () => {
  assert.equal(classifyRefreshFailure({ networkError: true }), "network-error");
});

test("classifyRefreshFailure treats invalid_grant as an OAuth error", () => {
  assert.equal(
    classifyRefreshFailure({ status: 400, body: { error: "invalid_grant" } }),
    "oauth-error",
  );
});

for (const error of ["expired", "revoked"]) {
  test(`classifyRefreshFailure treats ${error} as an OAuth error`, () => {
    assert.equal(classifyRefreshFailure({ status: 400, body: { error } }), "oauth-error");
  });
}

test("classifyRefreshFailure treats an unrelated 4xx body as a network error", () => {
  assert.equal(
    classifyRefreshFailure({ status: 400, body: { error: "invalid_request" } }),
    "network-error",
  );
});

test("classifyRefreshFailure treats a 5xx response as a network error", () => {
  assert.equal(
    classifyRefreshFailure({ status: 500, body: { error: "invalid_grant" } }),
    "network-error",
  );
});

test("classifyRefreshFailure treats a missing or malformed body as a network error", () => {
  assert.equal(classifyRefreshFailure({ status: 400, body: null }), "network-error");
  assert.equal(classifyRefreshFailure({ status: 400 }), "network-error");
  assert.equal(classifyRefreshFailure(), "network-error");
});

// ---- decideOnLoad: the boot decision ----

test("decideOnLoad shows the sign-in screen when nothing is stored", () => {
  assert.equal(decideOnLoad(null, undefined), "show-sign-in");
});

test("decideOnLoad shows chat, no redirect, when the silent refresh succeeds", () => {
  assert.equal(decideOnLoad({ refreshToken: "rt" }, { ok: true }), "show-chat");
});

test("decideOnLoad clears the session and shows sign-in on an OAuth error", () => {
  assert.equal(
    decideOnLoad({ refreshToken: "rt" }, { ok: false, kind: "oauth-error" }),
    "clear-and-show-sign-in",
  );
});

test("decideOnLoad keeps the session and shows sign-in on a network error", () => {
  assert.equal(
    decideOnLoad({ refreshToken: "rt" }, { ok: false, kind: "network-error" }),
    "keep-and-show-sign-in",
  );
});

test("decideOnLoad treats a missing refresh result for a stored session as a network error", () => {
  // Defensive: if the caller forgets to pass a result for a session that exists, do not
  // silently sign the person out of a session that might still be good.
  assert.equal(decideOnLoad({ refreshToken: "rt" }, undefined), "keep-and-show-sign-in");
});
