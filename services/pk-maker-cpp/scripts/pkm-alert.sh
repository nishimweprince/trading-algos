#!/usr/bin/env bash
# pkm's failure alert, run by systemd as ExecStopPost on every stop. It sends a
# Telegram message only when the stop was not clean (a crash, a kill, a failed
# start), so a `systemctl stop` or a deploy restart stays quiet. Restart=always
# keeps the unit out of the "failed" state, which is why OnFailure= never fired.
#
# Reads from the unit's environment (/etc/pkm.env):
#   PK_TELEGRAM_BOT_TOKEN       bot token; unset = log only
#   PK_TELEGRAM_ADMIN_USER_IDS  comma-separated chat ids
# One alert per 10 minutes at most; the rest are counted in the next one.
set -u

result="${SERVICE_RESULT:-unknown}"
[ "$result" = "success" ] && exit 0

unit="pkm.service"
host="$(hostname)"
state="${PKM_ALERT_STATE:-data/alert.last}"
now="$(date +%s)"
cooldown=600

last=0
suppressed=0
if [ -r "$state" ]; then
  read -r last suppressed < "$state" || true
fi
if [ $((now - ${last:-0})) -lt "$cooldown" ]; then
  echo "$last $(( ${suppressed:-0} + 1 ))" > "$state" 2>/dev/null || true
  echo "pkm-alert: $result (suppressed, cooldown)" >&2
  exit 0
fi
echo "$now 0" > "$state" 2>/dev/null || true

text="pkm FAILED on ${host}
result ${result}, exit ${EXIT_CODE:-?}/${EXIT_STATUS:-?}"
if [ "${suppressed:-0}" -gt 0 ]; then
  text="${text}
${suppressed} more failure(s) since the last alert"
fi
logs="$(journalctl -u "$unit" -n 12 --no-pager -o cat 2>/dev/null | cut -c1-300)"
if [ -n "$logs" ]; then
  text="${text}

${logs}"
fi
text="${text}

Check: journalctl -u ${unit} -n 50 --no-pager"

token="${PK_TELEGRAM_BOT_TOKEN:-}"
chats="${PK_TELEGRAM_ADMIN_USER_IDS:-}"
if [ -z "$token" ] || [ -z "$chats" ]; then
  echo "pkm-alert: $result; no destination (set PK_TELEGRAM_BOT_TOKEN and PK_TELEGRAM_ADMIN_USER_IDS in /etc/pkm.env)" >&2
  exit 0
fi
IFS=',' read -ra ids <<< "$chats"
for id in "${ids[@]}"; do
  id="${id// /}"
  [ -z "$id" ] && continue
  curl -sS -m 10 -o /dev/null "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${id}" --data-urlencode "text=${text:0:3900}" \
    || echo "pkm-alert: telegram send to ${id} failed" >&2
done
exit 0
