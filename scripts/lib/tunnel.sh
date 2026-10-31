#!/usr/bin/env bash
# SSH tunnel to the k3s API for networks that block outbound port 6443
# (common on campus/corporate networks; SSH on 22 usually still works).
# kubectl then talks to https://127.0.0.1:$TUNNEL_PORT, which the k3s
# certificate already covers. Source common.sh first.

TUNNEL_PORT="${POB_TUNNEL_PORT:-16443}"
TUNNEL_SOCK="$SECRETS_DIR/ssh-tunnel.sock"
SSH_KEY="${SSH_KEY:-$SECRETS_DIR/pob_ed25519}"

_tunnel_ssh_opts() {
  printf '%s\n' -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=4 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$SECRETS_DIR/known_hosts"
}

tunnel_status() {
  # tunnel_status <public-ip>: 0 if the tunnel's control master is alive
  [[ -S "$TUNNEL_SOCK" ]] && ssh -S "$TUNNEL_SOCK" -O check "ubuntu@$1" >/dev/null 2>&1
}

tunnel_start() {
  local ip="$1" opts
  if tunnel_status "$ip"; then
    info "SSH tunnel to $ip already running (127.0.0.1:$TUNNEL_PORT -> 6443)"
    return 0
  fi
  rm -f "$TUNNEL_SOCK"
  if (exec 3<>"/dev/tcp/127.0.0.1/$TUNNEL_PORT") 2>/dev/null; then
    error "local port $TUNNEL_PORT is already in use by another program"
    return 1
  fi
  mapfile -t opts < <(_tunnel_ssh_opts)
  ssh "${opts[@]}" -f -N -M -S "$TUNNEL_SOCK" -o ExitOnForwardFailure=yes \
    -L "127.0.0.1:$TUNNEL_PORT:127.0.0.1:6443" "ubuntu@$ip" || return 1
  info "SSH tunnel started: 127.0.0.1:$TUNNEL_PORT -> $ip:6443"
}

tunnel_stop() {
  local ip="${1:-node}"
  if [[ -S "$TUNNEL_SOCK" ]]; then
    ssh -S "$TUNNEL_SOCK" -O exit "ubuntu@$ip" >/dev/null 2>&1 || true
    rm -f "$TUNNEL_SOCK"
    info "SSH tunnel stopped"
  fi
}

# kubeconfig_point_at <kubeconfig> <direct|tunnel> <public-ip>
kubeconfig_point_at() {
  local file="$1" mode="$2" ip="$3" server
  if [[ "$mode" == tunnel ]]; then server="https://127.0.0.1:$TUNNEL_PORT"; else server="https://$ip:6443"; fi
  sed -i.bak -E "s#server: https://[^ ]+#server: $server#" "$file" && rm -f "$file.bak"
}
