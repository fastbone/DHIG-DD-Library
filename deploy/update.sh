#!/usr/bin/env bash
# Update DD Library on a Docker host: pull, build, swap, verify, roll back.
#
#   ./deploy/update.sh                 # the normal update
#   ./deploy/update.sh --status        # what is running, no changes
#   ./deploy/update.sh --dry-run       # print the plan, change nothing
#   ./deploy/update.sh --rollback      # back to the previous commit and image
#   ./deploy/update.sh --backup-only   # snapshot the index and secrets, nothing else
#   ./deploy/update.sh --no-backup     # skip the pre-update snapshot
#   ./deploy/update.sh --branch main   # deploy a different branch
#   ./deploy/update.sh --keep 10       # keep 10 backups instead of 5
#   ./deploy/update.sh --force         # deploy even with local modifications
#   ./deploy/update.sh --no-env-sync   # do not touch .env, even if it lacks variables
#
# Order matters. After the fetch and before the build, deploy/env-sync.sh appends
# any variable the new .env.example documents and .env is missing, and — on a
# terminal — opens .env so you can fill the values in. That happens *before* the
# image is built and the container swapped, so a variable added by an update
# takes effect in this run instead of needing a second restart.
#
# The new image is built while the old container is still
# serving, so the only downtime is the container swap (seconds). If the new
# container fails its health check the script restores the previous commit,
# rebuilds and brings the old version back up, then exits non-zero — a failed
# update leaves the service running, not down.
#
# Run it as a user in the `docker` group, from the repository checkout. Safe to
# put in cron: concurrent runs are serialised by a lock and a no-op update
# prints one line and exits 0.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

SERVICE="dd-library"
KEEP_BACKUPS=5
BRANCH=""
DO_PULL=1
DO_BACKUP=1
DO_ENV_SYNC=1
DO_PRUNE=0
FORCE=0
DRY_RUN=0
ROLLBACK=0
STATUS_ONLY=0
BACKUP_ONLY=0
HEALTH_TIMEOUT=180

# --- output --------------------------------------------------------------

if [[ -t 1 ]]; then
  B=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; RST=$'\e[0m'
else
  B=""; DIM=""; RED=""; GRN=""; YLW=""; RST=""
fi
step() { printf '%s==>%s %s\n' "$B" "$RST" "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '%s warn%s %s\n' "$YLW" "$RST" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }
ok()   { printf '%s  ok%s %s\n' "$GRN" "$RST" "$*"; }
run()  {
  if (( DRY_RUN )); then
    printf '%s  would run:%s %s\n' "$DIM" "$RST" "$*"
  else
    "$@"
  fi
}

# --- arguments -----------------------------------------------------------

while (( $# )); do
  case "$1" in
    --status)     STATUS_ONLY=1 ;;
    --dry-run)    DRY_RUN=1 ;;
    --rollback)   ROLLBACK=1 ;;
    --no-pull)    DO_PULL=0 ;;
    --no-backup)  DO_BACKUP=0 ;;
    --no-env-sync) DO_ENV_SYNC=0 ;;
    --backup-only) BACKUP_ONLY=1 ;;
    --prune)      DO_PRUNE=1 ;;
    --force)      FORCE=1 ;;
    --branch)     BRANCH="${2:?--branch needs a name}"; shift ;;
    --keep)       KEEP_BACKUPS="${2:?--keep needs a number}"; shift ;;
    --timeout)    HEALTH_TIMEOUT="${2:?--timeout needs seconds}"; shift ;;
    # The header comment is the help text: print it up to the first code line.
    -h|--help)    sed -n '2,/^[^#]/p' "$0" | sed -e '/^[^#]/d' -e 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    *)            die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# --- compose wrapper -----------------------------------------------------
# Prefer the v2 plugin; fall back to the standalone binary.

if docker compose version >/dev/null 2>&1; then
  compose() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  compose() { docker-compose "$@"; }
