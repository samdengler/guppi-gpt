#!/usr/bin/env bash
# Deploy the GuppiMcpApp stack, then publish web/ to the platform's site bucket under
# projects/mcp-app/ and invalidate that prefix. The stack has no secret parameters, so no
# 1Password read. Extra arguments are passed to `cdk deploy` (for example
# --require-approval never). `--site-only` skips cdk deploy (and the image build and push)
# and only publishes web/.
set -euo pipefail

SITE_ONLY=0
if [[ "${1:-}" == "--site-only" ]]; then
  SITE_ONLY=1
  shift
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="mcp-app"
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

tools=(aws)
[[ "$SITE_ONLY" == 0 ]] && tools+=(npx uv docker)
for tool in "${tools[@]}"; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

export AWS_REGION="${AWS_REGION:-us-east-1}"

if [[ "$SITE_ONLY" == 1 ]]; then
  echo "site only: skipping cdk deploy"
else
  cd "$ROOT/infra"
  npx --yes aws-cdk@2 deploy GuppiMcpApp --outputs-file "$OUTPUTS" "$@"
fi

cd "$ROOT"
platform_parameter() {
  aws ssm get-parameter --name "/guppi/platform/$1" --query Parameter.Value --output text
}
bucket="$(platform_parameter site-bucket-name)"
distribution="$(platform_parameter distribution-id)"

# Only this project's prefix is written, and --delete never reaches outside it. The
# manifest is read by the page with cache: "no-store"; the header keeps CloudFront and the
# browser from holding an old one.
aws s3 sync web "s3://$bucket/projects/$PROJECT/" --delete --exclude '.*' \
  --cache-control no-store
aws cloudfront create-invalidation --distribution-id "$distribution" \
  --paths "/projects/$PROJECT/*" >/dev/null
echo "published $(platform_parameter site-url)p/$PROJECT/"
