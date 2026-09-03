#!/usr/bin/env bash
# Deploy the GuppiGpt stack with secrets read from 1Password, then publish the page.
# Extra arguments are passed to `cdk deploy` (for example --hotswap or --require-approval never).
# `--site-only` skips cdk deploy (and the image build and push) and publishes the page
# from the outputs of the last deploy; useful on a slow connection.
set -euo pipefail

SITE_ONLY=0
if [[ "${1:-}" == "--site-only" ]]; then SITE_ONLY=1; shift; fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ITEM="op://Personal/GuppiGPT Google OAuth"
OUTPUTS="$ROOT/cdk-outputs.json"

# Everything below is also written to .deploy/deploy-<timestamp>.log (gitignored), with
# .deploy/latest.log pointing at the newest run, so another session can follow a deploy
# started from any terminal. The last line of a run is always "deploy exit=<code>".
LOG_DIR="$ROOT/.deploy"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/deploy-$(date +%Y%m%d-%H%M%S).log"
ln -sfn "$(basename "$LOG")" "$LOG_DIR/latest.log"
exec > >(tee -a "$LOG") 2>&1
trap 'code=$?; echo "deploy exit=$code"; exit $code' EXIT
echo "deploy log: $LOG"

for tool in op npx npm uv aws jq docker; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
if [[ "$SITE_ONLY" == 1 && ! -f "$OUTPUTS" ]]; then
  echo "--site-only needs $OUTPUTS from an earlier deploy" >&2
  exit 1
fi

export AWS_REGION="${AWS_REGION:-us-east-1}"

param_args=()
if [[ "$SITE_ONLY" == 1 ]]; then
  echo "site only: skipping cdk deploy, using $OUTPUTS"
elif op whoami >/dev/null 2>&1; then
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

if [[ "$SITE_ONLY" == 0 ]]; then
  cd "$ROOT/infra"
  npx --yes aws-cdk@2 deploy GuppiGpt \
    "${param_args[@]+"${param_args[@]}"}" \
    --outputs-file "$OUTPUTS" \
    "$@"
fi

cd "$ROOT/web"
npm ci --silent --no-audit --no-fund
npm run build --silent

cd "$ROOT"
bucket="$(jq -r '.GuppiGpt.SiteBucketName' "$OUTPUTS")"
distribution="$(jq -r '.GuppiGpt.DistributionId' "$OUTPUTS")"

jq --slurpfile features web/features.json '{
  region: "'"$AWS_REGION"'",
  userPoolClientId: .GuppiGpt.UserPoolClientId,
  authDomain: .GuppiGpt.AuthDomain,
  siteUrl: .GuppiGpt.SiteUrl,
  features: $features[0]
}' "$OUTPUTS" > web/dist/config.json

aws s3 sync web/dist "s3://$bucket" --delete --exclude '.*'
aws cloudfront create-invalidation --distribution-id "$distribution" --paths '/*' >/dev/null
echo "published $(jq -r .siteUrl web/dist/config.json)"
