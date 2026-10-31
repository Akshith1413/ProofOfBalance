#!/usr/bin/env bash
# ProofOfBalance — one command to create (or repair) the whole AWS environment.
#
#   scripts/setup.sh            # AWS: state bucket, Terraform (VPC, EC2 + k3s, OIDC, budget),
#                               #      kube-prometheus-stack, Argo Rollouts, the app, smoke test
#   scripts/setup.sh --local    # no AWS: docker compose stack on this machine + smoke test
#
# Options:
#   --skip-infra     do not run Terraform (cluster already exists)
#   --skip-app       stop after the cluster is ready
#   --yes            no interactive questions
#
# Safe to re-run at any time: every stage checks what exists and only creates
# or fixes what is missing or different. A stopped (paused) instance is started.
# Output: logs/setup-<time>.log, and logs/setup-status.txt explains any failure.
# Credentials and settings come from .env (see .env.example).
set -Eeuo pipefail
source "$(dirname "$0")/lib/common.sh"
source "$(dirname "$0")/lib/tools.sh"
source "$(dirname "$0")/lib/tunnel.sh"

MODE=aws SKIP_INFRA=false SKIP_APP=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --local) MODE=local ;;
    --skip-infra) SKIP_INFRA=true ;;
    --skip-app) SKIP_APP=true ;;
    --yes|-y) export ASSUME_YES=true ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 64 ;;
  esac
  shift
done

init_logging setup
TF_DIR="$POB_ROOT/infra/terraform"
NAMESPACE=pob
SSH_KEY="$SECRETS_DIR/pob_ed25519"
export KUBECONFIG="$SECRETS_DIR/kubeconfig"
TFVARS="$SECRETS_DIR/terraform.tfvars.json"
HELPER() { "$POB_ROOT/.venv/bin/python" "$POB_ROOT/scripts/aws_helper.py" "$@"; }

