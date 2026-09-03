#!/usr/bin/env bash
# Start one ingestion job for the knowledge base data source and wait for it to finish.
# Usage: scripts/ingest.sh   (reads KnowledgeBaseId and DataSourceId from cdk-outputs.json)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION="${AWS_REGION:-us-east-1}"
kb="$(jq -r '.GuppiGpt.KnowledgeBaseId' "$ROOT/cdk-outputs.json")"
ds="$(jq -r '.GuppiGpt.DataSourceId' "$ROOT/cdk-outputs.json")"

job="$(aws bedrock-agent start-ingestion-job --knowledge-base-id "$kb" --data-source-id "$ds" \
  --query 'ingestionJob.ingestionJobId' --output text)"
echo "ingestion job $job started" >&2

while true; do
  state="$(aws bedrock-agent get-ingestion-job --knowledge-base-id "$kb" --data-source-id "$ds" \
    --ingestion-job-id "$job" --query 'ingestionJob' --output json)"
  status="$(jq -r .status <<<"$state")"
  case "$status" in
    COMPLETE)
      jq -c '.statistics' <<<"$state"
      exit 0 ;;
    FAILED|STOPPED)
      jq -r '.failureReasons[]?' <<<"$state" >&2
      echo "ingestion $status" >&2
      exit 1 ;;
    *)
      sleep 15 ;;
  esac
done
