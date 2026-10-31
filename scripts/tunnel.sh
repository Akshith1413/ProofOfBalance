#!/usr/bin/env bash
# Manage the SSH tunnel to the k3s API (used when your network blocks port 6443).
#   scripts/tunnel.sh start | stop | status
# Then: export KUBECONFIG=$PWD/.secrets/kubeconfig
set -Eeuo pipefail
source "$(dirname "$0")/lib/common.sh"
source "$(dirname "$0")/lib/tunnel.sh"

ip=$(cat "$SECRETS_DIR/public_ip" 2>/dev/null) || { error "no .secrets/public_ip: run scripts/setup.sh first"; exit 1; }
case "${1:-status}" in
  start)
    tunnel_start "$ip"
    kubeconfig_point_at "$SECRETS_DIR/kubeconfig" tunnel "$ip"
    echo tunnel >"$SECRETS_DIR/api_mode"
    ok "kubectl now uses 127.0.0.1:$TUNNEL_PORT. export KUBECONFIG=$SECRETS_DIR/kubeconfig" ;;
  stop)
    tunnel_stop "$ip" ;;
  status)
    if tunnel_status "$ip"; then ok "tunnel up: 127.0.0.1:$TUNNEL_PORT -> $ip:6443"; else warn "tunnel down (scripts/tunnel.sh start)"; exit 1; fi ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 64 ;;
esac
