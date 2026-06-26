#!/usr/bin/env bash
# Sends a Telegram go-live reminder. Reads bot token + chat id from the agent .env.
# Usage: golive-remind.sh <t12|t2|t0>
set -euo pipefail

ENVF=/home/agent/bnbhack-track1-agent/.env
TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENVF" | head -1 | cut -d= -f2- | tr -d '"'"'"'\r' | xargs)
CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENVF" | head -1 | cut -d= -f2- | tr -d '"'"'"'\r' | xargs)

case "${1:-}" in
  t12) MSG=$'⏰ <b>BNB Hack go-live trong ~12h</b>\nMốc: 00:00 UTC 22/6 (07:00 sáng VN).\n\nChecklist:\n• Ví đã nạp đủ tiền?\n• Repo GitHub đã PUBLIC?\n• DoraHacks đã submit?\n• Giờ window chính xác đã xác nhận với DoraHacks?\n\nKhi tới giờ:\n<code>ssh -i &lt;ssh-key&gt; &lt;user&gt;@&lt;vps-host&gt;</code>\n<code>bash ~/go-live.sh</code>' ;;
  t2)  MSG=$'⏰ <b>Còn ~2h tới GO-LIVE</b> (00:00 UTC 22/6).\nChuẩn bị SSH vào VPS.\nLệnh: <code>bash /root/go-live.sh</code>' ;;
  t0)  MSG=$'🚀 <b>ĐẾN GIỜ GO-LIVE</b> (00:00 UTC 22/6)!\nSSH vào VPS và chạy NGAY:\n<code>bash /root/go-live.sh</code>\nSau đó theo dõi: <code>journalctl -u agent -f</code>' ;;
  *)   echo "usage: $0 <t12|t2|t0>"; exit 2 ;;
esac

if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
  echo "Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in $ENVF" >&2
  exit 1
fi

curl -fsS -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${CHAT}" \
  --data-urlencode "text=${MSG}" \
  -d parse_mode=HTML -d disable_web_page_preview=true >/dev/null \
  && echo "reminder $1 sent"
