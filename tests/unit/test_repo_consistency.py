"""Things that must stay in sync across the repository."""
import json
import os
from decimal import Decimal

import yaml

from app.rates import load_rates, parse_rates


def test_chart_rates_match_service_rates(root):
    values = yaml.safe_load((root / "helm/proofofbalance/values.yaml").read_text())
    chart = parse_rates({"fee_bps": values["rates"]["fee_bps"], "corridors": values["rates"]["corridors"]})
    assert chart == load_rates(root / "service/rates.yaml")


def test_chart_symlinks_point_at_single_sources(root):
    files = root / "helm/proofofbalance/files"
    for link, target in [("migrations", "db/migrations"), ("migrate.sh", "db/migrate.sh"),
                         ("alert-rules.yaml", "monitoring/alert-rules.yaml"),
                         ("grafana-dashboard.json", "monitoring/grafana-dashboard.json")]:
        assert (files / link).is_symlink(), link
        assert (files / link).resolve() == (root / target).resolve()


def test_alert_rules_cover_the_spec(root):
    rules = yaml.safe_load((root / "monitoring/alert-rules.yaml").read_text())
    names = {r["alert"]: r for g in rules["groups"] for r in g["rules"]}
    assert {"LedgerUnbalanced", "DuplicatePayout", "FailedValueHigh", "SlowSettlement"} <= set(names)
    assert names["LedgerUnbalanced"]["labels"]["severity"] == "critical"
    assert names["DuplicatePayout"]["labels"]["severity"] == "critical"


def test_dashboard_is_valid_json_with_required_panels(root):
    d = json.loads((root / "monitoring/grafana-dashboard.json").read_text())
    titles = " | ".join(p["title"] for p in d["panels"]).lower()
    for needle in ("unbalanced", "duplicate", "failed value", "settled value", "p95", "restarts"):
        assert needle in titles, needle


def test_baseline_is_balanced_and_sorted(root):
    import csv
    rows = list(csv.DictReader((root / "baseline/ledger_baseline.csv").open()))
    assert rows, "baseline is empty"
    keys = [(r["idempotency_key"], r["leg"], r["account"]) for r in rows]
    assert keys == sorted(keys) and len(keys) == len(set(keys))
    totals = {}
    for r in rows:
        if r["leg"] != "rail":
            k = (r["idempotency_key"], r["currency"])
            totals[k] = totals.get(k, Decimal(0)) + Decimal(r["amount"])
    assert not {k: v for k, v in totals.items() if v != 0}
    assert {r["amount"] for r in rows if r["leg"] == "rail"} == {"1"}, "baseline must have no duplicate payouts"
    assert len({r["idempotency_key"] for r in rows}) == 4850


def test_env_example_has_no_secrets(root):
    for line in (root / ".env.example").read_text().splitlines():
        if line.startswith(("AWS_ACCESS_KEY_ID=", "AWS_SECRET_ACCESS_KEY=", "GHCR_TOKEN=", "GITHUB_TOKEN=")):
            assert line.endswith("="), line


def test_scripts_are_executable(root):
    for p in ["scripts/setup.sh", "scripts/teardown.sh", "scripts/local-gate.sh", "scripts/kind-gate.sh",
              "scripts/ci/gate.sh", "scripts/ci/pr_comment.sh", "tools/replay.py", "tools/export_ledger.py",
              "tools/diff_ledger.py", "traffic/generator.py"]:
        assert os.access(root / p, os.X_OK), p
