// Where the page signs in, refreshes and signs out. Two shapes: Cognito's hosted UI (the
// default, from config.authDomain and config.userPoolClientId) and a standard OIDC issuer
// such as an Okta custom authorization server (config.oidc), whose endpoints deploy.sh
// copies from the issuer's discovery document. Pure functions, run under node:test
// (web/test/oidc.test.mjs).

const COGNITO_SCOPE = "openid email profile";
// offline_access asks an OIDC issuer for a refresh token; Cognito issues one without it.
const OIDC_SCOPE = "openid email profile offline_access";

/** The endpoints and client the page uses, for whichever issuer config.json names. */
export function oidcEndpoints(config) {
  const oidc = config && config.oidc;
  if (oidc && oidc.authorizeUrl && oidc.tokenUrl && oidc.clientId) {
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
  const base = `https://${config.authDomain}`;
  return {
    kind: "cognito",
    clientId: config.userPoolClientId,
    scope: COGNITO_SCOPE,
    authorize: `${base}/oauth2/authorize`,
    token: `${base}/oauth2/token`,
    logout: `${base}/logout`,
    authorizeExtra: { identity_provider: "Google" },
  };
}

/** The URL that ends the issuer's session and comes back to the site. */
export function logoutUrl(endpoints, { siteUrl, idToken }) {
  if (endpoints.kind === "cognito") {
    return `${endpoints.logout}?${new URLSearchParams({ client_id: endpoints.clientId, logout_uri: siteUrl })}`;
  }
  if (!endpoints.logout) return siteUrl;
  const params = new URLSearchParams({ post_logout_redirect_uri: siteUrl });
  if (idToken) params.set("id_token_hint", idToken);
  return `${endpoints.logout}?${params}`;
}
