#!/usr/bin/env bash
# Deploy the GuppiGpt stack with secrets read from 1Password, then publish the page.
# Extra arguments are passed to `cdk deploy` (for example --hotswap or --require-approval never).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ITEM="op://Private/GuppiGPT Google OAuth"
OUTPUTS="$ROOT/cdk-outputs.json"

for tool in op npx uv aws jq docker; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

export AWS_REGION="${AWS_REGION:-us-east-1}"

cd "$ROOT/infra"
npx --yes aws-cdk@2 deploy GuppiGpt \
  --parameters "GoogleClientId=$(op read "$ITEM/client id")" \
  --parameters "GoogleClientSecret=$(op read "$ITEM/client secret")" \
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