# ---------------------------------------------------------------- config ---
load_config() {
  if [[ ! -f "$POB_ROOT/.env" ]]; then
    cp "$POB_ROOT/.env.example" "$POB_ROOT/.env"; chmod 600 "$POB_ROOT/.env"
    fail ".env did not exist; created it from .env.example" \
      "Open .env, fill in AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (and optionally BUDGET_ALERT_EMAIL), then re-run."
  fi
  [[ "$(stat -c %a "$POB_ROOT/.env" 2>/dev/null || stat -f %Lp "$POB_ROOT/.env")" =~ ^6[04]0$ ]] || chmod 600 "$POB_ROOT/.env"
  load_env "$POB_ROOT/.env"
  load_env "$SECRETS_DIR/generated.env" 2>/dev/null || true

  export AWS_REGION="${AWS_REGION:-ap-south-1}"
  export AWS_DEFAULT_REGION="$AWS_REGION"
  [[ -n "${AWS_SESSION_TOKEN:-}" ]] || unset AWS_SESSION_TOKEN
  INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"
  MONTHLY_BUDGET_USD="${MONTHLY_BUDGET_USD:-10}"
  IMAGE_SOURCE="${IMAGE_SOURCE:-local}"
  IMAGE_TAG="${IMAGE_TAG:-latest}"
  GITHUB_REPO="${GITHUB_REPO:-Akshith1413/ProofOfBalance}"

  if [[ "$MODE" == aws ]]; then
    [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]] ||
      fail "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are empty in .env" "Fill both in .env and re-run."
    [[ "$AWS_ACCESS_KEY_ID" =~ ^(AKIA|ASIA)[A-Z0-9]{16}$ ]] ||
      fail "AWS_ACCESS_KEY_ID does not look like an access key id (AKIA... or ASIA..., 20 chars)" "Check for typos, quotes or spaces in .env."
    [[ ${#AWS_SECRET_ACCESS_KEY} -eq 40 ]] ||
      fail "AWS_SECRET_ACCESS_KEY should be 40 characters, got ${#AWS_SECRET_ACCESS_KEY}" "Check for a missing character or stray quote in .env."
    [[ "$AWS_ACCESS_KEY_ID" == ASIA* && -z "${AWS_SESSION_TOKEN:-}" ]] &&
      fail "ASIA... keys are temporary and need AWS_SESSION_TOKEN" "Add AWS_SESSION_TOKEN to .env or use long-lived IAM user keys."
    [[ "$MONTHLY_BUDGET_USD" =~ ^[0-9]+(\.[0-9]+)?$ ]] || fail "MONTHLY_BUDGET_USD must be a number" "Fix it in .env."
    [[ "$IMAGE_SOURCE" =~ ^(local|ghcr)$ ]] || fail "IMAGE_SOURCE must be local or ghcr" "Fix it in .env."
    if [[ -n "${BUDGET_ALERT_EMAIL:-}" && ! "$BUDGET_ALERT_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then
      fail "BUDGET_ALERT_EMAIL is not a valid email" "Fix it in .env or leave it empty."
    fi
    [[ -n "${BUDGET_ALERT_EMAIL:-}" ]] || warn "BUDGET_ALERT_EMAIL is empty: the \$${MONTHLY_BUDGET_USD} budget is created but will not email you."
  fi

  if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then POSTGRES_PASSWORD=$(random_secret); persist_generated POSTGRES_PASSWORD "$POSTGRES_PASSWORD"; fi
  if [[ -z "${GRAFANA_ADMIN_PASSWORD:-}" ]]; then GRAFANA_ADMIN_PASSWORD=$(random_secret); persist_generated GRAFANA_ADMIN_PASSWORD "$GRAFANA_ADMIN_PASSWORD"; fi
  [[ "$POSTGRES_PASSWORD" =~ ^[A-Za-z0-9._~-]+$ ]] ||
    fail "POSTGRES_PASSWORD may only contain letters, digits and . _ ~ - (it is embedded in a URL)" "Change it in .env."
  info "mode=$MODE region=$AWS_REGION instance=$INSTANCE_TYPE images=$IMAGE_SOURCE"
}

prerequisites() {
  require_cmd curl ssh ssh-keygen scp gzip
  ensure_venv
  if [[ "$MODE" == local ]]; then
    require_cmd docker
    docker info >/dev/null 2>&1 || fail "Docker is not running" "Start Docker and re-run."
    return 0
  fi
  ensure_tools kubectl helm terraform
  if [[ "$IMAGE_SOURCE" == local && "$SKIP_APP" != true ]]; then
    require_cmd docker
    docker info >/dev/null 2>&1 ||
      fail "Docker is not running but IMAGE_SOURCE=local needs it to build images" "Start Docker, or set IMAGE_SOURCE=ghcr in .env."
  fi
}

# ------------------------------------------------------------- local mode ---
local_up() {
  docker compose -f "$POB_ROOT/docker-compose.yml" up -d --build --wait --wait-timeout 300 || {
    docker compose -f "$POB_ROOT/docker-compose.yml" ps -a
    docker compose -f "$POB_ROOT/docker-compose.yml" logs --tail 60
    return 1; }
}

local_smoke() {
  "$POB_ROOT/.venv/bin/python" - <<'PY'
import time, uuid, httpx, sys
key = f"smoke-{uuid.uuid4()}"
body = {"idempotency_key": key, "corridor": "USD-INR", "usd_amount": "100.00", "client_id": 42,
        "beneficiary_id": 7, "occurred_at": "2026-11-02T10:00:00Z"}
r = httpx.post("http://localhost:8080/payments", json=body, timeout=10)
assert r.status_code == 201, r.text
pid = r.json()["id"]
assert httpx.post("http://localhost:8080/payments", json=body, timeout=10).status_code == 200
for _ in range(60):
    p = httpx.get(f"http://localhost:8080/payments/{pid}", timeout=10).json()
    if p["status"] != "pending":
        break
    time.sleep(0.5)
assert p["status"] == "paid" and len(p["postings"]) == 7, p
print(f"smoke test: payment {pid} paid with 7 balanced postings")
PY
}

# ------------------------------------------------------------------ AWS -----
check_aws() {
  local out
  out=$(HELPER whoami) || fail "AWS rejected the credentials in .env" \
    "The access key is wrong, deactivated or deleted. Create a new key in IAM -> Users -> Security credentials, put it in .env, re-run."
  ACCOUNT_ID="${out%% *}"
  info "AWS account $ACCOUNT_ID as ${out#* }"
  TF_STATE_BUCKET="${TF_STATE_BUCKET:-pob-tfstate-$ACCOUNT_ID}"
  [[ "$TF_STATE_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] ||
    fail "TF_STATE_BUCKET '$TF_STATE_BUCKET' is not a valid S3 bucket name" "Use 3-63 lower-case letters, digits, dots or dashes."
  summary "AWS account: $ACCOUNT_ID ($AWS_REGION)"
}

resolve_inputs() {
  # MY_IP may be one address or a comma-separated list. Empty = detect every
  # public IP this machine egresses from (some networks rotate between several).
  local ips="${MY_IP:-}"
  if [[ -z "$ips" ]]; then
    ips=$(HELPER my-ip) || fail "could not detect your public IP" "Set MY_IP=<your IPv4> in .env."
  fi
  ips="${ips// /}"
  local ip
  for ip in ${ips//,/ }; do
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || fail "MY_IP contains '$ip', which is not an IPv4 address" "Fix MY_IP in .env (comma-separated IPv4 addresses, no /32)."
  done
  MY_IP="${ips%%,*}"
  EXTRA_IPS=""
  [[ "$ips" == *,* ]] && EXTRA_IPS="${ips#*,}"
  info "allowing SSH and the Kubernetes API only from: ${ips//,/, } (each /32)"
  # Free-plan AWS accounts refuse non-Free-Tier types; a dry run finds one that works.
  local chosen
  chosen=$(HELPER pick-instance-type "$INSTANCE_TYPE") ||
    fail "no usable instance type" "See the message above: upgrade the AWS account plan or set INSTANCE_TYPE in .env."
  if [[ "$chosen" != "$INSTANCE_TYPE" ]]; then
    warn "using $chosen instead of $INSTANCE_TYPE (set INSTANCE_TYPE=$chosen in .env to silence this)"
    INSTANCE_TYPE="$chosen"
  fi
  info "instance type: $INSTANCE_TYPE"
  mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
  if [[ ! -f "$SSH_KEY" ]]; then
    ssh-keygen -q -t ed25519 -N "" -C "proofofbalance" -f "$SSH_KEY"
    info "generated SSH key $SSH_KEY"
  fi
  chmod 600 "$SSH_KEY"
}

# Only one GitHub OIDC provider may exist per account. Manage it if it is ours
# (in Terraform state) or absent; otherwise reuse the existing one.
detect_oidc() {
  CREATE_OIDC=true
  local exists in_state
  exists=$(HELPER oidc-exists) || fail "could not list IAM OIDC providers" "The IAM user needs iam:ListOpenIDConnectProviders."
  # Captured first: `terraform ... | grep -q` can SIGPIPE under pipefail and lie.
  in_state=$(terraform -chdir="$TF_DIR" state list 2>/dev/null || true)
  if [[ "$exists" == "true" && "$in_state" != *'aws_iam_openid_connect_provider.github[0]'* ]]; then
    CREATE_OIDC=false
    info "GitHub OIDC provider already exists in the account (created outside this stack); reusing it"
  fi
}

ensure_state_bucket() {
  HELPER ensure-bucket "$TF_STATE_BUCKET" || fail "could not create or verify the state bucket" \
    "Check the IAM user may create S3 buckets, or set TF_STATE_BUCKET to a bucket you own."
}

write_tfvars() {
  "$POB_ROOT/.venv/bin/python" - "$TFVARS" <<PY
import json, os, sys
v = {
  "region": os.environ["AWS_REGION"],
  "my_ip": "$MY_IP",
  "extra_admin_ips": [i for i in "$EXTRA_IPS".split(",") if i],
  "ssh_public_key": open("$SSH_KEY.pub").read().strip(),
  "instance_type": "$INSTANCE_TYPE",
  "instance_state": os.environ.get("POB_INSTANCE_STATE", "running"),
  "monthly_budget_usd": float("$MONTHLY_BUDGET_USD"),
  "budget_alert_email": os.environ.get("BUDGET_ALERT_EMAIL", ""),
  "owner": os.environ.get("OWNER_TAG") or "Akshith1413",
  "environment": os.environ.get("ENVIRONMENT_TAG") or "demo",
  "github_repo": "$GITHUB_REPO",
  "state_bucket": "$TF_STATE_BUCKET",
  "create_github_oidc_provider": "$CREATE_OIDC" == "true",
}
with open(sys.argv[1], "w") as fh:
    json.dump(v, fh, indent=2)
PY
  chmod 600 "$TFVARS"
}

tf_init() {
  retry 3 5 terraform -chdir="$TF_DIR" init -input=false -reconfigure \
    -backend-config="bucket=$TF_STATE_BUCKET" -backend-config="region=$AWS_REGION" >/dev/null ||
    fail "terraform init failed" "Usually network access to registry.terraform.io or S3; see the log."
}

tf_apply() {
  tf_init
  detect_oidc
  write_tfvars
  local attempt
  for attempt in 1 2; do
    if terraform -chdir="$TF_DIR" apply -input=false -auto-approve -lock-timeout=3m -var-file="$TFVARS"; then
      break
    fi
    if [[ $attempt -eq 2 ]]; then
      if grep -q "Error acquiring the state lock" "$LOG_FILE"; then
        fail "Terraform state is locked by another run" \
          "If no other setup/teardown is running, unlock with: terraform -chdir=infra/terraform force-unlock <LOCK_ID> (ID is in the log)."
      fi
      if grep -qE "UnauthorizedOperation|AccessDenied|not authorized" "$LOG_FILE"; then
        fail "Terraform was denied permission by AWS" \
          "The IAM user needs EC2, IAM (roles + OIDC provider), Budgets and S3 permissions. AdministratorAccess works for a sandbox account."
      fi
      if grep -q "not eligible for Free Tier" "$LOG_FILE"; then
        fail "this AWS account is on the Free plan and refuses $INSTANCE_TYPE" \
          "Set INSTANCE_TYPE=m7i-flex.large (8 GB, Free-Tier eligible) in .env, or upgrade the account plan in Billing."
      fi
      if grep -qE "VcpuLimitExceeded|InstanceLimitExceeded|InsufficientInstanceCapacity" "$LOG_FILE"; then
        fail "AWS has no capacity / quota for $INSTANCE_TYPE" "Try INSTANCE_TYPE=t3a.medium in .env, or request a vCPU quota increase."
      fi
      if grep -q "AddressLimitExceeded" "$LOG_FILE"; then
        fail "The account already uses all its Elastic IPs (default 5)" "Release unused Elastic IPs in the EC2 console and re-run."
      fi
      fail "terraform apply failed twice" "Read the Terraform error above. Re-running is safe; Terraform resumes where it stopped."
    fi
    warn "terraform apply failed (often IAM eventual consistency); retrying in 20s"
    sleep 20
  done
  PUBLIC_IP=$(terraform -chdir="$TF_DIR" output -raw public_ip)
  INSTANCE_ID=$(terraform -chdir="$TF_DIR" output -raw instance_id)
  summary "EC2 instance: $INSTANCE_ID at $PUBLIC_IP ($INSTANCE_TYPE)"
  summary "GitHub variable AWS_PLAN_ROLE_ARN = $(terraform -chdir="$TF_DIR" output -raw github_plan_role_arn)"
  summary "GitHub variable AWS_APPLY_ROLE_ARN = $(terraform -chdir="$TF_DIR" output -raw github_apply_role_arn)"
  summary "GitHub variable TF_STATE_BUCKET = $TF_STATE_BUCKET"
}

read_outputs() {
  tf_init
  PUBLIC_IP=$(terraform -chdir="$TF_DIR" output -raw public_ip 2>/dev/null) && [[ -n "$PUBLIC_IP" ]] ||
    fail "no Terraform outputs found" "Run without --skip-infra first."
  INSTANCE_ID=$(terraform -chdir="$TF_DIR" output -raw instance_id)
}

SSH_OPTS=()
ssh_setup() {
  SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15
            -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$SECRETS_DIR/known_hosts")
  # A replaced instance keeps the Elastic IP but has a new host key.
  if [[ -f "$SECRETS_DIR/instance_id" && "$(cat "$SECRETS_DIR/instance_id")" != "$INSTANCE_ID" ]]; then
    ssh-keygen -R "$PUBLIC_IP" -f "$SECRETS_DIR/known_hosts" >/dev/null 2>&1 || true
    info "instance changed; forgot the old SSH host key for $PUBLIC_IP"
  fi
  echo "$INSTANCE_ID" >"$SECRETS_DIR/instance_id"
}
on_node() { ssh "${SSH_OPTS[@]}" "ubuntu@$PUBLIC_IP" "$@"; }

fetch_kubeconfig() {
  local raw
  raw=$(on_node 'sudo cat /etc/rancher/k3s/k3s.yaml') || return 1
  [[ "$raw" == *"server: https://127.0.0.1:6443"* ]] || { warn "unexpected kubeconfig content from the node"; return 1; }
  # Written next to the live kubeconfig; wait_for_k3s swaps it in once the API
  # mode is known, so other kubectl sessions never see a half-decided file.
  ( umask 077; printf '%s\n' "${raw//127.0.0.1/$PUBLIC_IP}" >"$KUBECONFIG.new" )
}

wait_for_k3s() {
  ssh_setup
  info "waiting for SSH on $PUBLIC_IP (first boot takes 1-3 minutes)"
  wait_until 600 10 "SSH on $PUBLIC_IP" on_node true ||
    fail "cannot SSH to $PUBLIC_IP" "Your IP may have changed since the security group was written (re-run setup), or the instance failed to boot (check the EC2 console's system log)."
  info "waiting for k3s bootstrap to finish (cloud-init)"
  if ! wait_until 900 10 "k3s bootstrap" on_node test -f /var/lib/pob/bootstrap-done; then
    on_node 'sudo tail -n 60 /var/log/pob-bootstrap.log' || true
    fail "k3s did not finish installing" "See the bootstrap log above. Re-running setup is safe; a broken instance can be replaced with: terraform -chdir=infra/terraform apply -replace=aws_instance.node -var-file=.secrets/terraform.tfvars.json"
  fi
  retry 5 5 fetch_kubeconfig ||
    fail "could not copy the kubeconfig from the node" "SSH works only from the allowed IPs; if your public IP changed, re-run setup."
  echo "$PUBLIC_IP" >"$SECRETS_DIR/public_ip"
  # Direct first; many campus/corporate networks block outbound 6443, so fall
  # back to an SSH tunnel (port 22) instead of exposing the API more widely.
  local mode=direct
  if ! KUBECONFIG="$KUBECONFIG.new" probe_until 45 5 kubectl get --raw /readyz; then
    warn "port 6443 is not reachable from this network (firewall?); switching kubectl to an SSH tunnel"
    tunnel_start "$PUBLIC_IP" || fail "could not open an SSH tunnel to the node" \
      "SSH works but the tunnel failed. Is local port $TUNNEL_PORT free? Set POB_TUNNEL_PORT in .env to change it."
    kubeconfig_point_at "$KUBECONFIG.new" tunnel "$PUBLIC_IP"
    mv "$KUBECONFIG.new" "$KUBECONFIG"
    mode=tunnel
    wait_until 120 5 "Kubernetes API through the SSH tunnel" kubectl get --raw /readyz ||
      fail "Kubernetes API not reachable, directly or through SSH" "Check k3s on the node: ssh -i $SSH_KEY ubuntu@$PUBLIC_IP sudo k3s kubectl get nodes"
  fi
  if [[ -f "$KUBECONFIG.new" ]]; then mv "$KUBECONFIG.new" "$KUBECONFIG"; fi
  echo "$mode" >"$SECRETS_DIR/api_mode"
  wait_until 300 5 "node Ready" bash -c "kubectl get nodes --no-headers | grep -q ' Ready'"
  # CoreDNS only turns ready once pods can reach the API through its service IP:
  # a cheap end-to-end check of in-cluster networking before installing anything.
  if ! kubectl -n kube-system rollout status deploy/coredns --timeout=300s; then
    kubectl get endpoints kubernetes -o wide || true
    kubectl -n kube-system get pods -o wide || true
    fail "in-cluster networking is broken: pods cannot reach the Kubernetes API" \
      "The 'kubernetes' endpoint above must be the node's PRIVATE IP and the VPC must not overlap 10.42.0.0/16 or 10.43.0.0/16."
  fi
  kubectl get nodes -o wide
  summary "kubeconfig: export KUBECONFIG=$KUBECONFIG  (API access: $mode)"
  [[ "$mode" == tunnel ]] && summary "API tunnel: scripts/tunnel.sh start|stop|status  (needed for kubectl from this network)"
}

helm_repos() {
  retry 3 5 helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
  retry 3 5 helm repo add argo https://argoproj.github.io/argo-helm --force-update >/dev/null
  retry 3 5 helm repo update prometheus-community argo >/dev/null
}

# Recover releases stuck in failed/pending states from interrupted runs.
heal_release() {
  local rel="$1" ns="$2" status
  status=$(helm status "$rel" -n "$ns" -o json 2>/dev/null | "$POB_ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["info"]["status"])' 2>/dev/null || echo none)
  case "$status" in
    pending-install|pending-upgrade|pending-rollback)
      warn "release $rel is stuck in '$status' (an earlier run was interrupted); rolling back"
      helm rollback "$rel" -n "$ns" --wait --timeout 5m 2>/dev/null || helm uninstall "$rel" -n "$ns" --wait ;;
    failed)
      if [[ $(helm history "$rel" -n "$ns" -o json | "$POB_ROOT/.venv/bin/python" -c 'import json,sys; print(sum(r["status"]=="deployed" or r["status"]=="superseded" for r in json.load(sys.stdin)))') == 0 ]]; then
        warn "release $rel never deployed successfully; reinstalling it (persistent volumes are kept)"
        helm uninstall "$rel" -n "$ns" --wait
      fi ;;
  esac
}

install_monitoring() {
  local values="$SECRETS_DIR/monitoring-values.yaml"
  "$POB_ROOT/.venv/bin/python" - "$values" <<'PY'
import json, os, sys
discord, slack = os.environ.get("DISCORD_WEBHOOK_URL", ""), os.environ.get("SLACK_WEBHOOK_URL", "")
receiver = {"name": "chat"}
if discord:
    receiver["discord_configs"] = [{"webhook_url": discord, "send_resolved": True}]
if slack:
    receiver["slack_configs"] = [{"api_url": slack, "channel": os.environ.get("SLACK_CHANNEL") or "#alerts",
                                  "send_resolved": True, "title": "{{ .CommonLabels.alertname }} ({{ .Status }})",
                                  "text": "{{ range .Alerts }}{{ .Annotations.summary }}\n{{ end }}"}]
has_chat = bool(discord or slack)
values = {
    "grafana": {"adminPassword": os.environ["GRAFANA_ADMIN_PASSWORD"]},
    "alertmanager": {"config": {
        "global": {"resolve_timeout": "5m"},
        "route": {"receiver": "chat" if has_chat else "null", "group_by": ["alertname"],
                  "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
                  "routes": [{"matchers": ['alertname = "Watchdog"'], "receiver": "null"},
                             {"matchers": ['alertname = "InfoInhibitor"'], "receiver": "null"}]},
        "receivers": [{"name": "null"}] + ([receiver] if has_chat else []),
    }},
}
with open(sys.argv[1], "w") as fh:
    json.dump(values, fh, indent=2)  # JSON is valid YAML
PY
  chmod 600 "$values"
  [[ -n "${DISCORD_WEBHOOK_URL:-}${SLACK_WEBHOOK_URL:-}" ]] || warn "no DISCORD_WEBHOOK_URL / SLACK_WEBHOOK_URL: alerts stay inside Alertmanager"
  heal_release kube-prometheus-stack monitoring
  retry 2 20 helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
    --version "${KPS_CHART_VERSION:-92.16.1}" -n monitoring --create-namespace \
    -f "$POB_ROOT/monitoring/kube-prometheus-stack-values.yaml" -f "$values" \
    --wait --timeout 15m || { kubectl -n monitoring get pods; return 1; }
}

install_argo() {
  heal_release argo-rollouts argo-rollouts
  retry 2 20 helm upgrade --install argo-rollouts argo/argo-rollouts --version "${ARGO_CHART_VERSION:-2.43.8}" \
    -n argo-rollouts --create-namespace --set dashboard.enabled=false --wait --timeout 10m ||
    { kubectl -n argo-rollouts get pods; return 1; }
  # kubectl plugin (optional convenience)
  if ! command -v kubectl-argo-rollouts >/dev/null 2>&1; then
    local os arch; os=$(uname -s | tr '[:upper:]' '[:lower:]'); arch=$(uname -m); [[ "$arch" == x86_64 ]] && arch=amd64; [[ "$arch" == aarch64 ]] && arch=arm64
    if curl -fsSL --max-time 300 -o "$TOOLS_BIN/kubectl-argo-rollouts" \
        "https://github.com/argoproj/argo-rollouts/releases/download/v1.10.1/kubectl-argo-rollouts-${os}-${arch}"; then
      chmod +x "$TOOLS_BIN/kubectl-argo-rollouts"; info "installed kubectl argo rollouts plugin into .tools/bin"
    else
      warn "could not download the kubectl argo rollouts plugin (optional); continuing"
    fi
  fi
}

source_hash() {
  (cd "$POB_ROOT" && find "${@#"$POB_ROOT"/}" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 |
     LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16)
}

push_images() {
  if [[ "$IMAGE_SOURCE" == ghcr ]]; then
    local owner="${GITHUB_REPO%%/*}"; owner="${owner,,}"
    SERVICE_IMAGE="ghcr.io/$owner/proofofbalance-service"; RAIL_IMAGE="ghcr.io/$owner/proofofbalance-rail"
    IMG_TAG="$IMAGE_TAG"; PULL_POLICY=IfNotPresent
    [[ "$IMG_TAG" == latest ]] && PULL_POLICY=Always
    info "using $SERVICE_IMAGE:$IMG_TAG from GitHub Container Registry"
    return 0
  fi
  docker build --platform linux/amd64 -q -t pob/service:build "$POB_ROOT/service" >/dev/null
  docker build --platform linux/amd64 -q -t pob/rail:build "$POB_ROOT/rail" >/dev/null
  # Tag by a hash of the source files, not the image id: Docker Desktop's ids
  # include build attestations with timestamps, so they change on every build
  # and the "already on the node" check would never match.
  IMG_TAG="src-$(source_hash "$POB_ROOT/service" "$POB_ROOT/rail")"
  SERVICE_IMAGE=pob/service RAIL_IMAGE=pob/rail PULL_POLICY=IfNotPresent
  docker tag pob/service:build "pob/service:$IMG_TAG"; docker tag pob/rail:build "pob/rail:$IMG_TAG"
  local img
  for img in "pob/service:$IMG_TAG" "pob/rail:$IMG_TAG"; do
    if on_node "sudo k3s ctr -n k8s.io images ls -q | grep -qx 'docker.io/$img'"; then
      info "$img already on the node"
      continue
    fi
    info "copying $img to the node over SSH"
    docker save "$img" | gzip -1 | on_node 'gunzip | sudo k3s ctr -n k8s.io images import -' >/dev/null ||
      fail "could not import $img into k3s" "Check disk space on the node (df -h) and SSH connectivity."
  done
}

# Postgres keeps the password it was initialised with. If POSTGRES_PASSWORD
# changed since then, rotate it inside the database (local socket, trusted)
# before the Secret changes, so no ledger data is lost.
ROTATED_DB_PASSWORD=false
rotate_db_password_if_changed() {
  local marker="$SECRETS_DIR/postgres-initialised" want
  want=$(printf %s "$POSTGRES_PASSWORD" | sha256sum | cut -c1-16)
  kubectl -n "$NAMESPACE" get pvc data-pob-postgres-0 >/dev/null 2>&1 || return 0
  [[ -f "$marker" && "$(cat "$marker")" != "$want" ]] || return 0
  warn "POSTGRES_PASSWORD differs from the one the database was initialised with; rotating it in place"
  kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/pob-postgres-0 --timeout=180s >/dev/null 2>&1 ||
    fail "Postgres is not running, so its password cannot be rotated" \
      "Restore the previous POSTGRES_PASSWORD in .env (or .secrets/generated.env) and re-run."
  kubectl -n "$NAMESPACE" exec pob-postgres-0 -- psql -v ON_ERROR_STOP=1 -U pob -d pob -q \
    -c "ALTER USER pob WITH PASSWORD '$POSTGRES_PASSWORD';" ||
    fail "could not rotate the Postgres password" "Restore the previous POSTGRES_PASSWORD and re-run."
  echo "$want" >"$marker"
  ROTATED_DB_PASSWORD=true
}

restart_db_clients() {
  # Pods read the password at start; restart them (the Rollout without a canary).
  kubectl -n "$NAMESPACE" rollout restart deployment -l app.kubernetes.io/part-of=proofofbalance >/dev/null
  kubectl -n "$NAMESPACE" patch rollout pob-worker --type merge \
    -p "{\"spec\":{\"restartAt\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}" >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" rollout status deployment/pob-service --timeout=300s
  info "restarted database clients after the password rotation"
}

install_app() {
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  rotate_db_password_if_changed
  kubectl -n "$NAMESPACE" create secret generic pob-postgres-credentials \
    --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  gzip -9 -c "$POB_ROOT/traffic/day1.jsonl" >"$SECRETS_DIR/day1.jsonl.gz"
  kubectl -n "$NAMESPACE" create configmap pob-traffic --from-file=day1.jsonl.gz="$SECRETS_DIR/day1.jsonl.gz" \
    --dry-run=client -o yaml | kubectl apply --server-side --force-conflicts -f - >/dev/null
  local extra=()
  if [[ "$IMAGE_SOURCE" == ghcr && -n "${GHCR_TOKEN:-}" ]]; then
    kubectl -n "$NAMESPACE" create secret docker-registry ghcr-pull --docker-server=ghcr.io \
      --docker-username="${GHCR_USERNAME:-${GITHUB_REPO%%/*}}" --docker-password="$GHCR_TOKEN" \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    extra+=(--set "imagePullSecrets[0].name=ghcr-pull")
  fi
  heal_release pob "$NAMESPACE"
  if ! helm upgrade --install pob "$POB_ROOT/helm/proofofbalance" -n "$NAMESPACE" \
      -f "$POB_ROOT/helm/proofofbalance/values-aws.yaml" \
      --set images.service.repository="$SERVICE_IMAGE" --set images.service.tag="$IMG_TAG" \
      --set images.service.pullPolicy="$PULL_POLICY" \
      --set images.rail.repository="$RAIL_IMAGE" --set images.rail.tag="$IMG_TAG" \
      --set images.rail.pullPolicy="$PULL_POLICY" \
      "${extra[@]}" --wait --wait-for-jobs --timeout 15m 2> >(grep -v 'found symbolic link' >&2 || true); then
    kubectl -n "$NAMESPACE" get pods -o wide || true
    kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -30 || true
    kubectl -n "$NAMESPACE" logs -l app.kubernetes.io/part-of=proofofbalance --all-containers --tail=30 --prefix || true
    fail "the application did not become healthy within 15 minutes" \
      "See pod status/events above. ImagePullBackOff with IMAGE_SOURCE=ghcr: the image is missing or private (set GHCR_TOKEN). Re-running is safe."
  fi
  printf %s "$POSTGRES_PASSWORD" | sha256sum | cut -c1-16 >"$SECRETS_DIR/postgres-initialised"
  [[ "$ROTATED_DB_PASSWORD" == true ]] && restart_db_clients
  kubectl -n "$NAMESPACE" get pods
}

smoke_test() {
  local out
  out=$(helm test pob -n "$NAMESPACE" --timeout 5m --logs 2>&1) || {
    grep -v 'found symbolic link' <<<"$out"
    fail "helm test failed: the API is not ready" "kubectl -n pob logs deploy/pob-service"; }
  grep -v 'found symbolic link' <<<"$out" || true
  local pf_pid
  kubectl -n "$NAMESPACE" port-forward svc/pob-service 18080:80 >/dev/null 2>&1 & pf_pid=$!
  trap 'kill $pf_pid 2>/dev/null || true; _on_exit $?' EXIT
  wait_until 30 1 "port-forward" curl -fs http://127.0.0.1:18080/healthz
  local ok=0
  "$POB_ROOT/.venv/bin/python" - <<'PY' || ok=$?
import time, uuid, httpx
base = "http://127.0.0.1:18080"
key = f"smoke-{uuid.uuid4()}"
body = {"idempotency_key": key, "corridor": "USD-INR", "usd_amount": "100.00", "client_id": 42,
        "beneficiary_id": 7, "occurred_at": "2026-11-02T10:00:00Z"}
r = httpx.post(f"{base}/payments", json=body, timeout=15)
assert r.status_code == 201, r.text
pid = r.json()["id"]
assert httpx.post(f"{base}/payments", json=body, timeout=15).status_code == 200
for _ in range(120):
    p = httpx.get(f"{base}/payments/{pid}", timeout=15).json()
    if p["status"] != "pending":
        break
    time.sleep(0.5)
assert p["status"] == "paid" and len(p["postings"]) == 7, p
print(f"smoke test: payment {pid} paid with 7 balanced postings")
PY
  kill $pf_pid 2>/dev/null || true
  trap '_on_exit $?' EXIT
  [[ $ok -eq 0 ]] || fail "smoke payment did not settle" "kubectl -n pob logs deploy/pob-worker (or rollout pods) and deploy/pob-rail"
}

github_setup() {
  [[ -n "${GITHUB_TOKEN:-}" ]] || { info "GITHUB_TOKEN not set: skipping GitHub variables/labels/branch protection (see README)"; return 0; }
  "$POB_ROOT/.venv/bin/python" "$POB_ROOT/scripts/github_setup.py" \
    --repo "$GITHUB_REPO" --var "AWS_PLAN_ROLE_ARN=$(terraform -chdir="$TF_DIR" output -raw github_plan_role_arn)" \
    --var "AWS_APPLY_ROLE_ARN=$(terraform -chdir="$TF_DIR" output -raw github_apply_role_arn)" \
    --var "TF_STATE_BUCKET=$TF_STATE_BUCKET" --var "TF_VAR_MY_IP=$MY_IP" \
    --var "TF_VAR_EXTRA_ADMIN_IPS=$("$POB_ROOT/.venv/bin/python" -c 'import json,sys; print(json.dumps([i for i in sys.argv[1].split(",") if i]))' "$EXTRA_IPS")" \
    --var "TF_VAR_SSH_PUBLIC_KEY=$(cat "$SSH_KEY.pub")" --var "TF_VAR_BUDGET_ALERT_EMAIL=${BUDGET_ALERT_EMAIL:-}" ||
    warn "GitHub configuration failed (non-fatal); see above"
}

finish_aws() {
  summary "Grafana:   kubectl --kubeconfig $KUBECONFIG -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80  -> http://localhost:3000 (admin / see .secrets/generated.env or .env)"
  summary "API:       kubectl --kubeconfig $KUBECONFIG -n pob port-forward svc/pob-service 8080:80"
  summary "Canary:    kubectl --kubeconfig $KUBECONFIG argo rollouts -n pob get rollout pob-worker --watch"
  summary "SSH:       ssh -i $SSH_KEY ubuntu@$PUBLIC_IP"
  summary "COST:      $INSTANCE_TYPE + EBS + Elastic IP ~ \$1.25/day while running. Stop with: scripts/teardown.sh  (or --pause)"
  printf '\n%s\n' "${_SUMMARY_LINES[@]}"
}

# ---------------------------------------------------------------- run -------
stage "Load configuration" "A required value in .env is missing or invalid." load_config
stage "Prerequisites" "A required tool is missing and could not be installed." prerequisites

if [[ "$MODE" == local ]]; then
  stage "Start local stack" "docker compose could not start the stack (container logs above)." local_up
  stage "Smoke test" "A test payment was not accepted or not paid; see container logs: docker compose logs." local_smoke
  summary "API: http://localhost:8080  rail: http://localhost:8081  postgres: localhost:5432 (pob/pob)"
  summary "Ledger gate: scripts/local-gate.sh   Stop: scripts/teardown.sh --local"
  printf '\n%s\n' "${_SUMMARY_LINES[@]}"
  exit 0
fi

stage "Check AWS credentials" "AWS rejected the credentials." check_aws
stage "Resolve inputs" "Could not determine your IP, SSH key or OIDC provider state." resolve_inputs
stage "Terraform state bucket" "Could not create/verify the S3 state bucket." ensure_state_bucket
if [[ "$SKIP_INFRA" == true ]]; then
  stage "Read Terraform outputs" "Terraform has no outputs yet." read_outputs
else
  stage "Terraform apply (VPC, EC2 + k3s, OIDC, budget)" "Terraform could not create the infrastructure." tf_apply
fi
stage "Wait for k3s" "The EC2 node or k3s is not reachable." wait_for_k3s
[[ "$SKIP_APP" == true ]] && { finish_aws; exit 0; }
stage "Helm repositories" "Could not reach the Helm chart repositories." helm_repos
stage "Monitoring (Prometheus, Grafana, Alertmanager)" "kube-prometheus-stack did not become ready." install_monitoring
stage "Argo Rollouts" "The Argo Rollouts controller did not become ready." install_argo
stage "Application images" "Could not build or ship the application images." push_images
stage "Deploy ProofOfBalance" "The Helm release did not become healthy." install_app
stage "Smoke test" "The deployed API did not accept and settle a test payment." smoke_test
stage "GitHub repository settings" "GitHub configuration failed." github_setup
finish_aws
