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
#   ./deploy/update.sh --recreate      # recreate the container even with no new commit
#
# Order matters. The new image is built while the old container is still
# serving, so the only downtime is the container swap (seconds). If the new
# container fails its health check the script restores the previous commit,
# rebuilds and brings the old version back up, then exits non-zero — a failed
# update leaves the service running, not down.
#
# An edit to .env alone is a deploy too. `env_file` values are read when the
# container is *created*, so a `restart` keeps the old ones and a raised limit
# silently does not apply. The script compares the running container's
# environment against .env and recreates when they differ, rather than reporting
# "nothing to do" for a change that has not taken effect. Deleting a line counts
# as a difference: the old value stays in the container until it is replaced.
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
DO_PRUNE=0
FORCE=0
DRY_RUN=0
ROLLBACK=0
STATUS_ONLY=0
BACKUP_ONLY=0
RECREATE=0
HEALTH_TIMEOUT=180
# How the running container's environment differs from .env, filled in by
# env_drift(): DRIFTED for a changed value, REMOVED for a key the container still
# carries that .env no longer declares. Names only — the file holds the admin
# password and the API key.
DRIFTED=()
REMOVED=()

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
    --backup-only) BACKUP_ONLY=1 ;;
    --prune)      DO_PRUNE=1 ;;
    --force)      FORCE=1 ;;
    --recreate)   RECREATE=1 ;;
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
# same address the reverse proxy talks to.
HOST_PORT="$(grep -E '^DD_HOST_PORT=' .env | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
HOST_PORT="${HOST_PORT:-8412}"
BIND_ADDR="$(grep -E '^DD_BIND_ADDR=' .env | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
BIND_ADDR="${BIND_ADDR:-127.0.0.1}"
PROBE_HOST="$BIND_ADDR"
[[ "$PROBE_HOST" == "0.0.0.0" ]] && PROBE_HOST="127.0.0.1"
HEALTH_URL="http://${PROBE_HOST}:${HOST_PORT}/api/health"

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

