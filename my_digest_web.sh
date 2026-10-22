#!/usr/bin/env bash
# Serve the PR digest as a web page and open it. Arguments are passed to
# my_digest_web.py, e.g.
#   ./my_digest_web.sh --port 9000
set -euo pipefail

if [[ -z "${GH_TOKEN:-}" ]]; then
    GH_TOKEN="$(gh auth token)"
    export GH_TOKEN
fi

exec python3 "$(dirname "$0")/my_digest_web.py" "$@"
