#!/usr/bin/env bash
# The ledger gate against an installed Helm release (CI on kind, or any cluster).
#
#   scripts/ci/gate.sh [--release pob] [--namespace default] [--out out/gate]
#
# 1. port-forwards the service, Postgres and the rail (auto-restarting)
# 2. replays traffic/day1.jsonl with 8 workers and waits for payouts to settle
# 3. exports the ledger + rail transfer counts, diffs against the baseline
#
# Always writes <out>/report.md, even when replay or export break, so the PR
# comment explains what happened.
# Exit codes: 0 ledger identical, 1 ledger changed, 3 the gate itself could not run.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
RELEASE=pob NAMESPACE=default OUT="$ROOT/out/gate"
SERVICE_PORT=18080 PG_PORT=15432 RAIL_PORT=18081
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release) RELEASE="$2"; shift ;;
    --namespace) NAMESPACE="$2"; shift ;;
    --out) OUT="$2"; shift ;;
    *) echo "unknown argument $1" >&2; exit 64 ;;
  esac
  shift
done
mkdir -p "$OUT"
PY="${PYTHON:-python3}"
PIDS=()

log() { printf '[gate %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

cleanup() { for p in "${PIDS[@]:-}"; do [[ -n "$p" ]] && kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

forward() {
  # forward <svc> <local>:<remote> — restarts if kubectl port-forward dies
  local svc="$1" ports="$2"
  ( while true; do
      kubectl -n "$NAMESPACE" port-forward "svc/$svc" "$ports" >>"$OUT/port-forward.log" 2>&1 || true
      sleep 1
    done ) &
  PIDS+=($!)
}

wait_port() {
  local port="$1"
  for _ in $(seq 1 60); do
    (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

infra_error() {
  # The gate could not run: still leave a report behind for the PR comment.
  local msg="$1"
  {
    echo "### Ledger gate: ERROR — the gate could not run"
    echo
    echo "$msg"
    echo
    echo "This is an infrastructure problem, not a verdict on the ledger. Logs and pod"
    echo "diagnostics are attached to the workflow run (artifact \`ledger-run\`)."
  } >"$OUT/report.md"
  log "$msg"
  exit 3
}

forward "$RELEASE-service" "$SERVICE_PORT:80"
forward "$RELEASE-postgres" "$PG_PORT:5432"
forward "$RELEASE-rail" "$RAIL_PORT:8081"
for p in $SERVICE_PORT $PG_PORT $RAIL_PORT; do
  wait_port "$p" || infra_error "port-forward to localhost:$p never came up (see port-forward.log)."
done
log "port-forwards ready"

export DATABASE_URL="postgresql://pob:${PGPASSWORD:-pob}@127.0.0.1:$PG_PORT/pob"

replay_rc=0
"$PY" "$ROOT/tools/replay.py" --file "$ROOT/traffic/day1.jsonl" --url "http://127.0.0.1:$SERVICE_PORT" \
  --workers 8 --summary-json "$OUT/replay.json" || replay_rc=$?
if [[ $replay_rc -eq 2 ]]; then
  infra_error "replay could not start: the service never became ready or the traffic file is invalid."
fi

"$PY" "$ROOT/tools/export_ledger.py" --dsn "$DATABASE_URL" --out "$OUT/run.csv" \
  --rail-url "http://127.0.0.1:$RAIL_PORT" || infra_error "could not export the ledger from Postgres / the rail."

diff_rc=0
"$PY" "$ROOT/tools/diff_ledger.py" --baseline "$ROOT/baseline/ledger_baseline.csv" --run "$OUT/run.csv" \
  --report "$OUT/report.md" --rows-out "$OUT/diff_rows.csv" || diff_rc=$?
[[ $diff_rc -eq 2 ]] && infra_error "diff_ledger could not read the ledger files."

if [[ $replay_rc -ne 0 ]]; then
  # Replay errors or payouts that never settled are themselves a money change.
  {
    echo
    echo "> **Replay did not complete cleanly** (exit $replay_rc): some payments were not accepted or never"
    echo "> settled within 5 minutes. Pending payments show up above as status changes / missing payout legs."
    [[ -f "$OUT/replay.json" ]] && { echo; echo '```json'; cat "$OUT/replay.json"; echo '```'; }
  } >>"$OUT/report.md"
  [[ $diff_rc -eq 0 ]] && diff_rc=1
fi

if [[ $diff_rc -eq 0 ]]; then log "PASSED: ledger identical to baseline"; else log "FAILED: ledger changed"; fi
exit "$diff_rc"
