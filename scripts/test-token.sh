#!/usr/bin/env bash
# Prints a fresh Cognito access token for the test session on stdout, and nothing else.
#
# The refresh token lives in $HOME/.config/guppi/test-session.json ({"refreshToken": ...},
# mode 600, outside every repository), seeded by scripts/overnight.sh from 1Password. The
# user pool client rotates refresh tokens on every use, so the rotated token is written
# back to that file before the access token is printed; the next call uses it.
#
# GUPPI_AUTH_DOMAIN and GUPPI_USER_POOL_CLIENT_ID come from the environment
# (scripts/overnight.sh exports both), else from cdk-outputs.json.
#
# No token is ever passed as a command argument (curl reads the form from stdin) or
# written anywhere but that file and stdout. Errors name the OAuth error code only.
#
#   curl -H "authorization: Bearer $(scripts/test-token.sh)" ...
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SESSION_FILE="${GUPPI_TEST_SESSION_FILE:-$HOME/.config/guppi/test-session.json}"
OUTPUTS="$ROOT/cdk-outputs.json"

fail() { echo "test-token: $*" >&2; exit 1; }

command -v jq >/dev/null || fail "jq is required"
[[ -f "$SESSION_FILE" ]] || fail "no test session at $SESSION_FILE (run scripts/overnight.sh to seed it)"

if [[ -z "${GUPPI_AUTH_DOMAIN:-}" || -z "${GUPPI_USER_POOL_CLIENT_ID:-}" ]]; then
  [[ -f "$OUTPUTS" ]] || fail "set GUPPI_AUTH_DOMAIN and GUPPI_USER_POOL_CLIENT_ID, or deploy to write $OUTPUTS"
  GUPPI_AUTH_DOMAIN="${GUPPI_AUTH_DOMAIN:-$(jq -r '.GuppiGpt.AuthDomain' "$OUTPUTS")}"
  GUPPI_USER_POOL_CLIENT_ID="${GUPPI_USER_POOL_CLIENT_ID:-$(jq -r '.GuppiGpt.UserPoolClientId' "$OUTPUTS")}"
fi

# One caller at a time: two concurrent refreshes of the same rotating token would leave
# the file holding a token Cognito has already replaced.
LOCK="$SESSION_FILE.lock"
for _ in $(seq 1 100); do
  mkdir "$LOCK" 2>/dev/null && break
  sleep 0.1
done
[[ -d "$LOCK" ]] || fail "could not take $LOCK"
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

jq -e '.refreshToken | strings' "$SESSION_FILE" >/dev/null || fail "$SESSION_FILE has no refreshToken"

# The form is built by jq from the file and piped to curl; printf is a shell builtin, so
# no token reaches a process argument list.
response="$(
  jq -r --arg client "$GUPPI_USER_POOL_CLIENT_ID" \
    '"grant_type=refresh_token&client_id=\($client | @uri)&refresh_token=\(.refreshToken | @uri)"' \
    "$SESSION_FILE" |
    curl -sS --max-time 30 -X POST "https://$GUPPI_AUTH_DOMAIN/oauth2/token" \
      -H "content-type: application/x-www-form-urlencoded" --data-binary @-
)" || fail "token request to $GUPPI_AUTH_DOMAIN failed"

access_token="$(printf '%s' "$response" | jq -r '.access_token // empty' 2>/dev/null || true)"
if [[ -z "$access_token" ]]; then
  error="$(printf '%s' "$response" | jq -r '.error // "no access_token in the response"' 2>/dev/null ||
    echo "unreadable response")"
  fail "refresh refused: $error (a new refresh token is needed in 1Password)"
fi

if printf '%s' "$response" | jq -e '.refresh_token | strings' >/dev/null 2>&1; then
  tmp="$(mktemp "$SESSION_FILE.XXXXXX")"
  chmod 600 "$tmp"
  printf '%s' "$response" | jq '{refreshToken: .refresh_token}' >"$tmp"
  mv "$tmp" "$SESSION_FILE"
  chmod 600 "$SESSION_FILE"
fi
unset response

printf '%s\n' "$access_token"
