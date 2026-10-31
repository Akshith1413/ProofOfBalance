#!/usr/bin/env bash
# ProofOfBalance — stop everything that costs money.
#
#   scripts/teardown.sh             # destroy ALL AWS resources (keeps the state bucket) + stop local stacks
#   scripts/teardown.sh --pause     # only STOP the EC2 instance: no compute cost, keeps disk/IP/cluster
#                                   #   (EBS ~$2.4/month + Elastic IP ~$3.6/month still bill). setup.sh resumes it.
#   scripts/teardown.sh --local     # only stop docker compose and the kind gate cluster
#
# Options:
#   --yes             do not ask for confirmation
#   --purge-state     also delete the Terraform state bucket (every version). Irreversible.
#   --force-cleanup   if Terraform cannot destroy (lost state, errors), terminate tagged
#                     instances, release Elastic IPs and delete volumes directly
#
# Safe to re-run. After a destroy it lists anything tagged Project=ProofOfBalance
# that is still there, so you can be sure nothing is billing.
# Output: logs/teardown-<time>.log and logs/teardown-status.txt
set -Eeuo pipefail
source "$(dirname "$0")/lib/common.sh"
source "$(dirname "$0")/lib/tools.sh"
source "$(dirname "$0")/lib/tunnel.sh"

ACTION=destroy DO_AWS=true DO_LOCAL=true PURGE_STATE=false FORCE_CLEANUP=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pause) ACTION=pause; DO_LOCAL=false ;;
    --local) DO_AWS=false ;;
    --aws) DO_LOCAL=false ;;
    --purge-state) PURGE_STATE=true ;;
    --force-cleanup) FORCE_CLEANUP=true ;;
    --yes|-y) export ASSUME_YES=true ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 64 ;;
  esac
  shift
done

init_logging teardown
TF_DIR="$POB_ROOT/infra/terraform"
TFVARS="$SECRETS_DIR/terraform.tfvars.json"
HELPER() { "$POB_ROOT/.venv/bin/python" "$POB_ROOT/scripts/aws_helper.py" "$@"; }

stop_local() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker compose -f "$POB_ROOT/docker-compose.yml" down -v --remove-orphans 2>&1 | tail -n 3 || true
    if command -v kind >/dev/null 2>&1; then
      local c
      for c in $(kind get clusters 2>/dev/null | grep -E '^(pob-gate|gate)$' || true); do
        kind delete cluster --name "$c"
      done
    fi
    ok "local docker compose stack and kind gate clusters stopped"
  else
    info "Docker not running: no local stack to stop"
  fi
}

load_aws() {
  [[ -f "$POB_ROOT/.env" ]] || fail ".env not found" "Teardown needs the same AWS credentials as setup; restore .env."
  load_env "$POB_ROOT/.env"
  load_env "$SECRETS_DIR/generated.env" 2>/dev/null || true
  export AWS_REGION="${AWS_REGION:-ap-south-1}"
  export AWS_DEFAULT_REGION="$AWS_REGION"
  [[ -n "${AWS_SESSION_TOKEN:-}" ]] || unset AWS_SESSION_TOKEN
  [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]] ||
    fail "AWS credentials missing in .env" "Fill AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY and re-run."
  ensure_venv
  ensure_tools terraform
  local out
  out=$(HELPER whoami) || fail "AWS rejected the credentials" "Fix the keys in .env. Nothing was deleted."
  ACCOUNT_ID="${out%% *}"
  TF_STATE_BUCKET="${TF_STATE_BUCKET:-pob-tfstate-$ACCOUNT_ID}"
  info "AWS account $ACCOUNT_ID, region $AWS_REGION, state bucket $TF_STATE_BUCKET"
}

# Destroy needs values for required variables even though they no longer
# matter; reuse the last setup's file, or build harmless placeholders.
ensure_tfvars() {
  if [[ -f "$TFVARS" ]]; then return 0; fi
  warn "no saved .secrets/terraform.tfvars.json; using placeholder values (fine for destroy)"
  mkdir -p "$SECRETS_DIR"
  local key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPlaceholderPlaceholderPlaceholderPlace teardown"
  [[ -f "$SECRETS_DIR/pob_ed25519.pub" ]] && key=$(cat "$SECRETS_DIR/pob_ed25519.pub")
  cat >"$TFVARS" <<JSON
{"region": "$AWS_REGION", "my_ip": "${MY_IP:-127.0.0.1}", "ssh_public_key": "$key",
 "state_bucket": "$TF_STATE_BUCKET", "create_github_oidc_provider": true}
JSON
  chmod 600 "$TFVARS"
}

STATE_OK=false
tf_init() {
  if ! HELPER bucket-exists "$TF_STATE_BUCKET"; then
    warn "state bucket $TF_STATE_BUCKET does not exist: Terraform has nothing recorded"
    return 0
  fi
  ensure_tfvars
  if retry 3 5 terraform -chdir="$TF_DIR" init -input=false -reconfigure \
      -backend-config="bucket=$TF_STATE_BUCKET" -backend-config="region=$AWS_REGION" >/dev/null; then
    STATE_OK=true
    local n; n=$(terraform -chdir="$TF_DIR" state list 2>/dev/null | wc -l)
    info "Terraform state tracks $n resources"
  else
    warn "terraform init failed"
  fi
}

