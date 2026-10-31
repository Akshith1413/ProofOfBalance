#!/usr/bin/env python3
"""Configure the GitHub repository for the ledger gate (optional; needs GITHUB_TOKEN).

  * Actions variables (AWS_PLAN_ROLE_ARN, TF_STATE_BUCKET, ...) for terraform.yml
  * the 'update-baseline' label
  * the 'production' environment used by terraform apply
  * branch protection on main requiring the unit-tests and ledger-gate checks

Each step is independent: one failure is reported and the rest still run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def call(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{API}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "proofofbalance-setup"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"message": raw.decode(errors="replace")[:200]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--var", action="append", default=[], help="NAME=VALUE")
    args = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("github_setup: GITHUB_TOKEN not set", file=sys.stderr)
        return 1
    repo, failures = args.repo, 0

    code, _ = call("GET", f"/repos/{repo}", token)
    if code != 200:
        print(f"github_setup: cannot access {repo} (HTTP {code}); check the repo exists and the token's scope")
        return 1

    for item in args.var:
        name, _, value = item.partition("=")
        if not value:
            continue
        code, _ = call("PATCH", f"/repos/{repo}/actions/variables/{name}", token, {"name": name, "value": value})
        if code == 404:
            code, _ = call("POST", f"/repos/{repo}/actions/variables", token, {"name": name, "value": value})
        ok = code in (201, 204)
        failures += not ok
        print(f"github_setup: variable {name}: {'set' if ok else f'FAILED (HTTP {code})'}")

    code, _ = call("POST", f"/repos/{repo}/labels", token,
                   {"name": "update-baseline", "color": "d93f0b",
                    "description": "Deliberately accepts a ledger (money) change"})
    print(f"github_setup: label update-baseline: {'ok' if code in (201, 422) else f'FAILED (HTTP {code})'}")

    code, _ = call("PUT", f"/repos/{repo}/environments/production", token, {})
    print(f"github_setup: environment production: {'ok' if code == 200 else f'FAILED (HTTP {code})'}"
          " (add required reviewers in Settings -> Environments)")

    code, _ = call("GET", f"/repos/{repo}/branches/main", token)
    if code != 200:
        print("github_setup: branch main not pushed yet; skipping branch protection (re-run setup after pushing)")
    else:
        code, resp = call("PUT", f"/repos/{repo}/branches/main/protection", token, {
            "required_status_checks": {"strict": True, "contexts": ["unit-tests", "ledger-gate"]},
            "enforce_admins": False,
            "required_pull_request_reviews": None,
            "restrictions": None,
        })
        ok = code == 200
        failures += not ok
        print(f"github_setup: protect main (required: unit-tests, ledger-gate): "
              f"{'ok' if ok else 'FAILED (HTTP %s: %s)' % (code, resp.get('message'))}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
