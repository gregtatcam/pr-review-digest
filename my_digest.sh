#!/usr/bin/env bash
# Print open PRs related to me. Arguments are passed to my_digest.py, e.g.
#   ./my_digest.sh --milestone "2.5.0,2.6.0" --date 10-5-26
set -euo pipefail

if [[ -z "${GH_TOKEN:-}" ]]; then
    GH_TOKEN="$(gh auth token)"
    export GH_TOKEN
fi

# Print locally; never post a personal digest to Slack.
unset SLACK_BOT_TOKEN SLACK_CHANNEL_ID SLACK_WEBHOOK_URL

exec python3 "$(dirname "$0")/my_digest.py" "$@"
