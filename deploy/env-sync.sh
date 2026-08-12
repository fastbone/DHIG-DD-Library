#!/usr/bin/env bash
# Add variables that .env.example has and .env does not to the bottom of .env.
#
#   ./deploy/env-sync.sh              # append what is missing
#   ./deploy/env-sync.sh --check      # report only, change nothing
#   ./deploy/env-sync.sh --dry-run    # print the block that would be appended
#   ./deploy/env-sync.sh --edit       # append, then open .env in $EDITOR
#   ./deploy/env-sync.sh --env FILE --example FILE   # non-default paths
#
# Nothing already in .env is ever touched: the script only appends, in one
# timestamped block per run, so `git diff`-style review and a plain `tail` both
# show exactly what an update introduced. Values come from .env.example, i.e.
# the documented defaults — a new variable that needs a real value (a secret, a
# port) is yours to fill in, which is what --edit is for.
#
# Comments are not synced. Prose in .env.example is documentation, and .env
# stays a short list of what this host actually sets. Two consequences worth
# knowing:
#
#   * a variable that exists in .env only as a commented-out line is left
#     alone and reported — commenting one out is a decision, and re-adding it
#     with the example's default would silently undo it. Delete the commented
#     line if you want the variable back on the next run.
#   * variables .env sets that .env.example no longer mentions are reported,
#     never removed.
#
# Exit status: 0 nothing missing, 10 something was appended (or, with --check
# or --dry-run, would be), 1 an error.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ENV_FILE="$REPO_DIR/.env"
EXAMPLE_FILE="$REPO_DIR/.env.example"
CHECK_ONLY=0
DRY_RUN=0
DO_EDIT=0

# --- output --------------------------------------------------------------

if [[ -t 1 ]]; then
  B=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; RST=$'\e[0m'
else
  B=""; DIM=""; RED=""; GRN=""; YLW=""; RST=""
fi
step() { printf '%s==>%s %s\n' "$B" "$RST" "$*"; }
info() { printf '    %s\n' "$*"; }
dim()  { printf '%s    %s%s\n' "$DIM" "$*" "$RST"; }
warn() { printf '%s warn%s %s\n' "$YLW" "$RST" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }
ok()   { printf '%s  ok%s %s\n' "$GRN" "$RST" "$*"; }

# --- arguments -----------------------------------------------------------

while (( $# )); do
  case "$1" in
    --check)    CHECK_ONLY=1 ;;
    --dry-run)  DRY_RUN=1 ;;
    --edit)     DO_EDIT=1 ;;
    --env)      ENV_FILE="${2:?--env needs a path}"; shift ;;
    --example)  EXAMPLE_FILE="${2:?--example needs a path}"; shift ;;
    # The header comment is the help text: print it up to the first code line.
    -h|--help)  sed -n '2,/^[^#]/p' "$0" | sed -e '/^[^#]/d' -e 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    *)          die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

(( ${BASH_VERSINFO[0]} >= 4 )) || die "needs bash 4 or newer (associative arrays)"
[[ -f "$EXAMPLE_FILE" ]] || die "no such file: $EXAMPLE_FILE"
[[ -f "$ENV_FILE" ]] || die "no such file: $ENV_FILE (copy $EXAMPLE_FILE to it first)"

# --- reading env files ---------------------------------------------------
# Only what a dotenv reader would call an assignment counts: NAME=..., with an
# optional `export ` that docker compose tolerates. Everything else — blanks,
# banners, prose — is skipped.

assignments() {  # -> "KEY<TAB>the whole line", in file order
  awk '
    /^[[:space:]]*#/ { next }
    {
      s = $0
      sub(/^[[:space:]]+/, "", s)
      sub(/^export[[:space:]]+/, "", s)
      if (s !~ /^[A-Za-z_][A-Za-z0-9_]*=/) next
      key = s
      sub(/=.*/, "", key)
      print key "\t" $0
    }
  ' "$1"
}

commented_keys() {  # -> keys that appear only inside a comment, e.g. "#DD_OCR=0"
  awk '
    /^[[:space:]]*#/ {
      s = $0
      sub(/^[[:space:]]*#+[[:space:]]*/, "", s)
      sub(/^export[[:space:]]+/, "", s)
      if (s !~ /^[A-Za-z_][A-Za-z0-9_]*=/) next
      key = s
      sub(/=.*/, "", key)
      print key
    }
  ' "$1"
}

declare -A IN_ENV=() IS_COMMENTED=() IN_EXAMPLE=()
declare -a MISSING=() MISSING_LINES=() SKIPPED=() ORPHANED=()

while IFS=$'\t' read -r key line; do
  [[ -n "$key" ]] && IN_ENV["$key"]="$line"
done < <(assignments "$ENV_FILE")

while read -r key; do
  [[ -n "$key" ]] && IS_COMMENTED["$key"]=1
done < <(commented_keys "$ENV_FILE")

