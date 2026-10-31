#!/usr/bin/env bash
# Run the exact CI ledger gate on your machine: kind cluster -> Helm chart ->
# replay -> diff. Pass Helm --set overrides to simulate a pull request.
#
#   scripts/kind-gate.sh
#   scripts/kind-gate.sh --set settlement.roundingMode=ROUND_HALF_UP
#   scripts/kind-gate.sh --keep          # leave the kind cluster running
#
# Exit: 0 ledger identical, 1 ledger changed, other = the gate could not run.
set -Eeuo pipefail
source "$(dirname "$0")/lib/common.sh"
source "$(dirname "$0")/lib/tools.sh"

CLUSTER=pob-gate KEEP=false OUT="$POB_ROOT/out/kind-gate"
HELM_SETS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --set) HELM_SETS+=(--set "$2"); shift ;;
    --keep) KEEP=true ;;
    --out) OUT="$2"; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
  shift
done

init_logging kind-gate
mkdir -p "$OUT"
export KUBECONFIG="$OUT/kubeconfig"

cleanup_cluster() { [[ "$KEEP" == true ]] || kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; }

prepare() {
  require_cmd docker
  docker info >/dev/null 2>&1 || fail "docker daemon not reachable" "Start Docker and re-run."
  ensure_tools kubectl helm kind
  ensure_venv
}

build_images() {
  docker build -q -t pob/service:ci "$POB_ROOT/service" && docker build -q -t pob/rail:ci "$POB_ROOT/rail"
}

create_cluster() {
  local clusters
  clusters=$(kind get clusters 2>/dev/null || true)
  if grep -qx "$CLUSTER" <<<"$clusters"; then
    warn "kind cluster $CLUSTER exists from an earlier run; recreating it for a clean ledger"
    kind delete cluster --name "$CLUSTER"
  fi
  retry 2 10 kind create cluster --name "$CLUSTER" --wait 120s
}

load_images() {
  kind load docker-image pob/service:ci pob/rail:ci --name "$CLUSTER"
  # Side-loading Postgres saves a slow in-cluster pull, but kind cannot import
  # multi-platform images from Docker Desktop's containerd store; then the
  # cluster simply pulls it itself.
  if docker image inspect postgres:16 >/dev/null 2>&1; then
    kind load docker-image postgres:16 --name "$CLUSTER" 2>/dev/null ||
      info "postgres:16 could not be side-loaded (multi-platform image); the cluster will pull it"
  fi
}

install_chart() {
  if ! helm upgrade --install pob "$POB_ROOT/helm/proofofbalance" -f "$POB_ROOT/helm/proofofbalance/values-ci.yaml" \
      "${HELM_SETS[@]}" --wait --wait-for-jobs --timeout 10m 2> >(grep -v 'found symbolic link' >&2 || true); then
    kubectl get pods -o wide; kubectl describe pods | tail -80; kubectl logs -l app.kubernetes.io/part-of=proofofbalance --all-containers --tail=50 || true
    return 1
  fi
  kubectl get pods
}

stage "Prerequisites" "Docker, kind, kubectl, helm or the Python tools are missing." prepare
stage "Build images" "docker build failed for service/ or rail/." build_images
# shellcheck disable=SC2154  # _rc is set inside the trap
trap '_rc=$?; cleanup_cluster; _on_exit $_rc' EXIT
stage "Create kind cluster" "kind could not create a cluster (Docker memory? needs ~2 GB free)." create_cluster
stage "Load images into kind" "kind could not import the freshly built pob/service:ci or pob/rail:ci images." load_images
stage "Install chart" "helm install did not become ready in 10 minutes; pod diagnostics are above." install_chart

_CURRENT_STAGE="Ledger gate"; _CURRENT_HINT="The replayed ledger differs from the baseline (exit 1) or the gate could not run (exit 3). See $OUT/report.md."
set +e
PYTHON="$POB_ROOT/.venv/bin/python" "$POB_ROOT/scripts/ci/gate.sh" --out "$OUT"
rc=$?
set -e
echo; cat "$OUT/report.md"
summary "helm overrides: ${HELM_SETS[*]:-none}"
summary "report: $OUT/report.md"
exit $rc
