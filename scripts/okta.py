# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3"]
# ///
"""Configure the Okta org that signs people in to chat.dengler.io, and publish what the
stacks need to SSM. Idempotent: every object is found by name and created only if missing.

What it sets up (docs/proposals/okta.md; guppi-hr D20, D46):

- group `chat-users`: the people who may sign in (the invite gate); the admin who owns the
  API token is added to it;
- trusted origin `https://chat.dengler.io` for CORS and redirects;
- the browser OIDC app `chat.dengler.io`: authorization code with PKCE, refresh tokens
  that rotate, sign-in and sign-out redirects to the site, assigned to `chat-users`;
- an authentication policy for both apps: a password and any second factor;
- the custom authorization server `guppi` (audience `api://guppi`) with a policy that
  issues the app's tokens to `chat-users` only: 60-minute access tokens, refresh tokens
  that expire after 7 days unused.

It publishes /guppi/okta/issuer, /guppi/okta/client-id, /guppi/okta/audience and the
issuer's endpoints, and writes the issuer's public signing keys to the token issuer's
`okta-keys.json`, which the Rust binary embeds (guppi-hr D51): after Okta rotates its key,
run this, commit the file and deploy (until then the issuer fetches the new key once). The API token comes from 1Password (`op://Personal/Okta API token`)
and is never printed or passed as an argument.

    uv run scripts/okta.py            # configure and publish
    uv run scripts/okta.py --check    # show what exists, change nothing
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import boto3

SITE = "https://chat.dengler.io"
GROUP = "chat-users"
APP_LABEL = "chat.dengler.io"
# A native app for scripts (the latency harness, browser checks): Okta binds a browser
# app's refresh tokens to the browser, so a script cannot use the page's. It signs in once
# through a local callback (scripts/okta-harness-signin.py) and gets the same audience.
HARNESS_LABEL = "guppi-harness"
HARNESS_REDIRECT = "http://localhost:8765/callback"
AUTH_SERVER = "guppi"
AUDIENCE = "api://guppi"
POLICY = "chat.dengler.io sign-in"
RULE = "chat-users, code and refresh"
OP_ITEM = "op://Personal/Okta API token"
PARAMS = "/guppi/okta"


def secret(field: str) -> str:
    return subprocess.run(["op", "read", f"{OP_ITEM}/{field}"], check=True, capture_output=True, text=True).stdout.strip()


class Okta:
    def __init__(self, host: str, token: str) -> None:
        self.base = f"https://{host}"
        self._token = token

    def call(self, method: str, path: str, body: dict | None = None) -> dict | list | None:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"SSWS {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise SystemExit(f"okta {method} {path}: HTTP {error.code} {detail}") from None

    def find(self, path: str, match) -> dict | None:
        for item in self.call("GET", path) or []:
            if match(item):
                return item
        return None


def ensure_group(okta: Okta, check: bool) -> dict | None:
    q = urllib.parse.quote(GROUP)
    group = okta.find(f"/api/v1/groups?q={q}", lambda g: g["profile"]["name"] == GROUP)
    if group or check:
        return group
    return okta.call("POST", "/api/v1/groups", {"profile": {"name": GROUP, "description": "May sign in to chat.dengler.io"}})


def ensure_origin(okta: Okta, check: bool) -> dict | None:
    origin = okta.find("/api/v1/trustedOrigins", lambda o: o["origin"] == SITE)
    if origin or check:
        return origin
    return okta.call(
        "POST",
        "/api/v1/trustedOrigins",
        {"name": APP_LABEL, "origin": SITE, "scopes": [{"type": "CORS"}, {"type": "REDIRECT"}]},
    )


def app_settings() -> dict:
    return {
        "oauthClient": {
            "client_uri": f"{SITE}/",
            "redirect_uris": [f"{SITE}/"],
            "post_logout_redirect_uris": [f"{SITE}/"],
            "response_types": ["code"],
            "grant_types": ["authorization_code", "refresh_token"],
            "application_type": "browser",
            "consent_method": "TRUSTED",
            "refresh_token": {"rotation_type": "ROTATE", "leeway": 30},
        }
    }


def ensure_app(okta: Okta, check: bool) -> dict | None:
    q = urllib.parse.quote(APP_LABEL)
    app = okta.find(f"/api/v1/apps?q={q}", lambda a: a["label"] == APP_LABEL and a["status"] == "ACTIVE")
    if app or check:
        return app
    return okta.call(
        "POST",
        "/api/v1/apps",
        {
            "name": "oidc_client",
            "label": APP_LABEL,
            "signOnMode": "OPENID_CONNECT",
            "credentials": {"oauthClient": {"token_endpoint_auth_method": "none", "pkce_required": True}},
            "settings": app_settings(),
        },
    )


def harness_settings() -> dict:
    return {
        "oauthClient": {
            "redirect_uris": [HARNESS_REDIRECT],
            "response_types": ["code"],
            "grant_types": ["authorization_code", "refresh_token"],
            "application_type": "native",
            "consent_method": "TRUSTED",
            "refresh_token": {"rotation_type": "ROTATE", "leeway": 30},
        }
    }


def ensure_harness(okta: Okta, check: bool) -> dict | None:
    q = urllib.parse.quote(HARNESS_LABEL)
    app = okta.find(f"/api/v1/apps?q={q}", lambda a: a["label"] == HARNESS_LABEL and a["status"] == "ACTIVE")
    if app or check:
        return app
    return okta.call(
        "POST",
        "/api/v1/apps",
        {
            "name": "oidc_client",
            "label": HARNESS_LABEL,
            "signOnMode": "OPENID_CONNECT",
            "credentials": {"oauthClient": {"token_endpoint_auth_method": "none", "pkce_required": True}},
            "settings": harness_settings(),
        },
    )


def ensure_auth_server(okta: Okta, check: bool) -> dict | None:
    server = okta.find("/api/v1/authorizationServers", lambda s: s["name"] == AUTH_SERVER)
    if server or check:
        return server
    return okta.call(
        "POST",
        "/api/v1/authorizationServers",
        {"name": AUTH_SERVER, "description": "chat.dengler.io sign-in", "audiences": [AUDIENCE]},
    )


def ensure_policy(okta: Okta, server_id: str, client_ids: list[str], group_id: str, check: bool) -> None:
    base = f"/api/v1/authorizationServers/{server_id}/policies"
    policy = okta.find(base, lambda p: p["name"] == POLICY)
    if not policy:
        if check:
            print("policy: missing")
            return
        policy = okta.call(
            "POST",
            base,
            {
                "type": "OAUTH_AUTHORIZATION_POLICY",
                "status": "ACTIVE",
                "name": POLICY,
                "description": "Tokens for the chat.dengler.io app, to chat-users only",
                "priority": 1,
                "conditions": {"clients": {"include": client_ids}},
            },
        )
    elif not check and sorted(policy["conditions"]["clients"]["include"]) != sorted(client_ids):
        policy["conditions"]["clients"]["include"] = client_ids
        okta.call("PUT", f"{base}/{policy['id']}", policy)
    rules = f"{base}/{policy['id']}/rules"
    rule = okta.find(rules, lambda r: r["name"] == RULE)
    body = {
        "type": "RESOURCE_ACCESS",
        "name": RULE,
        "priority": 1,
        "conditions": {
            "people": {"groups": {"include": [group_id]}},
            # Refresh is not a separate grant here; the code grant covers it.
            "grantTypes": {"include": ["authorization_code"]},
            "scopes": {"include": ["*"]},
        },
        "actions": {
            "token": {
                "accessTokenLifetimeMinutes": 60,
                "refreshTokenLifetimeMinutes": 0,
                "refreshTokenWindowMinutes": 10080,
            }
        },
    }
    if check:
        print(f"policy: {policy['id']}, rule: {'present' if rule else 'missing'}")
        return
    if rule:
        okta.call("PUT", f"{rules}/{rule['id']}", body)
    else:
        okta.call("POST", rules, body)


ACCESS_POLICY = "chat.dengler.io: password and any second factor"
ACCESS_RULE = "password and any second factor"


def ensure_access_policy(okta: Okta, app_ids: list[str], check: bool) -> None:
    """The sign-on (authentication) policy for the chat and harness apps: a password and any
    one other factor (Okta Verify, an email code or a passkey), asked again every 12 hours.
    Okta's default "Any two factors" policy wants a phishing-resistant, device-bound factor
    (FastPass or a passkey), which refuses any browser on a device without one (4 Oct 2026)."""
    policy = okta.find("/api/v1/policies?type=ACCESS_POLICY", lambda p: p["name"] == ACCESS_POLICY)
    if check:
        print(f"access policy: {'present' if policy else 'missing'}")
        return
    if not policy:
        policy = okta.call("POST", "/api/v1/policies", {
            "type": "ACCESS_POLICY", "status": "ACTIVE", "name": ACCESS_POLICY,
            "description": "Sign-in to chat.dengler.io: a password and one more factor of any kind",
        })
    rules = f"/api/v1/policies/{policy['id']}/rules"
    body = {
        "type": "ACCESS_POLICY", "name": ACCESS_RULE, "priority": 0,
        "actions": {"appSignOn": {"access": "ALLOW", "verificationMethod": {
            "type": "ASSURANCE", "factorMode": "2FA", "reauthenticateIn": "PT12H",
            "constraints": [{"knowledge": {"required": True, "types": ["password"]}}],
        }}},
    }
    rule = okta.find(rules, lambda r: r["name"] == ACCESS_RULE)
    if rule:
        okta.call("PUT", f"{rules}/{rule['id']}", body)
    else:
        okta.call("POST", rules, body)
    for app_id in app_ids:
        okta.call("PUT", f"/api/v1/apps/{app_id}/policies/{policy['id']}", None)


OKTA_KEYS_FILE = Path(__file__).resolve().parents[1] / "infra" / "guppi_gpt_infra" / "lambdas" / "obo_issuer" / "okta-keys.json"


def write_okta_keys(jwks_url: str, check: bool) -> None:
    """Okta's public keys, only the fields the issuer checks with, for the binary to embed."""
    with urllib.request.urlopen(jwks_url, timeout=10) as response:  # noqa: S310 - Okta's https URL
        keys = [{"kid": k["kid"], "kty": k["kty"], "n": k["n"], "e": k["e"]}
                for k in json.load(response)["keys"] if k.get("kty") == "RSA"]
    text = json.dumps({"keys": keys}, indent=1) + "\n"
    current = OKTA_KEYS_FILE.read_text() if OKTA_KEYS_FILE.exists() else ""
    if check:
        print(f"okta-keys.json: {'current' if current == text else 'differs from Okta'}")
        return
    if current != text:
        OKTA_KEYS_FILE.write_text(text)
        print("okta-keys.json: written; commit it and deploy GuppiGpt")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report what exists, change nothing")
    args = parser.parse_args()
    host = secret("hostname")
    okta = Okta(host, secret("credential"))

    me = okta.call("GET", "/api/v1/users/me")
    group = ensure_group(okta, args.check)
    origin = ensure_origin(okta, args.check)
    app = ensure_app(okta, args.check)
    server = ensure_auth_server(okta, args.check)
    harness = ensure_harness(okta, args.check)
    print(f"org {host}; admin {me['profile']['login']}")
    print(f"group {GROUP}: {group and group['id']}")
    print(f"trusted origin {SITE}: {'present' if origin else 'missing'}")
    print(f"app {APP_LABEL}: {app and app['id']}")
    print(f"authorization server {AUTH_SERVER}: {server and server['id']}")
    print(f"app {HARNESS_LABEL}: {harness and harness['id']}")
    if not (group and app and server and harness):
        if args.check:
            return
        sys.exit("something was not created")

    client_id = app["credentials"]["oauthClient"]["client_id"]
    if not args.check:
        # Settings again on an existing app, so a change here reaches it.
        # Okta's update replaces the whole app, so ours are merged into its own settings
        # (it requires fields such as issuer_mode that the create filled in).
        settings = app["settings"]
        settings["oauthClient"] = {**settings.get("oauthClient", {}), **app_settings()["oauthClient"]}
        okta.call("PUT", f"/api/v1/apps/{app['id']}", {**{k: app[k] for k in ("name", "label", "signOnMode")}, "credentials": app["credentials"], "settings": settings})
        okta.call("PUT", f"/api/v1/apps/{app['id']}/groups/{group['id']}", {})
        okta.call("PUT", f"/api/v1/apps/{harness['id']}/groups/{group['id']}", {})
        okta.call("PUT", f"/api/v1/groups/{group['id']}/users/{me['id']}", None)
    harness_client_id = harness["credentials"]["oauthClient"]["client_id"]
    ensure_policy(okta, server["id"], [client_id, harness_client_id], group["id"], args.check)
    ensure_access_policy(okta, [app["id"], harness["id"]], args.check)

    issuer = server["issuer"]
    discovery = okta.call("GET", f"/oauth2/{server['id']}/.well-known/openid-configuration")
    values = {
        "issuer": issuer,
        "client-id": client_id,
        "harness-client-id": harness_client_id,
        "audience": AUDIENCE,
        "discovery-url": f"{issuer}/.well-known/openid-configuration",
        "authorize-url": discovery["authorization_endpoint"],
        "token-url": discovery["token_endpoint"],
        "logout-url": discovery["end_session_endpoint"],
        "jwks-url": discovery["jwks_uri"],
        # The invite flow adds an approved person to this group (docs/proposals/invites.md).
        "group-id": group["id"],
        "org-url": f"https://{host}",
    }
    for name, value in values.items():
        print(f"{PARAMS}/{name}: {value}")
    write_okta_keys(discovery["jwks_uri"], args.check)
    if args.check:
        return
    ssm = boto3.client("ssm", region_name="us-east-1")
    for name, value in values.items():
        ssm.put_parameter(Name=f"{PARAMS}/{name}", Value=value, Type="String", Overwrite=True)
    print("published to SSM")


if __name__ == "__main__":
    main()
