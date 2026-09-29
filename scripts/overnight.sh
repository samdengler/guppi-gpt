#!/usr/bin/env bash
# Runs platform phases 1 to 4 unattended: one `claude -p` session per phase, in the
# repository each phase belongs to, each told to read its brief. A phase that exits
# non-zero (a usage-limit pause is the usual cause) is retried after thirty minutes, up
# to four attempts; the briefs are written to resume from their reports. Phases 2 to 4
# depend on the one before, so a phase that fails every attempt ends the run.
#
# Before starting: `aws sts get-caller-identity` works and will keep working overnight,
# Colima is running, `gh auth status` is good, and a 1Password API Credential item titled
# "GuppiGPT Test Session" holds a refresh token from a signed-in chat.dengler.io tab in
# its `credential` field. Run under caffeinate inside tmux:
#   caffeinate -i scripts/overnight.sh
# Everything is appended to .deploy/overnight-<timestamp>.log. Per-phase detail is in each
# repository's git log, its .deploy/latest.log, and its phase report.
set -uo pipefail

SRC="$HOME/src/github.com/samdengler"
GUPPI="$SRC/guppi-gpt"
MCPAPP="$SRC/guppi-mcp-app"
LOG_DIR="$GUPPI/.deploy"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/overnight-$(date +%Y%m%d-%H%M%S).log"
say() { echo "=== $(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

for tool in claude op jq aws docker gh; do
  command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done

# The test session: read once from 1Password while Sam is present, then kept in a
# mode 600 file outside every repository that scripts/test-token.sh rotates in place.
SESSION_DIR="$HOME/.config/guppi"
SESSION_FILE="$SESSION_DIR/test-session.json"
mkdir -p "$SESSION_DIR"
chmod 700 "$SESSION_DIR"
refresh_token="$(op read 'op://Personal/GuppiGPT Test Session/credential')" || {
  echo "could not read the test session from 1Password" >&2; exit 1; }
jq -n --arg t "$refresh_token" '{refreshToken: $t}' > "$SESSION_FILE"
chmod 600 "$SESSION_FILE"
unset refresh_token
export GUPPI_USER_POOL_CLIENT_ID="$(jq -r '.GuppiGpt.UserPoolClientId' "$GUPPI/cdk-outputs.json")"
export GUPPI_AUTH_DOMAIN="$(jq -r '.GuppiGpt.AuthDomain' "$GUPPI/cdk-outputs.json")"
export AWS_REGION="${AWS_REGION:-us-east-1}"

phase() {
  local dir="$1" brief="$2" label="$3" attempt
  for attempt in 1 2 3 4; do
    say "$label: attempt $attempt in $dir"
    (cd "$dir" && claude -p \
      "Read $brief and carry out that phase exactly as it describes. This run is unattended: follow the brief's rules for decisions, blockers and resuming, and never wait for an answer. If the phase's report already exists, continue from its first unfinished step." \
      --dangerously-skip-permissions --output-format text) 2>&1 | tee -a "$LOG"
    if [[ "${PIPESTATUS[0]}" -eq 0 ]]; then say "$label: finished"; return 0; fi
    say "$label: exited non-zero; waiting 30 minutes"
    sleep 1800
  done
  say "$label: gave up after 4 attempts"
  return 1
}

say "overnight run starting; log $LOG"
phase "$GUPPI"  docs/proposals/platform-phase-1.md "phase 1" || exit 1
phase "$MCPAPP" docs/phase-2.md                    "phase 2" || exit 1
phase "$GUPPI"  docs/proposals/platform-phase-3.md "phase 3" || exit 1
phase "$MCPAPP" docs/phase-4.md                    "phase 4"
say "overnight run done"
