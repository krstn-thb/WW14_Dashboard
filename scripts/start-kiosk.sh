#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

TARGET_URL="${DASHBOARD_URL:-http://127.0.0.1:8080}"

for _ in $(seq 1 60); do
  if curl --silent --fail --max-time 2 "$TARGET_URL/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if command -v chromium >/dev/null 2>&1; then
  BROWSER="chromium"
elif command -v chromium-browser >/dev/null 2>&1; then
  BROWSER="chromium-browser"
else
  echo "Chromium wurde nicht gefunden." >&2
  exit 1
fi

exec "$BROWSER" \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --disable-session-crashed-bubble \
  --check-for-update-interval=31536000 \
  "$TARGET_URL"

