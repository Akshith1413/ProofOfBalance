#!/usr/bin/env bash
# Shared helpers for ProofOfBalance scripts: logging, staged execution with
# human-readable failure reports, retries, and a safe .env loader.
#
# Every script that sources this gets:
#   logs/<name>-<timestamp>.log   full output of the run
#   logs/<name>-latest.log        symlink to the newest run
#   logs/<name>-status.txt        SUCCESS/FAILED, which stage failed, why, and what to do

POB_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export POB_ROOT
LOG_DIR="$POB_ROOT/logs"
SECRETS_DIR="$POB_ROOT/.secrets"
TOOLS_BIN="$POB_ROOT/.tools/bin"
export PATH="$TOOLS_BIN:$POB_ROOT/.venv/bin:$PATH"

_CURRENT_STAGE="startup"
_CURRENT_HINT="Check the log file for details."
_STAGES_DONE=()
_RUN_NAME="run"
_STARTED_AT=$(date +%s)
_SUMMARY_LINES=()

if [[ -t 1 ]]; then
  _C_RESET=$'\e[0m'; _C_RED=$'\e[31m'; _C_GREEN=$'\e[32m'; _C_YELLOW=$'\e[33m'; _C_BLUE=$'\e[34m'; _C_BOLD=$'\e[1m'
else
  _C_RESET=""; _C_RED=""; _C_GREEN=""; _C_YELLOW=""; _C_BLUE=""; _C_BOLD=""
fi

_ts() { date '+%Y-%m-%d %H:%M:%S'; }
info()  { printf '%s %s[info]%s  %s\n' "$(_ts)" "$_C_BLUE" "$_C_RESET" "$*"; }
ok()    { printf '%s %s[ ok ]%s  %s\n' "$(_ts)" "$_C_GREEN" "$_C_RESET" "$*"; }
warn()  { printf '%s %s[warn]%s  %s\n' "$(_ts)" "$_C_YELLOW" "$_C_RESET" "$*" >&2; }
error() { printf '%s %s[fail]%s  %s\n' "$(_ts)" "$_C_RED" "$_C_RESET" "$*" >&2; }
summary() { _SUMMARY_LINES+=("$*"); }

# init_logging <name>: tee all output to a timestamped log and arm the exit trap.
init_logging() {
  _RUN_NAME="$1"
  umask 077   # logs, kubeconfig, tfvars and generated secrets are private to you
  mkdir -p "$LOG_DIR"
  LOG_FILE="$LOG_DIR/${_RUN_NAME}-$(date '+%Y%m%d-%H%M%S').log"
  STATUS_FILE="$LOG_DIR/${_RUN_NAME}-status.txt"
  : >"$LOG_FILE"
  ln -sfn "$(basename "$LOG_FILE")" "$LOG_DIR/${_RUN_NAME}-latest.log"
  exec > >(tee -a "$LOG_FILE") 2>&1
  trap '_on_exit $?' EXIT
  trap 'error "interrupted"; exit 130' INT TERM
  # Unexpected failures (not handled by fail()) still say exactly what broke.
  trap '_on_err $? "$BASH_COMMAND" "${BASH_SOURCE[0]:-?}" "$LINENO"' ERR
  info "log file: $LOG_FILE"
}

_on_err() {
  local rc="$1" cmd="$2" src="$3" line="$4"
  [[ -n "${_ERR_REPORTED:-}" ]] && return 0
  _ERR_REPORTED=1
  error "command failed (exit $rc) at ${src#"$POB_ROOT"/}:$line: $cmd"
  [[ "$rc" -eq 141 ]] && error "exit 141 = SIGPIPE: a pipeline was cut short"
  _CURRENT_HINT="$_CURRENT_HINT (Failing command: '$cmd' at ${src#"$POB_ROOT"/}:$line, exit $rc.)"
}

