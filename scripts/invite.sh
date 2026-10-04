#!/usr/bin/env bash
# Invite requests for chat.dengler.io (docs/proposals/invites.md).
#
#   scripts/invite.sh list              every request: status, address, name, when
#   scripts/invite.sh approve <email>   a pending request becomes approved (the email's button does the same)
#   scripts/invite.sh grant <email>     approves an address directly, with or without a request
#                                       (an approval adds the person to Okta's chat-users)
#   scripts/invite.sh revoke <email>    marks it revoked and removes the person from chat-users, so no
#                                       new sign-in works and the current session ends when its token
#                                       expires (an hour); the Okta API token comes from 1Password
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"
TABLE="guppi-gpt-invites"

usage() { sed -n '3,9p' "$0" | sed -e '/^set /d' -e 's/^# \{0,1\}//' >&2; exit 2; }

now_ms() { python3 -c 'import time; print(int(time.time() * 1000))'; }

lower() { tr '[:upper:]' '[:lower:]' <<<"$1" | tr -d '[:space:]'; }

key() { printf '{"email":{"S":"%s"}}' "$1"; }

cmd="${1:-}"
case "$cmd" in
  list)
    aws dynamodb scan --table-name "$TABLE" --output json \
      | python3 -c '
import json, sys, datetime
items = json.load(sys.stdin)["Items"]
items.sort(key=lambda i: (i.get("status", {}).get("S", ""), -int(i.get("requestedAt", {}).get("N", "0"))))
for i in items:
    when = int(i.get("requestedAt", {}).get("N", "0")) / 1000
    stamp = datetime.datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M") if when else "-"
    status, email = i["status"]["S"], i["email"]["S"]
    name = i.get("name", {}).get("S", "")
    print(f"{status:9} {email:40} {name:24} {stamp}")
print(f"{len(items)} request(s)")
'
    ;;
  approve)
    [[ $# -eq 2 ]] || usage
    email="$(lower "$2")"
    aws dynamodb update-item --table-name "$TABLE" --key "$(key "$email")" \
      --update-expression "SET #s = :approved, decidedAt = :now" \
      --condition-expression "#s = :pending" \
      --expression-attribute-names '{"#s":"status"}' \
      --expression-attribute-values "{\":approved\":{\"S\":\"approved\"},\":pending\":{\"S\":\"pending\"},\":now\":{\"N\":\"$(now_ms)\"}}" \
      >/dev/null 2>&1 \
      || { echo "not approved: $email has no pending request" >&2; exit 1; }
    echo "approved $email"
    ;;
  grant)
    [[ $# -eq 2 ]] || usage
    email="$(lower "$2")"
    # Approves whatever is there (a pending or revoked request) or creates the item. A new
    # item has no request token, so the approval page never matches it. A pending request
    # granted this way gets the "you're in" email, as from the approval page; a new item
    # gets nothing, since the Pipe passes only new pending items and pending-to-approved.
    aws dynamodb update-item --table-name "$TABLE" --key "$(key "$email")" \
      --update-expression "SET #s = :approved, decidedAt = :now, requestedAt = if_not_exists(requestedAt, :now), #n = if_not_exists(#n, :granted)" \
      --expression-attribute-names '{"#s":"status","#n":"name"}' \
      --expression-attribute-values "{\":approved\":{\"S\":\"approved\"},\":now\":{\"N\":\"$(now_ms)\"},\":granted\":{\"S\":\"(granted)\"}}" \
      >/dev/null
    echo "granted $email"
    ;;
  revoke)
    [[ $# -eq 2 ]] || usage
    email="$(lower "$2")"
    aws dynamodb update-item --table-name "$TABLE" --key "$(key "$email")" \
      --update-expression "SET #s = :revoked, decidedAt = :now" \
      --condition-expression "attribute_exists(email)" \
      --expression-attribute-names '{"#s":"status"}' \
      --expression-attribute-values "{\":revoked\":{\"S\":\"revoked\"},\":now\":{\"N\":\"$(now_ms)\"}}" \
      >/dev/null 2>&1 \
      || { echo "no request for $email" >&2; exit 1; }
    # The token stays inside python: read from 1Password, never an argument or a file.
    EMAIL="$email" python3 - <<'PY'
import json, os, subprocess, urllib.error, urllib.parse, urllib.request
read = lambda f: subprocess.run(["op", "read", f"op://Personal/Okta API token/{f}"], check=True,
                                capture_output=True, text=True).stdout.strip()
host, token = read("hostname"), read("credential")
group = subprocess.run(["aws", "ssm", "get-parameter", "--name", "/guppi/okta/group-id", "--query",
                        "Parameter.Value", "--output", "text"], check=True, capture_output=True, text=True).stdout.strip()
def call(method, path):
    req = urllib.request.Request(f"https://{host}{path}", method=method,
                                 headers={"Authorization": f"SSWS {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else None
email = os.environ["EMAIL"]
try:
    user = call("GET", f"/api/v1/users/{urllib.parse.quote(email)}")
except urllib.error.HTTPError as e:
    print(f"no Okta user for {email} (HTTP {e.code})")
else:
    call("DELETE", f"/api/v1/groups/{group}/users/{user['id']}")
    print(f"removed {email} from chat-users")
PY
    echo "revoked $email"
    ;;
  *)
    usage
    ;;
esac
