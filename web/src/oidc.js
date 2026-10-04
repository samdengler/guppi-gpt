// Where the page signs in, refreshes and signs out: a standard OIDC issuer, the Okta custom
// authorization server `guppi` since guppi-hr D46, whose endpoints deploy.sh copies into
// config.json (`oidc`) from what scripts/okta.py published. Cognito's hosted UI, the
// earlier issuer, is gone. Pure functions, run under node:test (web/test/oidc.test.mjs).

// offline_access asks the issuer for a refresh token.
const OIDC_SCOPE = "openid email profile offline_access";

/** The endpoints and client the page uses. Throws when config.json has no usable `oidc`. */
export function oidcEndpoints(config) {
  const oidc = config && config.oidc;
  if (!oidc || !oidc.authorizeUrl || !oidc.tokenUrl || !oidc.clientId) {
    throw new Error("config.json has no oidc block; run scripts/deploy.sh");
  }
  return {
    kind: "oidc",
    clientId: oidc.clientId,
    scope: oidc.scope || OIDC_SCOPE,
    authorize: oidc.authorizeUrl,
    token: oidc.tokenUrl,
    logout: oidc.logoutUrl || null,
    // An Okta identity provider id sends the employee straight to it (idp=).
    authorizeExtra: oidc.idp ? { idp: oidc.idp } : {},
  };
}

/** The URL that ends the issuer's session and comes back to the site. */
export function logoutUrl(endpoints, { siteUrl, idToken }) {
  if (!endpoints.logout) return siteUrl;
  const params = new URLSearchParams({ post_logout_redirect_uri: siteUrl });
  if (idToken) params.set("id_token_hint", idToken);
  return `${endpoints.logout}?${params}`;
}
