#!/usr/bin/env bash
# Deploy the GuppiGpt stack with secrets read from 1Password, then publish the page.
# Extra arguments are passed to `cdk deploy` (for example --hotswap or --require-approval never).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ITEM="op://Personal/GuppiGPT Google OAuth"
OUTPUTS="$ROOT/cdk-outputs.json"

for tool in op npx uv aws jq docker; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

export AWS_REGION="${AWS_REGION:-us-east-1}"

param_args=()
if op whoami >/dev/null 2>&1; then
  client_id="$(op read "$ITEM/username")"
  client_secret="$(op read "$ITEM/credential")"
  if [[ -z "$client_id" || -z "$client_secret" ]]; then
    echo "could not read the Google OAuth client from 1Password ($ITEM)" >&2
    echo "check the vault, item title, and field labels with: op item get 'GuppiGPT Google OAuth' --format json | jq '.vault.name, [.fields[].label]'" >&2
    exit 1
  fi
  param_args+=(--parameters "GoogleClientId=$client_id" --parameters "GoogleClientSecret=$client_secret")
else
  echo "not signed in to 1Password (op whoami failed); reusing the stack's existing Google OAuth parameters" >&2
fi
if [[ -n "${GUPPI_ALARM_EMAIL:-}" ]]; then
  param_args+=(--parameters "AlarmEmail=$GUPPI_ALARM_EMAIL")
fi

cd "$ROOT/infra"
npx --yes aws-cdk@2 deploy GuppiGpt \
  "${param_args[@]+"${param_args[@]}"}" \
  --outputs-file "$OUTPUTS" \
  "$@"

cd "$ROOT"
bucket="$(jq -r '.GuppiGpt.SiteBucketName' "$OUTPUTS")"
distribution="$(jq -r '.GuppiGpt.DistributionId' "$OUTPUTS")"

jq '{
  region: "'"$AWS_REGION"'",
  userPoolClientId: .GuppiGpt.UserPoolClientId,
  authDomain: .GuppiGpt.AuthDomain,
  siteUrl: .GuppiGpt.SiteUrl
}' "$OUTPUTS" > web/config.json

aws s3 sync web "s3://$bucket" --delete --exclude '.*'
aws cloudfront create-invalidation --distribution-id "$distribution" --paths '/*' >/dev/null
echo "published $(jq -r .siteUrl web/config.json)"
