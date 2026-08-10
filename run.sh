#!/usr/bin/env bash
# Start the DD Library web app.
#
#   ./run.sh                 # http://127.0.0.1:8000
#   DD_PORT=9000 ./run.sh
#   DD_HOST=0.0.0.0 ./run.sh # only behind an authenticating proxy — see README
set -euo pipefail
cd "$(dirname "$0")"

if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
  if ! compgen -G "${ANTHROPIC_CONFIG_DIR:-$HOME/.config/anthropic}/credentials/*.json" > /dev/null; then
    echo "warning: no Anthropic credentials found." >&2
    echo "         Ingest and search work; indexing and Ask need a key — add one in" >&2
    echo "         Admin -> API keys, or set ANTHROPIC_API_KEY." >&2
  fi
fi

exec python3 -m app.server
