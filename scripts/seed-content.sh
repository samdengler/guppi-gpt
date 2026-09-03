#!/usr/bin/env bash
# Refresh the knowledge base corpus: the MCP specification, Strands Agents, and AG-UI
# documentation, cloned at pinned revisions, reduced to Markdown plus each license, and
# synced to docs/<source>/ in the content bucket. Run scripts/ingest.sh afterwards.
# Usage: scripts/seed-content.sh [bucket]   (default: ContentBucketName from cdk-outputs.json)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
bucket="${1:-$(jq -r '.GuppiGpt.ContentBucketName' "$ROOT/cdk-outputs.json")}"

for tool in git aws jq; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

# name | repository | revision | docs subtree
SOURCES=(
  "mcp|modelcontextprotocol/modelcontextprotocol|5f5440bb26a62e2cf3440b92da5a667efa03b267|docs"
  "strands|strands-agents/docs|658b6f05a2d4cbe8e1b8ffc860915c8dafc6b654|site/src/content/docs"
  "ag-ui|ag-ui-protocol/ag-ui|33b1caf7c9b203fed38bb3017758948f3082dba2|docs"
)

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

for source in "${SOURCES[@]}"; do
  IFS='|' read -r name repo revision subtree <<<"$source"
  work="$stage/clone-$name"
  echo "cloning $repo at ${revision:0:12}, keeping $subtree" >&2
  git clone --quiet --filter=blob:none --no-checkout "https://github.com/$repo.git" "$work"
  git -C "$work" sparse-checkout set --no-cone "/$subtree/" "/LICENSE" "/$(dirname "$subtree")/LICENSE" "/site/LICENSE"
  git -C "$work" checkout --quiet "$revision"

  dest="$stage/docs/$name"
  while IFS= read -r -d '' file; do
    relative="${file#"$work/$subtree/"}"
    target="$dest/${relative%.mdx}"
    [[ "$relative" == *.mdx ]] && target="$target.md"
    mkdir -p "$(dirname "$target")"
    cp "$file" "$target"
  done < <(find "$work/$subtree" -type f \( -name '*.md' -o -name '*.mdx' \) -print0)

  license="$(find "$work" -maxdepth 2 -name LICENSE -type f | head -1 || true)"
  if [[ -n "$license" ]]; then cp "$license" "$dest/LICENSE.txt"; else echo "no LICENSE found in $repo" >&2; fi
  echo "$name: $(find "$dest" -type f | wc -l | tr -d ' ') files" >&2
done

# --size-only: every clone is fresh, so modification times would otherwise force a full
# re-upload and a full re-ingestion on every run. Same-size edits are the accepted miss.
aws s3 sync "$stage/docs" "s3://$bucket/docs" --delete --size-only --only-show-errors
echo "synced to s3://$bucket/docs; run scripts/ingest.sh to index the changes"