pause_instance() {
  [[ "$STATE_OK" == true ]] || fail "no Terraform state, nothing to pause" "Run scripts/setup.sh first."
  tunnel_stop "$(cat "$SECRETS_DIR/public_ip" 2>/dev/null || echo node)"
  "$POB_ROOT/.venv/bin/python" - "$TFVARS" <<'PY'
import json, sys
p = sys.argv[1]; v = json.load(open(p)); v["instance_state"] = "stopped"; json.dump(v, open(p, "w"), indent=2)
PY
  terraform -chdir="$TF_DIR" apply -input=false -auto-approve -lock-timeout=3m -var-file="$TFVARS" \
    -target=aws_ec2_instance_state.node
  "$POB_ROOT/.venv/bin/python" - "$TFVARS" <<'PY'
import json, sys
p = sys.argv[1]; v = json.load(open(p)); v["instance_state"] = "running"; json.dump(v, open(p, "w"), indent=2)
PY
  summary "instance stopped. Still billing: EBS volume and Elastic IP (~\$0.20/day). Resume: scripts/setup.sh"
}

DESTROY_OK=false
destroy_aws() {
  if [[ "$STATE_OK" == true ]]; then
    confirm "Destroy ALL ProofOfBalance AWS resources in $ACCOUNT_ID/$AWS_REGION (EC2, Elastic IP, VPC, IAM roles, budget)?" ||
      fail "cancelled by user" "Nothing was destroyed."
    local attempt
    for attempt in 1 2; do
      if terraform -chdir="$TF_DIR" destroy -input=false -auto-approve -lock-timeout=3m -var-file="$TFVARS"; then
        DESTROY_OK=true; break
      fi
      warn "terraform destroy attempt $attempt failed"; sleep 15
    done
  fi
  if [[ "$DESTROY_OK" != true ]]; then
    if [[ "$FORCE_CLEANUP" == true ]]; then
      warn "falling back to direct cleanup of tagged cost-incurring resources"
      HELPER force-cleanup
    else
      warn "Terraform could not destroy everything; checking what is left (re-run with --force-cleanup to remove it directly)"
    fi
  fi
  tunnel_stop "$(cat "$SECRETS_DIR/public_ip" 2>/dev/null || echo node)"
  rm -f "$SECRETS_DIR/kubeconfig" "$SECRETS_DIR/instance_id" "$SECRETS_DIR/public_ip" "$SECRETS_DIR/api_mode" "$SECRETS_DIR/postgres-initialised" "$SECRETS_DIR/known_hosts"
}

verify_aws() {
  local out
  out=$(HELPER find-tagged) || { warn "could not list remaining resources"; return 0; }
  echo "$out"
  local costly; costly=$(grep -E '^summary:' <<<"$out" | awk '{print $2}')
  if [[ "${costly:-0}" != 0 ]]; then
    summary "WARNING: $costly cost-incurring resource(s) still exist (see log). Re-run with --force-cleanup."
    fail "resources that cost money are still running" \
      "Re-run: scripts/teardown.sh --force-cleanup  (terminates tagged instances, releases Elastic IPs, deletes volumes)."
  fi
  summary "verified: no ProofOfBalance instances, Elastic IPs or volumes left in $AWS_REGION"
  [[ "$DESTROY_OK" == true ]] || summary "note: free resources (VPC/IAM/budget) may remain until terraform destroy succeeds"
}

purge_state() {
  echo
  warn "This permanently deletes s3://$TF_STATE_BUCKET and every version of the Terraform state."
  if [[ "${ASSUME_YES:-false}" != true ]]; then
    local answer
    [[ -t 0 ]] || fail "--purge-state needs a terminal to confirm" "Run it interactively."
    read -r -p "Type the bucket name to confirm: " answer
    [[ "$answer" == "$TF_STATE_BUCKET" ]] || fail "bucket name did not match; state kept" "Nothing was deleted."
  fi
  HELPER purge-bucket "$TF_STATE_BUCKET"
  summary "state bucket $TF_STATE_BUCKET deleted"
}

if [[ "$DO_LOCAL" == true ]]; then
  stage "Stop local stacks" "Could not stop docker compose / kind." stop_local
fi
if [[ "$DO_AWS" == true ]]; then
  stage "Check AWS credentials" "AWS rejected the credentials." load_aws
  stage "Terraform init" "Could not read the Terraform state." tf_init
  if [[ "$ACTION" == pause ]]; then
    stage "Pause EC2 instance" "Could not stop the instance." pause_instance
  else
    stage "Destroy AWS resources" "terraform destroy failed." destroy_aws
    stage "Verify nothing is billing" "Some resources were not removed." verify_aws
    [[ "$PURGE_STATE" == true ]] && stage "Delete state bucket" "Could not delete the state bucket." purge_state
    [[ "$PURGE_STATE" == true ]] || summary "kept: state bucket $TF_STATE_BUCKET (cents per month; --purge-state removes it)"
  fi
fi
printf '\n%s\n' "${_SUMMARY_LINES[@]}"
