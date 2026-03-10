#!/usr/bin/env bash
# Start Chrome/Chromium with remote debugging enabled.
# This lets the collector connect to your REAL browser session (not a bot).
#
# Usage: ./start_chrome.sh [port]

PORT="${1:-9222}"

# Detect Chrome binary
if command -v google-chrome &>/dev/null; then
    CHROME="google-chrome"
elif command -v google-chrome-stable &>/dev/null; then
    CHROME="google-chrome-stable"
elif command -v chromium-browser &>/dev/null; then
    CHROME="chromium-browser"
elif command -v chromium &>/dev/null; then
    CHROME="chromium"
else
    echo "Error: Chrome/Chromium not found. Please install it first."
    exit 1
fi

echo "Starting ${CHROME} with remote debugging on port ${PORT}..."
echo ""
echo "Steps:"
echo "  1. Chrome will open. Log into Facebook normally."
echo "  2. Navigate to your private group page."
echo "  3. In another terminal, run:  uv run python collector.py --port ${PORT}"
echo ""

"${CHROME}" \
    --remote-debugging-port="${PORT}" \
    --user-data-dir="${HOME}/.config/chrome-debug-profile" \
    --no-first-run \
    --no-default-browser-check \
    "https://www.facebook.com"
