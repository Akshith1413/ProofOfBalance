#!/usr/bin/env bash
# Install pinned CLI tools into .tools/bin (checksum-verified) when they are
# missing from PATH. Nothing is installed system-wide and no sudo is needed.
# Source common.sh first.

KUBECTL_VERSION="${KUBECTL_VERSION:-v1.37.2}"
HELM_VERSION="${HELM_VERSION:-v3.22.1}"
TERRAFORM_VERSION="${TERRAFORM_VERSION:-1.17.1}"
KIND_VERSION="${KIND_VERSION:-v0.33.0}"

_os() { uname -s | tr '[:upper:]' '[:lower:]'; }
_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) fail "unsupported CPU architecture $(uname -m)" "Install kubectl/helm/terraform manually and re-run." ;;
  esac
}

_sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi
}

_download() {
  # _download <url> <dest>
  retry 3 3 curl -fsSL --connect-timeout 15 --max-time 600 -o "$2" "$1"
}

_verify() {
  # _verify <file> <expected-sha256>
  local got; got="$(_sha256 "$1")"
  if [[ -z "$2" || "$got" != "$2" ]]; then
    error "checksum mismatch for $(basename "$1"): expected '${2:-<none>}', got '$got'"
    return 1
  fi
}

_version_ge() {
  # _version_ge 1.17.1 1.10.0  -> true
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

_install_kubectl() {
  local os arch tmp; os=$(_os); arch=$(_arch); tmp=$(mktemp -d)
  local url="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${os}/${arch}/kubectl"
  _download "$url" "$tmp/kubectl" && _download "$url.sha256" "$tmp/kubectl.sha256" &&
    _verify "$tmp/kubectl" "$(tr -d '[:space:]' <"$tmp/kubectl.sha256")" &&
    install -m 0755 "$tmp/kubectl" "$TOOLS_BIN/kubectl"
  local rc=$?; rm -rf "$tmp"; return $rc
}

_install_helm() {
  local os arch tmp; os=$(_os); arch=$(_arch); tmp=$(mktemp -d)
  local tgz="helm-${HELM_VERSION}-${os}-${arch}.tar.gz"
  _download "https://get.helm.sh/$tgz" "$tmp/$tgz" &&
    _download "https://get.helm.sh/$tgz.sha256sum" "$tmp/$tgz.sha256sum" &&
    _verify "$tmp/$tgz" "$(awk '{print $1}' "$tmp/$tgz.sha256sum")" &&
    tar -xzf "$tmp/$tgz" -C "$tmp" && install -m 0755 "$tmp/${os}-${arch}/helm" "$TOOLS_BIN/helm"
  local rc=$?; rm -rf "$tmp"; return $rc
}

_install_terraform() {
  local os arch tmp; os=$(_os); arch=$(_arch); tmp=$(mktemp -d)
  local zip="terraform_${TERRAFORM_VERSION}_${os}_${arch}.zip"
  local base="https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}"
  _download "$base/$zip" "$tmp/$zip" &&
    _download "$base/terraform_${TERRAFORM_VERSION}_SHA256SUMS" "$tmp/SUMS" &&
    _verify "$tmp/$zip" "$(awk -v f="$zip" '$2==f {print $1}' "$tmp/SUMS")" &&
    python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract('terraform', sys.argv[2])" "$tmp/$zip" "$tmp" &&
    install -m 0755 "$tmp/terraform" "$TOOLS_BIN/terraform"
  local rc=$?; rm -rf "$tmp"; return $rc
}

_install_kind() {
  local os arch tmp; os=$(_os); arch=$(_arch); tmp=$(mktemp -d)
  local url="https://github.com/kubernetes-sigs/kind/releases/download/${KIND_VERSION}/kind-${os}-${arch}"
  _download "$url" "$tmp/kind" && _download "$url.sha256sum" "$tmp/kind.sha256sum" &&
    _verify "$tmp/kind" "$(awk '{print $1}' "$tmp/kind.sha256sum")" &&
    install -m 0755 "$tmp/kind" "$TOOLS_BIN/kind"
  local rc=$?; rm -rf "$tmp"; return $rc
}

_tool_ok() {
  local tool="$1"
  command -v "$tool" >/dev/null 2>&1 || return 1
  case "$tool" in
    terraform)
      local v; v=$(terraform version -json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["terraform_version"])' 2>/dev/null)
      [[ -n "$v" ]] && _version_ge "$v" "1.10.0" ;;
    helm)
      helm version --short 2>/dev/null | grep -Eq '^v(3|4)\.' ;;
    *) return 0 ;;
  esac
}

# ensure_tools kubectl helm terraform kind ...
ensure_tools() {
  mkdir -p "$TOOLS_BIN"
  local tool
  for tool in "$@"; do
    if _tool_ok "$tool"; then
      info "$tool: $(command -v "$tool")"
      continue
    fi
    info "installing $tool into .tools/bin (pinned, checksum-verified)"
    if ! "_install_$tool"; then
      fail "could not install $tool" \
        "Downloading $tool failed (network/proxy?). Install $tool yourself and make sure it is on PATH, then re-run."
    fi
    _tool_ok "$tool" || fail "$tool installed but not usable" "Check $TOOLS_BIN/$tool runs on this machine."
    ok "$tool installed: $(command -v "$tool")"
  done
}

# ensure_venv: .venv with the Python tool dependencies (+ boto3 for AWS checks).
ensure_venv() {
  local req_hash marker="$POB_ROOT/.venv/.requirements.sha256"
  command -v python3 >/dev/null || fail "python3 not found" "Install Python 3.10+ and re-run."
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' ||
    fail "Python $(python3 -V) is too old" "Install Python 3.10 or newer."
  req_hash=$(cat "$POB_ROOT/tools/requirements.txt" "$POB_ROOT/scripts/requirements.txt" | _sha256 /dev/stdin)
  if [[ -x "$POB_ROOT/.venv/bin/python" && -f "$marker" && "$(cat "$marker")" == "$req_hash" ]]; then
    info "python venv up to date"
    return 0
  fi
  [[ -x "$POB_ROOT/.venv/bin/python" ]] || python3 -m venv "$POB_ROOT/.venv" ||
    fail "could not create .venv" "Install the python3-venv package (Debian/Ubuntu: sudo apt install python3-venv)."
  retry 3 5 "$POB_ROOT/.venv/bin/pip" install -q --disable-pip-version-check \
    -r "$POB_ROOT/tools/requirements.txt" -r "$POB_ROOT/scripts/requirements.txt" ||
    fail "pip install failed" "Check network access to pypi.org."
  echo "$req_hash" >"$marker"
  ok "python venv ready"
}
