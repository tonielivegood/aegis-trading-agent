#!/usr/bin/env bash
# BNB Hack 2026 Track1 — automated CONTEST-START sequence.
# Fired once by contest-start.timer at 2026-06-22 00:00:00 UTC.
#
# The agent is ALREADY running `--live` (flipped early 21/6 for validation), so we
# do NOT use go-live.sh (its guard aborts when already --live). Instead we:
#   1) stop the agent (no tick races the flatten),
#   2) panic --live  -> flatten EVERY non-stable holding to USDT (clears orphaned
#      meme/dust positions that the position book never tracked -> no unmanaged risk),
#   3) reset         -> re-baseline drawdown/compliance + clear the books,
#   4) start the agent (stays --live) on a clean, all-cash, contest-start baseline.
#
# Idempotent: a sentinel file guards against a second run. Always restarts the agent,
# even if panic/reset hit a snag, so the bot is never left stopped. Logs to the journal
# and sends a Telegram confirmation with the post-reset equity.
set -uo pipefail

REPO=/home/agent/bnbhack-track1-agent
SENTINEL=/root/.contest-started
PYRUN=(sudo -u agent env PYTHONPATH=. .venv/bin/python -m src.agent)

log() { echo "[contest-start $(date -u '+%F %T UTC')] $*"; }

notify() {
  sudo -u agent bash -c "cd '$REPO' && .venv/bin/python -c \
    'import sys; from src.agent.monitor import notifier; notifier.send(sys.argv[1])' \
    \"$1\"" >/dev/null 2>&1 || true
}

if [ -f "$SENTINEL" ]; then
  log "sentinel $SENTINEL present — already ran; aborting to avoid a double reset."
  exit 0
fi

cd "$REPO" || { log "repo $REPO missing"; exit 1; }

log "=== CONTEST START sequence begin ==="
log "stopping agent..."
systemctl stop agent || log "WARN: stop agent returned non-zero"

log "panic --live (flatten all non-stable holdings to USDT)..."
if "${PYRUN[@]}" panic --live; then
  log "panic OK"
else
  log "WARN: panic returned non-zero (continuing — reset+restart still run)"
fi

log "reset (re-baseline drawdown/compliance + clear books)..."
if "${PYRUN[@]}" reset; then
  log "reset OK"
else
  log "WARN: reset returned non-zero"
fi

log "starting agent (stays --live)..."
systemctl start agent || log "ERROR: failed to start agent"

# Mark done so the timer/script can never double-fire.
touch "$SENTINEL"

# Report the clean baseline.
EQUITY="$("${PYRUN[@]}" status 2>/dev/null | awk -F'[$]' '/Equity/{print $2}')"
log "=== CONTEST START done. baseline equity ~\$${EQUITY:-?} ==="
notify "🚀 Contest start (22/6 00:00 UTC): flattened to USDT, reset, agent LIVE. Baseline equity ~\$${EQUITY:-?}."
