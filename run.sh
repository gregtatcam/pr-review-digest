#!/bin/zsh
# Restart the PR digest web server in the background and open its page.
# http://127.0.0.1:8765/
cd "$(dirname "$0")"
pkill -f my_digest_web.py
nohup ./my_digest_web.sh --no-browser > /tmp/pr-review-digest.log 2>&1 &
# Give the server a moment to start listening before opening the page.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.3
    curl -s -o /dev/null http://127.0.0.1:8765/ && break
done
open http://127.0.0.1:8765/
