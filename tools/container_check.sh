#!/usr/bin/env bash
# Reproduce the container's runtime constraints without Docker.
#
# The image runs the app with a read-only /app and only /data writable, as an
# unprivileged user. Those are exactly the conditions that break an app which
# quietly writes next to its source, so this recreates them and checks the app
# still starts, bootstraps its admin, and can run its Python tool.
#
#   tools/container_check.sh              # read-only app dir, current user
#   sudo tools/container_check.sh --user  # also drop to an unprivileged user
#
# Useful anywhere a base image cannot be pulled, and as a check that nothing has
# started writing into the source tree.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=$(mktemp -d /tmp/ddcheck-app-XXXXXX)
DATA_DIR=$(mktemp -d /tmp/ddcheck-data-XXXXXX)
HOME_DIR=$(mktemp -d /tmp/ddcheck-home-XXXXXX)
# A free ephemeral port, so a leftover server from an earlier run cannot answer
# these checks and make them pass against the wrong data directory.
PORT="${DD_PORT:-$(python3 -c 'import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')}"
DROP_USER=0
[[ "${1:-}" == "--user" ]] && DROP_USER=1

cleanup() {
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null || true
  chmod -R u+w "$APP_DIR" 2>/dev/null || true
  rm -rf "$APP_DIR" "$DATA_DIR" "$HOME_DIR"
  [[ $DROP_USER -eq 1 ]] && userdel ddcheck 2>/dev/null || true
}
trap cleanup EXIT

cp -r "$SRC/app" "$SRC/web" "$SRC/tools" "$SRC/requirements.txt" "$APP_DIR/"
# mktemp -d is 0700, so grant traversal first and then take write away — the
# result is what `COPY` + `chmod -R a-w /app` produces in the image.
chmod -R a+rX "$APP_DIR"; chmod 755 "$APP_DIR"; chmod -R a-w "$APP_DIR"

RUNNER=""
if [[ $DROP_USER -eq 1 ]]; then
  if [[ $EUID -ne 0 ]]; then echo "--user needs root" >&2; exit 2; fi
  id -u ddcheck >/dev/null 2>&1 || useradd --system --no-create-home ddcheck
  chown -R ddcheck "$DATA_DIR" "$HOME_DIR"
  if ! su -s /bin/bash ddcheck -c "python3 -c 'import anthropic, fastapi' " 2>/dev/null; then
    echo "note: the Python dependencies are not readable by other users on this host"
    echo "      (commonly ~/.local installs). Skipping the drop-to-user part; the"
    echo "      image installs them system-wide as root, so this does not apply there."
    DROP_USER=0
  else
    RUNNER="su -s /bin/bash ddcheck -c"
  fi
fi

# HOME is only redirected when dropping to another user (the image gives that
# user a small tmpfs home). Keeping the caller's HOME otherwise matters: on many
# hosts the Python dependencies live under ~/.local, and overriding HOME would
# take them off sys.path and produce a misleading failure.
[[ $DROP_USER -eq 1 ]] && HOME_ARG="HOME=$HOME_DIR" || HOME_ARG="HOME=$HOME"

ENV_ARGS=(
  "DD_DATA_DIR=$DATA_DIR" "$HOME_ARG"
  "DD_SECRET_KEY=container-check-secret"
  "DD_ADMIN_USER=checker" "DD_ADMIN_PASSWORD=container-check-pw"
  "DD_HOST=127.0.0.1" "DD_PORT=$PORT"
  "DD_BROWSE_ROOTS=$DATA_DIR/uploads/extracted"
  "PYTHONDONTWRITEBYTECODE=1" "PATH=$PATH"
)

run_in_app() {  # run a command with the app dir as cwd, as the chosen user
  if [[ -n "$RUNNER" ]]; then
    $RUNNER "cd '$APP_DIR' && env ${ENV_ARGS[*]} $1"
  else
    (cd "$APP_DIR" && env "${ENV_ARGS[@]}" bash -c "$1")
  fi
}

echo "app dir (read-only): $APP_DIR"
echo "data dir (writable): $DATA_DIR"
echo "running as:          $([[ -n "$RUNNER" ]] && echo ddcheck || whoami)"
echo

run_in_app "python3 -m app.server" > /tmp/ddcheck-server.log 2>&1 &
SERVER_PID=$!
for _ in $(seq 80); do
  sleep 0.5
  curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "  FAIL  the server exited during startup"
    tail -20 /tmp/ddcheck-server.log
    exit 1
  fi
done

fail=0
pass() { echo "  PASS  $1"; }
bad()  { echo "  FAIL  $1"; fail=1; }

if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  pass "starts with a read-only application directory"
else
  bad "server did not come up"
  echo "--- server log ---"; tail -30 /tmp/ddcheck-server.log
  exit 1
fi

if curl -fsS -X POST "http://127.0.0.1:$PORT/api/login" \
     -H 'Content-Type: application/json' \
     -d '{"username":"checker","password":"container-check-pw"}' >/dev/null 2>&1; then
  pass "the environment-bootstrapped administrator can sign in"
else
  bad "DD_ADMIN_USER/DD_ADMIN_PASSWORD did not produce a usable admin"
fi

STRAY=$(find "$APP_DIR" -newer "$APP_DIR/requirements.txt" \( -type f -o -type d \) -print 2>/dev/null | head -3)
if [[ -z "$STRAY" ]]; then
  pass "no writes into the read-only application directory"
else
  bad "something wrote into the app directory: $STRAY"
fi

[[ -f "$DATA_DIR/index.sqlite3" ]] && pass "index created in the data volume" \
  || bad "no index in the data volume"
[[ -d "$DATA_DIR/uploads/extracted" ]] && pass "upload directories created in the data volume" \
  || bad "upload directories missing from the data volume"

OUT=$(run_in_app "python3 -c \"
from app import db, tools
db.init()
r = tools.run_python('print(6*7)')
print(r['exit_code'], (r['stdout'] or r['stderr']).strip())
\"" 2>&1 | tail -1)
if [[ "$OUT" == "0 42" ]]; then
  pass "run_python works (needs only a writable /tmp)"
else
  bad "run_python failed under container constraints: $OUT"
fi

echo
[[ $fail -eq 0 ]] && echo "container constraints satisfied" || echo "see /tmp/ddcheck-server.log"
exit $fail
