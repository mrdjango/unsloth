#!/usr/bin/env bash
# Build the DeepTenLab UI (when stale) and start the server that serves both the API and the UI.
#
# Usage:  ./deeptenlab_run.sh [--rebuild] [--port N] [--host H]
#   --rebuild   force a frontend build even when dist/ looks current
#   --port N    port to listen on          (default: $PORT or 8888)
#   --host H    address to bind            (default: $HOST or 127.0.0.1; 0.0.0.0 exposes it on your network)
#
# Environment:
#   PYTHON               Python with the studio dependencies (default: the installed studio venv, else python3)
#   UNSLOTH_STUDIO_HOME  data directory (accounts, settings, model catalog); set it to keep test data apart
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND="$ROOT/studio/frontend"
BACKEND="$ROOT/studio/backend"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8888}"
REBUILD=0

while [ $# -gt 0 ]; do
    case "$1" in
        --rebuild) REBUILD=1 ;;
        --port) PORT="${2:?--port needs a value}"; shift ;;
        --host) HOST="${2:?--host needs a value}"; shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

if [ -z "${PYTHON:-}" ]; then
    if [ -x "$HOME/.unsloth/studio/unsloth_studio/bin/python" ]; then
        PYTHON="$HOME/.unsloth/studio/unsloth_studio/bin/python"
    else
        PYTHON="$(command -v python3 || true)"
    fi
fi
[ -n "$PYTHON" ] || { echo "No Python found. Set PYTHON=/path/to/python." >&2; exit 1; }
"$PYTHON" -c "import fastapi, uvicorn" 2>/dev/null || {
    echo "$PYTHON is missing the studio dependencies (fastapi, uvicorn)." >&2
    echo "Install Unsloth Studio first, or point PYTHON at an environment that has them." >&2
    exit 1
}

command -v node >/dev/null || { echo "Node.js is required to build the UI." >&2; exit 1; }

needs_build() {
    [ "$REBUILD" = 1 ] && return 0
    [ -f "$FRONTEND/dist/index.html" ] || return 0
    # Stale when any source file is newer than the last build.
    [ -n "$(find "$FRONTEND/src" "$FRONTEND/public" "$FRONTEND/index.html" "$FRONTEND/package.json" \
        -newer "$FRONTEND/dist/index.html" -type f -print -quit 2>/dev/null)" ]
}

if needs_build; then
    echo "==> Building the UI"
    cd "$FRONTEND"
    [ -d node_modules ] || npm install --no-audit --no-fund
    npx vite build
else
    echo "==> UI is up to date (use --rebuild to force a build)"
fi

echo "==> Starting DeepTenLab on http://$HOST:$PORT"
echo "    Owner username: unsloth (first-run password: <data dir>/auth/.bootstrap_password)"
echo "    Admin panel:    http://$HOST:$PORT/admin"
cd "$BACKEND"
exec "$PYTHON" run.py --host "$HOST" --port "$PORT"
