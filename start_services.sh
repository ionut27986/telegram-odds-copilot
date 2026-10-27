
#!/bin/sh
set -eu

python -u internal_api.py &
API_PID=$!

cleanup() {
    kill "$API_PID" 2>/dev/null || true
}

trap cleanup EXIT

python -u bot.py
