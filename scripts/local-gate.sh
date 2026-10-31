#!/usr/bin/env bash
# Run the ledger gate locally with docker compose: fresh stack -> replay ->
# export -> diff against the committed baseline. Same tools as CI.
#
#   scripts/local-gate.sh                                  # should PASS on main
#   scripts/local-gate.sh ROUNDING_MODE=ROUND_HALF_UP       # demo PR: should FAIL
#   scripts/local-gate.sh SETTLEMENT_TZ=Asia/Kolkata        # demo PR: should FAIL
#   scripts/local-gate.sh RAIL_DELAY_MS=300 RAIL_TIMEOUT_MS=100   # duplicate payouts
#   scripts/local-gate.sh --update-baseline                # rewrite baseline/ledger_baseline.csv
#
# Options: --keep (leave the stack running), --out DIR (default out/local-gate)
# Exit: 0 ledger identical, 1 ledger changed, other = the gate could not run.
set -Eeuo pipefail
source "$(dirname "$0")/lib/common.sh"
source "$(dirname "$0")/lib/tools.sh"

UPDATE_BASELINE=false KEEP=false OUT="$POB_ROOT/out/local-gate"
OVERRIDES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --update-baseline) UPDATE_BASELINE=true ;;
    --keep) KEEP=true ;;
    --out) OUT="$2"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *=*) OVERRIDES+=("$1") ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
  shift
done

init_logging local-gate
mkdir -p "$OUT"
# shellcheck disable=SC2163  # exporting the KEY=VALUE pair itself
for kv in "${OVERRIDES[@]}"; do export "$kv"; info "override: $kv"; done
summary "overrides: ${OVERRIDES[*]:-none}"
PG_PORT="${POB_PG_PORT:-5432}"
export DATABASE_URL="postgresql://pob:pob@localhost:${PG_PORT}/pob"

compose() { docker compose -f "$POB_ROOT/docker-compose.yml" "$@"; }

cleanup() { if [[ "$KEEP" != true ]]; then compose down -v --remove-orphans >/dev/null 2>&1 || true; fi; }

check_docker() {
  require_cmd docker
  docker info >/dev/null 2>&1 || fail "docker daemon not reachable" "Start Docker (Docker Desktop or 'sudo systemctl start docker') and re-run."
  docker compose version >/dev/null 2>&1 || fail "docker compose v2 missing" "Install the Docker Compose plugin."
}

fresh_stack() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  retry 2 5 compose up -d --build --wait --wait-timeout 300 || {
    compose ps -a; compose logs --tail 50; return 1; }
}

REPLAY_RC=0
run_replay() {
  # rc 1 (send errors / payouts never settled) is itself a ledger change: keep
  # going so the diff can show it. rc 2 means the replay could not start.
  python "$POB_ROOT/tools/replay.py" --file "$POB_ROOT/traffic/day1.jsonl" \
    --url "http://localhost:${POB_SERVICE_PORT:-8080}" --workers 8 --summary-json "$OUT/replay.json" || REPLAY_RC=$?
  if [[ $REPLAY_RC -eq 1 ]]; then warn "replay did not complete cleanly; diffing what was recorded"; return 0; fi
  return $REPLAY_RC
}

run_export() {
  python "$POB_ROOT/tools/export_ledger.py" --dsn "$DATABASE_URL" --out "$OUT/run.csv" \
    --rail-url "http://localhost:${POB_RAIL_PORT:-8081}"
}

stage "Check Docker" "Docker must be running for the local gate." check_docker
stage "Python tools" "Creating .venv or installing tools/requirements.txt failed." ensure_venv
# shellcheck disable=SC2154  # _rc is set inside the trap
trap '_rc=$?; cleanup; _on_exit $_rc' EXIT
stage "Start fresh stack" "docker compose could not start the stack; see the container logs above." fresh_stack
stage "Replay traffic" "Replay failed: payments were rejected by the service or did not settle within 5 minutes." run_replay
stage "Export ledger" "Could not read the ledger from Postgres or the transfer list from the rail." run_export

if [[ "$UPDATE_BASELINE" == true ]]; then
  cp "$OUT/run.csv" "$POB_ROOT/baseline/ledger_baseline.csv"
  summary "baseline updated: baseline/ledger_baseline.csv ($(wc -l <"$OUT/run.csv") lines)"
  ok "baseline written; commit it in a PR labelled update-baseline"
  exit 0
fi

_CURRENT_STAGE="Diff ledger"; _CURRENT_HINT="The replayed ledger differs from baseline/ledger_baseline.csv: this change moves money. See $OUT/report.md."
set +e
python "$POB_ROOT/tools/diff_ledger.py" --baseline "$POB_ROOT/baseline/ledger_baseline.csv" \
  --run "$OUT/run.csv" --report "$OUT/report.md" --rows-out "$OUT/diff_rows.csv"
rc=$?
set -e
if [[ $REPLAY_RC -ne 0 ]]; then
  printf '\n> **Replay did not complete cleanly** (exit %s): some payments were rejected or never settled.\n' "$REPLAY_RC" >>"$OUT/report.md"
  [[ $rc -eq 0 ]] && rc=1
fi
echo; cat "$OUT/report.md"
summary "report: $OUT/report.md"
exit $rc
