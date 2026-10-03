import { test } from "node:test";
import assert from "node:assert/strict";

import { logoutUrl, oidcEndpoints } from "../src/oidc.js";
import { signInRefusal } from "../src/invite-core.js";

const COGNITO = { authDomain: "auth.example.com", userPoolClientId: "abc", siteUrl: "https://chat.example.com/" };
const OKTA = {
  ...COGNITO,
  oidc: {
    issuer: "https://org.okta.com/oauth2/aus1",
    clientId: "0oa1",
    authorizeUrl: "https://org.okta.com/oauth2/aus1/v1/authorize",
    tokenUrl: "https://org.okta.com/oauth2/aus1/v1/token",
    logoutUrl: "https://org.okta.com/oauth2/aus1/v1/logout",
  },
};

test("without an oidc block the page keeps Cognito's hosted UI", () => {
  const e = oidcEndpoints(COGNITO);
  assert.equal(e.kind, "cognito");
  assert.equal(e.token, "https://auth.example.com/oauth2/token");
  assert.deepEqual(e.authorizeExtra, { identity_provider: "Google" });
  assert.equal(
    logoutUrl(e, { siteUrl: COGNITO.siteUrl }),
    "https://auth.example.com/logout?client_id=abc&logout_uri=https%3A%2F%2Fchat.example.com%2F",
  );
});

test("an oidc block switches to the issuer's endpoints and asks for a refresh token", () => {
  const e = oidcEndpoints(OKTA);
  assert.equal(e.kind, "oidc");
  assert.equal(e.clientId, "0oa1");
  assert.equal(e.authorize, OKTA.oidc.authorizeUrl);
  assert.match(e.scope, /offline_access/);
  assert.deepEqual(e.authorizeExtra, {});
  const url = new URL(logoutUrl(e, { siteUrl: OKTA.siteUrl, idToken: "id.tok.en" }));
  assert.equal(url.searchParams.get("id_token_hint"), "id.tok.en");
  assert.equal(url.searchParams.get("post_logout_redirect_uri"), OKTA.siteUrl);
});

test("an Okta user not assigned to the app reads as not invited", () => {
  assert.equal(
    signInRefusal("?error=access_denied&error_description=User+is+not+assigned+to+the+client+application."),
    "not-invited",
  );
  assert.equal(signInRefusal("?error=server_error&error_description=boom"), "failed");
});