env_drift() {
  # Is the running container carrying the current .env? Compose reads env_file
  # when it *creates* a container, so an edit plus `docker compose restart` keeps
  # the old values — the app then goes on enforcing a limit that .env says was
  # raised, with nothing anywhere to say why. `up -d` normally notices, but only
  # if it is reached at all, and a no-op update used to exit before it.
  #
  # Sets DRIFTED to the keys whose value differs and REMOVED to the keys the
  # container still carries that .env no longer declares, and returns 0 when
  # either is non-empty. Names only, never values: .env holds DD_ADMIN_PASSWORD
  # and the API key, and this output goes to a terminal, a cron mail and whatever
  # log collects it.
  DRIFTED=()
  REMOVED=()
  [[ -f .env ]] || return 1
  local created created_epoch env_epoch
  created="$(docker inspect -f '{{.Created}}' "$SERVICE" 2>/dev/null || true)"
  [[ -n "$created" ]] || return 1
  # Cheap gate: an .env untouched since the container was created cannot have
  # drifted, and this keeps a quoting difference we parse differently from
  # compose from swapping the container on every cron run.
  created_epoch="$(date -d "$created" +%s 2>/dev/null || echo 0)"
  env_epoch="$(stat -c %Y .env 2>/dev/null || echo 0)"
  (( created_epoch > 0 && env_epoch > created_epoch )) || return 1

  local -a live
  mapfile -t live < <(
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$SERVICE" 2>/dev/null || true
  )
  local line key want got entry
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    key="${key#export }"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    want="${line#*=}"
    # Compose strips one layer of matching quotes from an env_file value.
    if [[ "$want" == \"*\" || "$want" == \'*\' ]] && (( ${#want} >= 2 )); then
      want="${want:1:${#want}-2}"
    fi
    got=""
    for entry in "${live[@]}"; do
      if [[ "$entry" == "$key="* ]]; then
        got="${entry#*=}"
        break
      fi
    done
    [[ "$want" == "$got" ]] || DRIFTED+=("$key")
  done < .env

  # The other direction. Deleting a line — or commenting it out — leaves the value
  # in the running container, so the app goes on using a setting the file no longer
  # mentions. DD_ADMIN_RESET_PASSWORD is the one that bites: removing it is exactly
  # how you stop resetting the admin password on every restart.
  #
  # Only the app's own variables, and only where .env is the source. The image sets
  # DD_DATA_DIR, DD_HOST and DD_PORT, and docker-compose.yml's `environment:` block
  # sets three more of its own; neither is .env's business, so a key either file
  # supplies is left alone.
  local -a image_env=()
  local image
  image="$(docker inspect -f '{{.Config.Image}}' "$SERVICE" 2>/dev/null || true)"
  if [[ -n "$image" ]]; then
    mapfile -t image_env < <(
      docker image inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$image" 2>/dev/null || true
    )
  fi
  for entry in "${live[@]}"; do
    key="${entry%%=*}"
    [[ "$key" == DD_* || "$key" == ANTHROPIC_* ]] || continue
    # Still declared in .env? Then the loop above has already judged it. An
    # anchored match, so a commented-out line does not count as declared.
    if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${key}=" .env; then
      continue
    fi
    # Set by compose itself? Matched as a YAML mapping key, not anywhere in the
    # file: DD_HOST_PORT and DD_BIND_ADDR appear only in `${...}` interpolation on
    # the ports line, so a substring match called them compose-managed and stopped
    # noticing when they were deleted — and it matched DD_HOST inside DD_HOST_PORT
    # too, which skipped the image comparison below for a different variable.
    if grep -Eq "^[[:space:]]+${key}:" docker-compose.yml; then
      continue
    fi
    # Exactly what the image bakes in, rather than merely the same name: a value
    # deleted from .env that shadowed an image default is still a change.
    if [[ ${#image_env[@]} -gt 0 ]] && printf '%s\n' "${image_env[@]}" | grep -qxF "$entry"; then
      continue
    fi
    REMOVED+=("$key")
  done

  (( ${#DRIFTED[@]} > 0 || ${#REMOVED[@]} > 0 ))
}

port_conflict() {
  # Something else on our port would make `up` fail with a confusing bind
  # error. Our own container holding it is expected, so ignore that case.
  command -v ss >/dev/null 2>&1 || return 1  # can't tell; let docker complain
  local out
  out="$(ss -Hltnp "sport = :${HOST_PORT}" 2>/dev/null || true)"
  [[ -z "$out" ]] && return 1
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
  # The question this answers is "why is the app not honouring what .env says",
  # and it is the first thing to rule out before going looking in the app.
  if [[ "$(container_state)" == "absent" ]]; then
    info "env       no container to compare .env against"
  elif env_drift; then
    warn "env       .env has been edited since the container was created"
    (( ${#DRIFTED[@]} )) && warn "          not yet in effect: ${DRIFTED[*]}"
    (( ${#REMOVED[@]} )) && warn "          still set in the container, gone from .env: ${REMOVED[*]}"
    warn "          apply with: ./deploy/update.sh  (or docker compose up -d)"
  else
    info "env       matches .env"
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
  # `up -d` recreates a container whose resolved config has changed, env_file
  # contents included. --force-recreate when we already know the environment has
  # drifted, or were asked to: it costs the same few seconds and does not depend
  # on the compose version agreeing with us about what counts as a change. A
  # container that only reads .env at creation is not worth being subtle about.
  local up_args=(up -d --remove-orphans)
  (( RECREATE )) && up_args+=(--force-recreate)
  run compose "${up_args[@]}"
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

# An .env edit is a deploy the running container has not had. Checked whether or
# not the commit moved, because the image may well be identical either way and it
# is the container that has to be replaced.
if env_drift; then
  warn "the running container's environment differs from .env — it will be recreated"
  (( ${#DRIFTED[@]} )) && warn "changed: ${DRIFTED[*]}"
  (( ${#REMOVED[@]} )) && warn "no longer in .env: ${REMOVED[*]}"
  RECREATE=1
fi

if [[ "$NEW_SHA" == "$OLD_SHA" ]]; then
  # No new commit is not the same as nothing to do: exiting here on a changed
  # .env is what makes a raised limit look like a limit the app is ignoring.
  if (( RECREATE )); then
    step "No new commit, but the container needs replacing"
  elif [[ "$(container_state)" == "running" ]] && curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
    ok "already up to date at ${OLD_SHA:0:12} and healthy — nothing to do"
    exit 0
  else
    warn "already at ${OLD_SHA:0:12} but the service is not healthy — redeploying"
  fi
else
  step "Changes to deploy"
  git --no-pager log --oneline --no-decorate "$OLD_SHA..$NEW_SHA" | sed 's/^/    /'
fi

# .env.example gaining a variable is the usual cause of a working directory
# that starts but misbehaves, so say so rather than letting it surprise later.
if git diff --name-only "$OLD_SHA" "$NEW_SHA" | grep -q '^\.env\.example$'; then
  warn ".env.example changed in this update — check .env for new variables:"
  git --no-pager diff "$OLD_SHA" "$NEW_SHA" -- .env.example | sed 's/^/    /' >&2 || true
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