while IFS=$'\t' read -r key line; do
  [[ -n "$key" ]] || continue
  IN_EXAMPLE["$key"]=1
  # ${a[k]+x} rather than [[ -v a[k] ]]: the latter only learned about array
  # elements in bash 4.3.
  [[ -n "${IN_ENV[$key]+x}" ]] && continue
  if [[ -n "${IS_COMMENTED[$key]+x}" ]]; then
    SKIPPED+=("$key")
    continue
  fi
  MISSING+=("$key")
  MISSING_LINES+=("$line")
done < <(assignments "$EXAMPLE_FILE")

if (( ${#IN_ENV[@]} )); then
  for key in "${!IN_ENV[@]}"; do
    [[ -n "${IN_EXAMPLE[$key]+x}" ]] || ORPHANED+=("$key")
  done
fi

# --- report --------------------------------------------------------------

step "Comparing $(basename "$ENV_FILE") with $(basename "$EXAMPLE_FILE")"
info "$(basename "$ENV_FILE") sets ${#IN_ENV[@]} variables, $(basename "$EXAMPLE_FILE") documents ${#IN_EXAMPLE[@]}"

if (( ${#SKIPPED[@]} )); then
  warn "commented out in $(basename "$ENV_FILE") — left alone, not re-added: ${SKIPPED[*]}"
fi
if (( ${#ORPHANED[@]} )); then
  # Old variables the app no longer reads are harmless; a typo is not, and this
  # is the only place it shows up.
  mapfile -t ORPHANED < <(printf '%s\n' "${ORPHANED[@]}" | sort)
  info "not in $(basename "$EXAMPLE_FILE") (kept, check for typos): ${ORPHANED[*]}"
fi

if (( ! ${#MISSING[@]} )); then
  ok "no new variables — $(basename "$ENV_FILE") is up to date"
  (( DO_EDIT )) && info "nothing to fill in, not opening an editor"
  exit 0
fi

step "${#MISSING[@]} new variable(s) from $(basename "$EXAMPLE_FILE")"
for line in "${MISSING_LINES[@]}"; do
  dim "$line"
done

if (( CHECK_ONLY )); then
  info "run without --check to append these to the bottom of $(basename "$ENV_FILE")"
  exit 10
fi
if (( DRY_RUN )); then
  info "dry run — $(basename "$ENV_FILE") not modified"
  exit 10
fi

# --- append --------------------------------------------------------------
# Append only, and in one block, so this can never disturb a value that is
# already set: whatever a hand-edited .env says above stays byte-for-byte.

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# A .env whose last line has no newline would otherwise swallow the header into
# it, turning the first appended comment into part of a value.
if [[ -s "$ENV_FILE" && -n "$(tail -c1 "$ENV_FILE")" ]]; then
  printf '\n' >> "$ENV_FILE"
fi

{
  [[ -s "$ENV_FILE" ]] && printf '\n'  # a blank line before the block, but not as line 1
  printf '# ── Added by deploy/env-sync.sh on %s ──────────────────────\n' "$STAMP"
  printf '# New in %s. Values are its defaults — review them.\n' "$(basename "$EXAMPLE_FILE")"
  printf '%s\n' "${MISSING_LINES[@]}"
} >> "$ENV_FILE"

ok "appended ${#MISSING[@]} variable(s) to $ENV_FILE"

# --- edit ----------------------------------------------------------------

if (( DO_EDIT )); then
  if [[ ! -t 0 || ! -t 1 ]]; then
    warn "not a terminal — skipping the editor; the appended defaults are in effect"
  else
    editor="${VISUAL:-${EDITOR:-}}"
    if [[ -z "$editor" ]]; then
      for candidate in nano vim vi editor; do
        if command -v "$candidate" >/dev/null 2>&1; then editor="$candidate"; break; fi
      done
    fi
    # Split on whitespace: $EDITOR carries flags often enough ("code --wait",
    # "emacs -nw") that treating the whole value as one command name would fail
    # to launch exactly the editors people configure by hand.
    read -r -a editor_cmd <<< "$editor"
    if (( ${#editor_cmd[@]} == 0 )); then
      warn "no editor found (set \$EDITOR) — edit $ENV_FILE by hand if the new values need changing"
    elif ! command -v "${editor_cmd[0]}" >/dev/null 2>&1; then
      warn "\$EDITOR is set to '${editor_cmd[0]}', which is not on PATH — edit $ENV_FILE by hand"
    else
      step "Opening $ENV_FILE in $editor"
      info "the new block is at the bottom; save and quit to continue"
      # Not silenced and not fatal: a full-screen editor needs the real terminal,
      # and one that fails to launch should say so without taking the deploy with
      # it — the appended defaults are valid on their own.
      "${editor_cmd[@]}" "$ENV_FILE" </dev/tty >/dev/tty 2>&1 \
        || warn "editor exited non-zero — continuing with $ENV_FILE as it stands"
    fi
  fi
fi

exit 10
