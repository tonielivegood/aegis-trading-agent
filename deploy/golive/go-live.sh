#!/usr/bin/env bash
# BNB Hack 2026 Track1 — one-command GO-LIVE (run as root on the VPS)
# Wraps the runbook with safety pre-flight checks + an explicit human confirmation.
set -euo pipefail

REPO=/home/agent/bnbhack-track1-agent
SVC=/etc/systemd/system/agent.service

echo "================================================================"
echo " BNB Hack Track1 — GO-LIVE  ($(date -u '+%Y-%m-%d %H:%M:%S UTC'))"
echo "================================================================"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Run as root (the service/sed steps need it).  Try: sudo bash $0"
  exit 1
fi

# Guard: don't double-flip if already live.
if grep -q -- '--live' "$SVC"; then
  echo "⚠️  Service is ALREADY configured with --live."
  echo "   Looks like go-live already happened. Aborting to avoid a double reset."
  echo "   Current ExecStart:"
  grep ExecStart "$SVC" | sed 's/^/     /'
  exit 1
fi

# Pre-flight: show agent status (wallet balances / equity baseline).
echo
echo "--- Pre-flight: agent status (check wallet is funded) ---"
if ! sudo -u agent bash -c "cd '$REPO' && .venv/bin/python -m src.agent status"; then
  echo "❌ 'src.agent status' failed. Investigate before going live."
  exit 1
fi

# Manual checklist + confirmation.
cat <<'CHECKLIST'

--- Manual checklist (confirm each is TRUE) ---
  [ ] Wallet funded enough for trading (see balances above)
  [ ] DoraHacks BUIDL submitted
  [ ] GitHub repo is PUBLIC
  [ ] Trading window is actually OPEN (per DoraHacks official time)

CHECKLIST

read -r -p 'Flip agent to LIVE trading now? Type EXACTLY  GO-LIVE  to proceed > ' CONFIRM
if [ "$CONFIRM" != "GO-LIVE" ]; then
  echo "Aborted. Nothing was changed."
  exit 1
fi

# Backup the unit file before editing.
BAK="${SVC}.bak.$(date -u '+%Y%m%dT%H%M%SZ')"
cp "$SVC" "$BAK"
echo "Backed up unit file -> $BAK"

echo "1/5 stop agent";            systemctl stop agent
echo "2/5 reset state";          sudo -u agent bash -c "cd '$REPO' && .venv/bin/python -m src.agent reset"
echo "3/5 enable --live";        sed -i 's#src.agent run#src.agent run --live#' "$SVC"
echo "4/5 daemon-reload";        systemctl daemon-reload
echo "5/5 restart agent";        systemctl restart agent

sleep 3
echo
echo "--- Result ---"
echo "ExecStart now: $(grep ExecStart "$SVC")"
echo "Service active: $(systemctl is-active agent)"
echo
journalctl -u agent -n 25 --no-pager || true
echo
echo "✅ Done. Agent is LIVE. Follow logs with:  journalctl -u agent -f"
echo "   (Telegram should resume hourly heartbeats.)"
