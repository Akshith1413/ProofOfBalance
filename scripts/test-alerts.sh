#!/usr/bin/env bash
# Prove that alerts leave the cluster. Needs the AWS environment (scripts/setup.sh).
#
#   scripts/test-alerts.sh            webhook in .env -> a test alert goes to Discord/Slack;
#                                     no webhook      -> an in-cluster receiver checks both formats
#   scripts/test-alerts.sh --sink     always use the in-cluster receiver
#
# The in-cluster test points Alertmanager's Discord AND Slack receivers at a tiny
# HTTP sink, fires a test alert through Alertmanager's API, checks that both
# payloads arrived, then rolls the Alertmanager config back. Result and the
# received payloads go to logs/test-alerts-status.txt.
set -Eeuo pipefail
source "$(dirname "$0")/lib/common.sh"

MODE=auto
case "${1:-}" in
  --sink) MODE=sink ;;
  "") ;;
  *) echo "usage: $0 [--sink]" >&2; exit 64 ;;
esac

init_logging test-alerts
load_env "$POB_ROOT/.env" || true
load_env "$SECRETS_DIR/generated.env" 2>/dev/null || true
export KUBECONFIG="$SECRETS_DIR/kubeconfig"
[[ "$MODE" == auto && -z "${DISCORD_WEBHOOK_URL:-}${SLACK_WEBHOOK_URL:-}" ]] && MODE=sink
[[ "$MODE" == auto ]] && MODE=real

