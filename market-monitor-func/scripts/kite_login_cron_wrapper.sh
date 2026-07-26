#!/usr/bin/env bash
# Cron entry point for the daily Kite login. Runs the Playwright login
# script and sends a Telegram alert if it fails, so a broken automated
# login (Kite UI change, expired password, TOTP drift, etc.) surfaces
# immediately instead of silently leaving the Function App without a
# valid session all day.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.kite_env"
VENV_PYTHON="$SCRIPT_DIR/../.venv/bin/python3"
LOGIN_SCRIPT="$SCRIPT_DIR/playwright_kite_login.py"

# shellcheck source=/dev/null
source "$ENV_FILE"

output="$("$VENV_PYTHON" "$LOGIN_SCRIPT" 2>&1)"
status=$?

echo "$output"

if [ "$status" -ne 0 ]; then
    tail_output="$(echo "$output" | tail -c 800)"
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=⚠️ Daily Kite login FAILED ($(date -u '+%Y-%m-%d %H:%M UTC')). Function App will run without a valid Kite session today.

${tail_output}" \
        > /dev/null
fi

exit "$status"