_on_exit() {
  local rc=$1 elapsed=$(( $(date +%s) - _STARTED_AT ))
  [[ -n "${STATUS_FILE:-}" ]] || return 0
  {
    if [[ $rc -eq 0 ]]; then
      echo "RESULT: SUCCESS"
    else
      echo "RESULT: FAILED (exit code $rc)"
      echo
      echo "Failed stage : $_CURRENT_STAGE"
      echo "What it means: $_CURRENT_HINT"
    fi
    echo "Finished     : $(date '+%Y-%m-%d %H:%M:%S %Z') after ${elapsed}s"
    echo "Full log     : $LOG_FILE"
    echo
    echo "Stages completed:"
    if [[ ${#_STAGES_DONE[@]} -eq 0 ]]; then echo "  (none)"; else printf '  - %s\n' "${_STAGES_DONE[@]}"; fi
    if [[ ${#_SUMMARY_LINES[@]} -gt 0 ]]; then
      echo
      echo "Summary:"
      printf '  %s\n' "${_SUMMARY_LINES[@]}"
    fi
    if [[ $rc -ne 0 ]]; then
      echo
      echo "Last 40 lines of the log:"
      echo "-------------------------"
      tail -n 40 "$LOG_FILE" 2>/dev/null | sed -e 's/\x1b\[[0-9;]*m//g'
      echo
      echo "Re-running the same script is safe: every stage is idempotent and"
      echo "skips work that is already done."
    fi
  } >"$STATUS_FILE" 2>/dev/null
  if [[ $rc -eq 0 ]]; then
    printf '\n%s%s finished successfully in %ss.%s Status: %s\n' "$_C_GREEN$_C_BOLD" "$_RUN_NAME" "$elapsed" "$_C_RESET" "$STATUS_FILE"
  else
    printf '\n%s%s FAILED at stage "%s".%s\n  Why : %s\n  Log : %s\n  Status report: %s\n' \
      "$_C_RED$_C_BOLD" "$_RUN_NAME" "$_CURRENT_STAGE" "$_C_RESET" "$_CURRENT_HINT" "$LOG_FILE" "$STATUS_FILE" >&2
  fi
}

# stage "<name>" "<hint shown if it fails>" command [args...]
stage() {
  local name="$1" hint="$2"; shift 2
  _CURRENT_STAGE="$name"
  _CURRENT_HINT="$hint"
  local t0
  t0=$(date +%s)
  printf '\n%s==> %s%s\n' "$_C_BOLD" "$name" "$_C_RESET"
  "$@"
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    error "stage '$name' failed (exit $rc)"
    exit "$rc"
  fi
  _STAGES_DONE+=("$name ($(( $(date +%s) - t0 ))s)")
  ok "$name"
}

# fail "<message>" ["<hint>"]: abort the current stage with an explanation.
fail() {
  error "$1"
  [[ -n "${2:-}" ]] && _CURRENT_HINT="$2"
  exit 1
}

# retry <attempts> <delay-seconds> command [args...]
retry() {
  local attempts="$1" delay="$2" n=1; shift 2
  until "$@"; do
    if (( n >= attempts )); then
      return 1
    fi
    warn "attempt $n/$attempts failed: $*; retrying in ${delay}s"
    sleep "$delay"
    n=$((n + 1))
    delay=$((delay * 2 > 60 ? 60 : delay * 2))
  done
}

# wait_until <timeout-seconds> <interval> <description> command [args...]
wait_until() {
  local timeout="$1" interval="$2" what="$3"; shift 3
  local deadline=$(( $(date +%s) + timeout ))
  until "$@" >/dev/null 2>&1; do
    if (( $(date +%s) >= deadline )); then
      error "timed out after ${timeout}s waiting for: $what"
      return 1
    fi
    sleep "$interval"
  done
}

# probe_until: like wait_until, but a timeout is an expected answer, not an error.
probe_until() {
  local timeout="$1" interval="$2"; shift 2
  local deadline=$(( $(date +%s) + timeout ))
  until "$@" >/dev/null 2>&1; do
    (( $(date +%s) >= deadline )) && return 1
    sleep "$interval"
  done
}

require_cmd() {
  local missing=()
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
  if [[ ${#missing[@]} -gt 0 ]]; then
    fail "missing required commands: ${missing[*]}" \
      "Install: ${missing[*]} (e.g. with your package manager) and re-run."
  fi
}

# load_env <file>: parse KEY=VALUE lines without executing anything.
# Supports comments, blank lines, 'export KEY=...', and single/double quotes.
# Variables already set (non-empty) in the environment win over the file.
load_env() {
  local file="$1" line key value
  [[ -f "$file" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    line="${line#export }"
    if [[ ! "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
      warn "ignoring malformed line in $(basename "$file"): ${line%%=*}=..."
      continue
    fi
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    value="${value#"${value%%[![:space:]]*}"}"   # ltrim
    if [[ "$value" =~ ^\"(.*)\"[[:space:]]*(#.*)?$ ]]; then
      value="${BASH_REMATCH[1]}"
    elif [[ "$value" =~ ^\'(.*)\'[[:space:]]*(#.*)?$ ]]; then
      value="${BASH_REMATCH[1]}"
    else
      value="${value%%[[:space:]]#*}"              # strip trailing ' # comment'
      value="${value%"${value##*[![:space:]]}"}"   # rtrim
    fi
    # A non-empty value already in the environment wins. An empty one does not
    # block a later file (e.g. .secrets/generated.env) from filling it in.
    if [[ -z "${!key:-}" ]]; then
      export "$key=$value"
    fi
  done <"$file"
}

# Write a value to .secrets/generated.env once, so reruns reuse it.
persist_generated() {
  local key="$1" value="$2" file="$SECRETS_DIR/generated.env"
  mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
  (
    umask 077   # the temp file replaces the original, so it must be private too
    touch "$file"
    grep -v "^${key}=" "$file" >"$file.tmp" 2>/dev/null || true
    printf '%s=%s\n' "$key" "$value" >>"$file.tmp"
    mv "$file.tmp" "$file"
  )
  chmod 600 "$file"
}

random_secret() {
  # 32 alphanumeric characters. (Not `tr </dev/urandom | head`: under pipefail
  # tr dies of SIGPIPE and the whole script exits 141.)
  python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(32)))'
}

confirm() {
  local prompt="$1" answer
  if [[ "${ASSUME_YES:-false}" == "true" ]]; then return 0; fi
  if [[ ! -t 0 ]]; then
    error "$prompt — no terminal to confirm on; pass --yes to proceed non-interactively"
    return 1
  fi
  read -r -p "$prompt [y/N] " answer
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}
