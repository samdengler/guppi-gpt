# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3"]
# ///
"""Sign the test harness in to Okta once, through the `guppi-harness` native app.

Okta binds the page's refresh tokens to the browser, so scripts (test-token.sh, the
latency harness, browser checks) get their own: this opens a local callback on
http://localhost:8765/callback, prints the authorize URL to open in a browser that is
signed in to Okta, exchanges the code with PKCE, and writes the refresh token to
~/.config/guppi/test-session.json (mode 600). Nothing secret is printed.

    uv run scripts/okta-harness-signin.py
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path

import boto3

REDIRECT = "http://localhost:8765/callback"
SESSION_FILE = Path(os.environ.get("GUPPI_TEST_SESSION_FILE", Path.home() / ".config/guppi/test-session.json"))


def param(name: str) -> str:
    return boto3.client("ssm", region_name="us-east-1").get_parameter(Name=f"/guppi/okta/{name}")["Parameter"]["Value"]


def main() -> None:
    client_id, authorize_url, token_url = param("harness-client-id"), param("authorize-url"), param("token-url")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = authorize_url + "?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "scope": "openid email profile offline_access",
        "redirect_uri": REDIRECT, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    result: dict = {}
    done = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "code" in query:
                body = urllib.parse.urlencode({
                    "grant_type": "authorization_code", "client_id": client_id, "code": query["code"][0],
                    "redirect_uri": REDIRECT, "code_verifier": verifier,
                }).encode()
                request = urllib.request.Request(token_url, data=body, headers={"content-type": "application/x-www-form-urlencoded"})
                try:
                    tokens = json.load(urllib.request.urlopen(request, timeout=30))
                    result["refresh"] = tokens.get("refresh_token")
                except Exception as error:  # noqa: BLE001
                    result["error"] = f"token exchange failed: {type(error).__name__}"
            else:
                result["error"] = query.get("error", ["no code"])[0]
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab." if result.get("refresh") else b"Sign-in failed; see the terminal.")
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", 8765), Callback)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("open this URL in a browser signed in to Okta:")
    print(url, flush=True)
    done.wait(timeout=300)
    server.shutdown()
    if not result.get("refresh"):
        raise SystemExit(f"no refresh token: {result.get('error', 'timed out')}")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=SESSION_FILE.parent, delete=False) as handle:
        os.chmod(handle.name, 0o600)
        json.dump({"refreshToken": result["refresh"]}, handle)
    os.replace(handle.name, SESSION_FILE)
    print(f"saved the harness session to {SESSION_FILE}")


if __name__ == "__main__":
    main()