else
  die "neither 'docker compose' nor 'docker-compose' is available"
fi

# --- one at a time -------------------------------------------------------
# A second run mid-swap would fight the first over the container and the git
# checkout. flock makes the loser exit immediately rather than queue behind a
# build that may take minutes.

LOCK="${TMPDIR:-/tmp}/dd-library-update.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  die "another update is already running (lock: $LOCK)"
fi

# --- environment ---------------------------------------------------------

[[ -f docker-compose.yml ]] || die "no docker-compose.yml in $REPO_DIR"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is your user in the docker group?)"

if [[ ! -f .env ]]; then
  die ".env is missing. Copy .env.example to .env and fill in DD_SECRET_KEY."
fi
if ! grep -Eq '^DD_SECRET_KEY=.+' .env; then
  die "DD_SECRET_KEY is empty in .env. Stored API keys are encrypted with it; \
without it every key in the database becomes undecryptable."
fi

# Read the published port the way compose does, so the health check probes the
# same address the reverse proxy talks to. A function, not straight-line code:
# the .env sync below may open an editor, and whatever comes back out of it is
# what the container will actually be published on.
read_env_addressing() {
  HOST_PORT="$(grep -E '^DD_HOST_PORT=' .env | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  HOST_PORT="${HOST_PORT:-8412}"
  BIND_ADDR="$(grep -E '^DD_BIND_ADDR=' .env | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
  BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
  PROBE_HOST="$BIND_ADDR"
  [[ "$PROBE_HOST" == "0.0.0.0" ]] && PROBE_HOST="127.0.0.1"
  HEALTH_URL="http://${PROBE_HOST}:${HOST_PORT}/api/health"
}
read_env_addressing

STATE_DIR="$REPO_DIR/.deploy"
mkdir -p "$STATE_DIR"
PREV_SHA_FILE="$STATE_DIR/previous-sha"

container_state() {
  # `docker inspect` on a missing container still writes a newline to stdout,
  # which would otherwise show up as a blank state.
  local s
  s="$(docker inspect -f '{{.State.Status}}' "$SERVICE" 2>/dev/null || true)"
  s="${s//[[:space:]]/}"
  printf '%s' "${s:-absent}"
}

port_listening() {
  # Anything at all listening on $1, our own container's published port
  # included.
  command -v ss >/dev/null 2>&1 || return 1  # can't tell; let docker complain
  local out
  out="$(ss -Hltnp "sport = :${1}" 2>/dev/null || true)"
  [[ -n "$out" ]]
}

port_conflict() {
  # Something else on our port would make `up` fail with a confusing bind
  # error. Our own container holding it is expected, so ignore that case.
  port_listening "$HOST_PORT" || return 1
  [[ "$(container_state)" != "absent" ]] && return 1
  return 0
}

# --- status --------------------------------------------------------------

show_status() {
  step "Status"
  info "repo      $REPO_DIR"
  info "branch    $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?') @ $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
  info "container $(container_state)"
  info "health    $HEALTH_URL"
  if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
    ok "responding"
  else
    warn "not responding"
  fi
  if [[ -f "$PREV_SHA_FILE" ]]; then
    info "rollback  $(cat "$PREV_SHA_FILE")"
  fi
  # -a so a stopped container still shows up — that is exactly when you look.
  compose ps -a 2>/dev/null || true
}

if (( STATUS_ONLY )); then
  show_status
  exit 0
fi

# --- backup --------------------------------------------------------------
# The index lives in a Docker volume, so a file copy of a live SQLite database
# would be torn. sqlite3's backup API takes a consistent snapshot while the app
# keeps writing; secret.key and settings.json are copied alongside it because
# the database is useless without the key that decrypts the stored API keys.