NS=monitoring
AM=svc/kube-prometheus-stack-alertmanager
PF_PID=""
RESTORE_REV=""
cleanup() {
  [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true
  if [[ -n "$RESTORE_REV" ]]; then
    info "restoring Alertmanager config (helm rollback to revision $RESTORE_REV)"
    helm rollback kube-prometheus-stack "$RESTORE_REV" -n "$NS" --wait --timeout 10m >/dev/null 2>&1 ||
      warn "rollback failed: re-run scripts/setup.sh to restore the monitoring release"
  fi
  kubectl -n "$NS" delete deploy/alert-sink svc/alert-sink --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
# rc is assigned inside the trap string itself.
# shellcheck disable=SC2154
trap 'rc=$?; cleanup; _on_exit $rc' EXIT

check_cluster() {
  [[ -f "$KUBECONFIG" ]] || fail "no .secrets/kubeconfig" "Run scripts/setup.sh first."
  if ! kubectl get ns "$NS" >/dev/null 2>&1; then
    "$POB_ROOT/scripts/tunnel.sh" start >/dev/null 2>&1 || true
    kubectl get ns "$NS" >/dev/null 2>&1 || fail "cannot reach the cluster" "Is the instance running? scripts/setup.sh starts it."
  fi
}

start_sink() {
  kubectl -n "$NS" apply -f - >/dev/null <<'YAML'
apiVersion: apps/v1
kind: Deployment
metadata: { name: alert-sink, labels: { app: alert-sink } }
spec:
  replicas: 1
  selector: { matchLabels: { app: alert-sink } }
  template:
    metadata: { labels: { app: alert-sink } }
    spec:
      containers:
        - name: sink
          image: python:3.12-slim
          command: ["python", "-u", "-c"]
          args:
            - |
              import http.server, json
              class H(http.server.BaseHTTPRequestHandler):
                  def do_POST(self):
                      body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                      print("RECEIVED " + self.path + " " + json.dumps(json.loads(body or b"{}")), flush=True)
                      self.send_response(204); self.end_headers()
                  def log_message(self, *a): pass
              http.server.HTTPServer(("", 8080), H).serve_forever()
          ports: [{ containerPort: 8080 }]
---
apiVersion: v1
kind: Service
metadata: { name: alert-sink }
spec:
  selector: { app: alert-sink }
  ports: [{ port: 8080, targetPort: 8080 }]
YAML
  kubectl -n "$NS" rollout status deploy/alert-sink --timeout=180s
}

point_alertmanager_at_sink() {
  RESTORE_REV=$(helm history kube-prometheus-stack -n "$NS" -o json | "$POB_ROOT/.venv/bin/python" -c \
    'import json,sys; print([r["revision"] for r in json.load(sys.stdin) if r["status"]=="deployed"][-1])')
  local sink=http://alert-sink.$NS:8080
  helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack -n "$NS" \
    --version "${KPS_CHART_VERSION:-92.16.1}" --reuse-values --wait --timeout 10m \
    --set-json "alertmanager.config.receivers=[{\"name\":\"null\"},{\"name\":\"chat\",\"discord_configs\":[{\"webhook_url\":\"$sink/discord\",\"send_resolved\":true}],\"slack_configs\":[{\"api_url\":\"$sink/slack\",\"channel\":\"#alerts\",\"send_resolved\":true,\"title\":\"{{ .CommonLabels.alertname }} ({{ .Status }})\",\"text\":\"{{ range .Alerts }}{{ .Annotations.summary }}\\n{{ end }}\"}]}]" \
    --set alertmanager.config.route.receiver=chat >/dev/null
}

fire_test_alert() {
  kubectl -n "$NS" port-forward "$AM" 19093:9093 >/dev/null 2>&1 &
  PF_PID=$!
  wait_until 60 2 "Alertmanager port-forward" curl -sf http://127.0.0.1:19093/-/ready
  # Alertmanager reloads its config a few seconds after the Secret changes.
  wait_until 120 5 "Alertmanager to load the chat receiver" bash -c \
    'curl -sf http://127.0.0.1:19093/api/v2/status | grep -q "receiver: chat"'
  local now; now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  curl -sf -XPOST -H 'Content-Type: application/json' http://127.0.0.1:19093/api/v2/alerts -d "[{
    \"labels\": {\"alertname\": \"PobAlertDeliveryTest\", \"severity\": \"info\", \"namespace\": \"pob\"},
    \"annotations\": {\"summary\": \"ProofOfBalance alert delivery test ($now)\"},
    \"startsAt\": \"$now\"}]" >/dev/null || fail "Alertmanager rejected the test alert"
  ok "test alert PobAlertDeliveryTest sent to Alertmanager"
}

verify_sink() {
  local got
  wait_until 120 5 "both payloads at the sink" bash -c \
    "kubectl -n $NS logs deploy/alert-sink | grep -q 'RECEIVED /discord .*PobAlertDeliveryTest' &&
     kubectl -n $NS logs deploy/alert-sink | grep -q 'RECEIVED /slack .*PobAlertDeliveryTest'"
  got=$(kubectl -n "$NS" logs deploy/alert-sink | grep RECEIVED)
  echo "$got" | cut -c1-400
  summary "Discord payload received: $(echo "$got" | grep -c '/discord') ; Slack payload received: $(echo "$got" | grep -c '/slack')"
  summary "Discord format: $(echo "$got" | grep -m1 '/discord' | grep -o '"embeds"' | head -1 || echo missing) ; Slack format: $(echo "$got" | grep -m1 '/slack' | grep -o '"attachments"' | head -1 || echo missing)"
  summary "Set DISCORD_WEBHOOK_URL or SLACK_WEBHOOK_URL in .env and re-run scripts/setup.sh to deliver to a real channel."
}

stage "Check cluster" "The cluster is not reachable." check_cluster
if [[ "$MODE" == sink ]]; then
  stage "Start in-cluster receiver" "Could not start the alert-sink pod (image pull?)." start_sink
  stage "Point Alertmanager at the receiver" "helm upgrade of kube-prometheus-stack failed." point_alertmanager_at_sink
  stage "Fire a test alert" "Alertmanager did not accept or load the config." fire_test_alert
  stage "Verify Discord and Slack payloads" "Alertmanager did not deliver to the receiver within 2 minutes; see kubectl -n monitoring logs statefulset/alertmanager-kube-prometheus-stack-alertmanager." verify_sink
else
  stage "Fire a test alert" "Alertmanager did not accept the alert." fire_test_alert
  summary "Test alert sent through Alertmanager to $([[ -n "${DISCORD_WEBHOOK_URL:-}" ]] && echo Discord) $([[ -n "${SLACK_WEBHOOK_URL:-}" ]] && echo Slack). It arrives after group_wait (30s): check the channel."
fi
