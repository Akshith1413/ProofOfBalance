#!/usr/bin/env python3
"""Regenerate monitoring/grafana-dashboard.json (python scripts/dev/build_dashboard.py)."""
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "${datasource}"}
NS = 'namespace=~"$namespace"'
_id = 0


def panel(kind, title, targets, x, y, w, h, unit=None, thresholds=None, desc="", **extra):
    global _id
    _id += 1
    p = {
        "id": _id, "type": kind, "title": title, "description": desc, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": l, "datasource": DS} for i, (e, l) in enumerate(targets)],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {},
    }
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    if thresholds:
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": thresholds}
    p.update(extra)
    return p


def stat(title, expr, x, y, desc, unit="short"):
    return panel("stat", title, [(expr, "")], x, y, 6, 5, unit=unit, desc=desc,
                 thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
                 options={"colorMode": "background", "graphMode": "none", "textMode": "value",
                          "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}})


panels = [
    stat("Unbalanced payments", f"sum(pob_ledger_unbalanced_payments{{{NS}}}) or vector(0)", 0, 0,
         "Payments whose postings do not sum to zero per currency (reconciler). Must be 0."),
    stat("Duplicate payouts (1h)", f"sum(increase(pob_rail_duplicate_transfers_total{{{NS}}}[1h])) or vector(0)", 6, 0,
         "Transfers the rail received for a payment it had already paid. Must be 0."),
    panel("stat", "Failed value ratio (5m)",
          [(f'(sum(rate(pob_settlement_value_usd_total{{{NS},status="failed"}}[5m])) or vector(0)) / '
            f'sum(rate(pob_settlement_value_usd_total{{{NS},status=~"paid|failed"}}[5m]))', "")], 12, 0, 6, 5, unit="percentunit",
          desc="Failed USD value / all settled (paid + failed) USD value. FailedValueHigh fires above 0.5%.",
          thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 0.0025}, {"color": "red", "value": 0.005}],
          options={"colorMode": "background", "graphMode": "area", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}),
    panel("stat", "Payments accepted (5m)", [(f'sum(increase(pob_payments_total{{{NS},status="pending"}}[5m]))', "")],
          18, 0, 6, 5, unit="short", desc="New payments accepted by the API in the last 5 minutes.",
          thresholds=[{"color": "blue", "value": None}],  # a count, not a health signal: never red
          options={"colorMode": "value", "graphMode": "area", "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}),
    panel("timeseries", "Settled value per corridor (USD / 5m)",
          [(f'sum by (corridor) (increase(pob_settlement_value_usd_total{{{NS},status="paid"}}[5m]))', "{{corridor}}")],
          0, 5, 12, 8, unit="currencyUSD"),
    panel("timeseries", "Failed value ratio per corridor: canary vs stable",
          [(f'(sum by (rollouts_pod_template_hash, corridor) (rate(pob_settlement_value_usd_total{{{NS},status=~"failed|retry"}}[2m])) or '
            f'sum by (rollouts_pod_template_hash, corridor) (rate(pob_settlement_value_usd_total{{{NS}}}[2m])) * 0) / '
            f'sum by (rollouts_pod_template_hash, corridor) (rate(pob_settlement_value_usd_total{{{NS}}}[2m]))',
            "{{corridor}} @ {{rollouts_pod_template_hash}}")],
          12, 5, 12, 8, unit="percentunit",
          desc="Value of failed or timed-out payout attempts / value of all attempts, per corridor and per Argo "
               "Rollouts pod-template-hash (the canary is the newest hash). This is what the canary analysis "
               "judges. Red line = 0.5% rollback threshold.",
          thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.005}],
          fieldConfig={"defaults": {"unit": "percentunit", "custom": {"thresholdsStyle": {"mode": "line"}},
                                    "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 0.005}]}},
                       "overrides": []}),
    panel("timeseries", "p95 settlement latency",
          [(f'histogram_quantile(0.95, sum by (le, corridor) (rate(pob_settlement_latency_seconds_bucket{{{NS}}}[5m])))', "{{corridor}}")],
          0, 13, 12, 8, unit="s", desc="SlowSettlement fires above 2s for 5m."),
    panel("timeseries", "Payments by outcome (per second)",
          [(f'sum by (status) (rate(pob_payments_total{{{NS}}}[5m]))', "{{status}}"),
           (f'sum(rate(pob_idempotent_replays_total{{{NS}}}[5m]))', "idempotent replay")],
          12, 13, 12, 8, unit="reqps"),
    panel("timeseries", "Pod restarts (1h)",
          [(f'sum by (pod) (increase(kube_pod_container_status_restarts_total{{{NS}}}[1h]))', "{{pod}}")],
          0, 21, 12, 8, unit="short"),
    panel("timeseries", "Rail transfers and retries",
          [(f'sum by (currency) (rate(pob_rail_transfers_total{{{NS}}}[5m]))', "transfers {{currency}}"),
           (f'sum by (reason) (rate(pob_rail_retries_total{{{NS}}}[5m]))', "retry: {{reason}}")],
          12, 21, 12, 8, unit="reqps"),
]

dashboard = {
    "uid": "proofofbalance", "title": "ProofOfBalance — settlement", "tags": ["proofofbalance", "payments"],
    "timezone": "utc", "schemaVersion": 39, "version": 1, "refresh": "30s", "editable": True,
    "time": {"from": "now-1h", "to": "now"},
    "templating": {"list": [
        {"name": "datasource", "label": "Data source", "type": "datasource", "query": "prometheus",
         "current": {"text": "Prometheus", "value": "prometheus"}, "hide": 0},
        {"name": "namespace", "label": "Namespace", "type": "custom", "query": "pob", "current": {"text": "pob", "value": "pob"},
         "options": [{"text": "pob", "value": "pob", "selected": True}], "hide": 0},
    ]},
    "annotations": {"list": []},
    "panels": panels,
}

out = Path(__file__).resolve().parents[2] / "monitoring" / "grafana-dashboard.json"
out.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {out} ({len(panels)} panels)")