backup() {
  local stamp dest
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  dest="$STATE_DIR/backups/$stamp"
  step "Backing up the index and secrets"
  if [[ "$(container_state)" != "running" ]]; then
    warn "container is not running — skipping backup (nothing to snapshot safely)"
    return 0
  fi
  if (( DRY_RUN )); then
    info "would snapshot /data/index.sqlite3, secret.key and settings.json into $dest"
    return 0
  fi
  mkdir -p "$dest"
  # Staged inside /data rather than /tmp: /tmp is a tmpfs mount, and `docker cp`
  # cannot read files inside one ("Could not find the file ..."). /data is the
  # volume, so it is both writable and reachable from the host.
  #
  # sqlite3's backup API rather than a file copy: the app keeps writing during
  # the snapshot, and copying a live WAL database gives you a torn file.
  #
  # -i is required, or docker leaves stdin unattached and python runs an empty
  # script — which "succeeds" and backs up nothing.
  local staged="/data/backups/$stamp"
  if docker exec -i "$SERVICE" python3 - "$staged" <<'PY'
import shutil, sqlite3, sys
from pathlib import Path

staged = Path(sys.argv[1])
staged.mkdir(parents=True, exist_ok=True)

src = Path("/data/index.sqlite3")
if src.exists():
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(str(staged / "index.sqlite3"))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
# The catalogue is useless without the key that decrypts the API keys in it.
for name in ("secret.key", "settings.json"):
    p = Path("/data") / name
    if p.exists():
        shutil.copy2(p, staged / name)
print(" ".join(sorted(p.name for p in staged.iterdir())))
PY
  then
    docker cp "$SERVICE:$staged/." "$dest/" >/dev/null 2>&1 || \
      warn "could not copy the snapshot out of the container"
    docker exec "$SERVICE" rm -rf "$staged" 2>/dev/null || true
    if [[ -s "$dest/index.sqlite3" ]]; then
      ok "snapshot in $dest ($(du -sh "$dest" | cut -f1))"
    else
      warn "snapshot produced no database — continuing, but check $dest"
    fi
  else
    warn "snapshot failed — continuing without a backup of this update"
  fi

  # Retention: keep the newest N, drop the rest. The directory names are UTC
  # timestamps, so a reverse sort is newest-first.
  local -a all
  shopt -s nullglob
  all=("$STATE_DIR"/backups/*/)
  shopt -u nullglob
  if (( ${#all[@]} > KEEP_BACKUPS )); then
    local -a doomed
    mapfile -t doomed < <(printf '%s\n' "${all[@]}" | sort -r | tail -n "+$((KEEP_BACKUPS + 1))")
    for d in "${doomed[@]}"; do
      rm -rf "$d" && info "pruned old backup $(basename "$d")"
    done
  fi
}

if (( BACKUP_ONLY )); then
  backup
  exit 0
fi

# --- .env ----------------------------------------------------------------
# Run after the fetch (so .env.example is the new one) and before the build, so
# a variable an update introduces is in place for the container that update
# starts. Interactively that includes filling the value in: an editor here costs
# nothing, whereas noticing afterwards costs a second build and swap.

ENV_CHANGED=0

sync_env() {
  if (( ! DO_ENV_SYNC )); then
    info "skipping the .env check (--no-env-sync)"
    return 0
  fi
  local script="$REPO_DIR/deploy/env-sync.sh"
  if [[ ! -x "$script" ]]; then
    warn "deploy/env-sync.sh missing or not executable — skipping the .env check"
    return 0
  fi

  local -a args=()
  if (( DRY_RUN )); then
    args+=(--dry-run)
  elif [[ -t 0 && -t 1 ]]; then
    args+=(--edit)
  fi
  # Unattended (cron), the appended defaults stand: they are the same values the
  # example documents, and env-sync.sh says on stdout what it added.

  local rc=0
  "$script" ${args[@]+"${args[@]}"} || rc=$?
  case "$rc" in
    0)  ;;
    # Set on a dry run too, so the plan that follows is the plan a real run
    # would carry out: appending to .env is a reason to recreate the container
    # even when the commit does not change.
    10) ENV_CHANGED=1 ;;
    *)  die "the .env check failed (exit $rc) — fix .env or pass --no-env-sync" ;;
  esac
  if (( ! ENV_CHANGED )); then
    return 0
  fi
  if (( DRY_RUN )); then
    info "would append those and open .env before building"
    return 0  # nothing was appended or edited, so there is nothing to re-check
  fi

  # The editor is a free hand on the file the deploy depends on, so re-check the
  # two things preflight checked before it was opened.
  grep -Eq '^DD_SECRET_KEY=.+' .env \
    || die "DD_SECRET_KEY is empty in .env — refusing to deploy without it."
  local addr_before="${BIND_ADDR}:${HOST_PORT}" port_before="$HOST_PORT"
  read_env_addressing
  if [[ "${BIND_ADDR}:${HOST_PORT}" != "$addr_before" ]]; then
    info "published address is now ${BIND_ADDR}:${HOST_PORT} — the reverse proxy needs to agree"
  fi
  # Only a *changed port* gets the strict test, and it has to be the strict one:
  # port_conflict forgives a busy port when our own container exists, which during
  # an update it does — on the old port. A port nobody held before is nobody's, so
  # anything listening on it is a real conflict, and finding out at `up` time means
  # a failed swap and a rollback onto a .env that still names the busy port.
  # An unchanged port is ours already, bind address edited or not: `up` releases
  # and rebinds it, so port_listening seeing our own docker-proxy there is not a
  # conflict.
  if [[ "$HOST_PORT" != "$port_before" ]] && port_listening "$HOST_PORT"; then
    die "${HOST_PORT} is already in use. Pick another port with DD_HOST_PORT \
in .env and update the reverse proxy."
  fi
  return 0
}

# --- health --------------------------------------------------------------

wait_healthy() {
  local deadline=$(( SECONDS + HEALTH_TIMEOUT ))
  step "Waiting for $HEALTH_URL"
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
      ok "healthy after $(( SECONDS - (deadline - HEALTH_TIMEOUT) ))s"
      return 0
    fi
    # A container that has already exited will never become healthy.
    case "$(container_state)" in
      exited|dead)
        warn "container exited while starting"
        return 1
        ;;
    esac
    sleep 3
  done
  warn "still not healthy after ${HEALTH_TIMEOUT}s"
  return 1
}

deploy_current_tree() {
  step "Building the image"
  local build_args=(build)
  (( DO_PULL )) && build_args+=(--pull)
  run compose "${build_args[@]}"

  step "Swapping the container"
  run compose up -d --remove-orphans
}

# --- rollback ------------------------------------------------------------

if (( ROLLBACK )); then
  [[ -f "$PREV_SHA_FILE" ]] || die "no recorded previous commit to roll back to"
  target="$(cat "$PREV_SHA_FILE")"
  step "Rolling back to $target"
  run git checkout --quiet --force "$target"
  deploy_current_tree
  if (( DRY_RUN )); then
    # Nothing was swapped, so the health of whatever is running now says nothing
    # about the rollback. Claiming success here would be a lie.
    ok "dry run complete — nothing was changed"
    exit 0
  fi
  if wait_healthy; then
    ok "rolled back to $target"
    exit 0
  fi
  compose logs --tail 60 "$SERVICE" || true
  die "rollback did not come up healthy — inspect the logs above"
fi

# --- preflight -----------------------------------------------------------

step "Preflight"
info "compose:  $(compose version --short 2>/dev/null || echo '?')"
info "port:     ${BIND_ADDR}:${HOST_PORT} -> container 8000"
if port_conflict; then
  die "${HOST_PORT} is already in use by something other than $SERVICE. \
Pick another port with DD_HOST_PORT in .env and update the reverse proxy."
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  if (( FORCE )); then
    warn "local modifications present — continuing because of --force"
  else
    git status --short --untracked-files=no >&2
    die "the checkout has local modifications. Commit, stash, or pass --force."
  fi
fi

BRANCH="${BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
[[ "$BRANCH" != "HEAD" ]] || die "detached HEAD — pass --branch <name> to say what to deploy"
OLD_SHA="$(git rev-parse HEAD)"
info "branch:   $BRANCH @ ${OLD_SHA:0:12}"
ok "preflight passed"

# --- pull ----------------------------------------------------------------

step "Fetching origin/$BRANCH"
if (( DRY_RUN )); then
  info "would fetch and fast-forward"
else
  attempt=0
  until git fetch --quiet origin "$BRANCH"; do
    attempt=$(( attempt + 1 ))
    (( attempt >= 4 )) && die "could not fetch origin/$BRANCH after 4 attempts"
    delay=$(( 2 ** attempt ))
    warn "fetch failed — retrying in ${delay}s"
    sleep "$delay"
  done
  git checkout --quiet "$BRANCH"
  # Fast-forward only: a merge or rebase on a deployment host is how you get a
  # half-resolved tree in production.
  git merge --quiet --ff-only "origin/$BRANCH" \
    || die "cannot fast-forward $BRANCH onto origin/$BRANCH — the checkout has \
diverged. Resolve it by hand, or reset to origin/$BRANCH if the host holds \
nothing you need."
fi

NEW_SHA="$(git rev-parse HEAD)"
UP_TO_DATE=0
if [[ "$NEW_SHA" == "$OLD_SHA" ]]; then
  UP_TO_DATE=1
else
  step "Changes to deploy"
  git --no-pager log --oneline --no-decorate "$OLD_SHA..$NEW_SHA" | sed 's/^/    /'
  # New variables are added to .env by sync_env below; a *changed default* for a
  # variable .env already sets is not, because .env wins and its value is yours.
  # The diff is the only place that shows up.
  if git diff --name-only "$OLD_SHA" "$NEW_SHA" | grep -q '^\.env\.example$'; then
    warn ".env.example changed in this update — review it for changed defaults:"
    git --no-pager diff "$OLD_SHA" "$NEW_SHA" -- .env.example | sed 's/^/    /' >&2 || true
  fi
fi

sync_env

if (( UP_TO_DATE )); then
  if (( ENV_CHANGED )); then
    info "no new commits, but .env needs new variables — recreating the container so they take effect"
  elif [[ "$(container_state)" == "running" ]] && curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
    ok "already up to date at ${OLD_SHA:0:12} and healthy — nothing to do"
    exit 0
  else
    warn "already at ${OLD_SHA:0:12} but the service is not healthy — redeploying"
  fi
fi

(( DO_BACKUP )) && backup

# Remember where to go back to before anything is swapped.
if (( ! DRY_RUN )); then
  echo "$OLD_SHA" > "$PREV_SHA_FILE"
fi

deploy_current_tree

if (( DRY_RUN )); then
  ok "dry run complete — nothing was changed"
  exit 0
fi

if wait_healthy; then
  ok "deployed ${NEW_SHA:0:12}"
  if (( DO_PRUNE )); then
    step "Pruning superseded images"
    # Only ours: the label is set in docker-compose.yml. A bare `image prune`
    # on a shared host is somebody else's outage.
    docker image prune -f --filter "label=net.dhig.ddlib=true" || true
  fi
  show_status
  exit 0
fi

# --- automatic rollback --------------------------------------------------

warn "the new version did not come up healthy — rolling back to ${OLD_SHA:0:12}"
compose logs --tail 80 "$SERVICE" >&2 || true
git checkout --quiet --force "$OLD_SHA"
deploy_current_tree
if wait_healthy; then
  warn "rolled back to ${OLD_SHA:0:12}; the service is up on the previous version"
  warn "the checkout is now on a detached HEAD — deploy again with: ./deploy/update.sh --branch $BRANCH"
  exit 1
fi
die "rollback also failed to become healthy. The service is down — check the logs above."
