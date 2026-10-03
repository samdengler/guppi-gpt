#!/usr/bin/env bash
# Invite requests for chat.dengler.io (docs/proposals/invites.md).
#
#   scripts/invite.sh list              every request: status, address, name, when
#   scripts/invite.sh approve <email>   a pending request becomes approved (the email's button does the same)
#   scripts/invite.sh revoke <email>    marks it revoked and deletes the Cognito user, so no new sign-in
#                                       works and the current session ends when its token expires (an hour)
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"
TABLE="guppi-gpt-invites"

usage() { sed -n '3,8p' "$0" | sed -e '/^set /d' -e 's/^# \{0,1\}//' >&2; exit 2; }

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
    pool="$(aws cloudformation describe-stacks --stack-name GuppiGpt \
      --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)"
    users="$(aws cognito-idp list-users --user-pool-id "$pool" --filter "email = \"$email\"" \
      --query 'Users[].Username' --output text)"
    for user in $users; do
      aws cognito-idp admin-delete-user --user-pool-id "$pool" --username "$user"
      echo "deleted Cognito user $user"
    done
    echo "revoked $email"
    ;;
  *)
    usage
    ;;
esac
