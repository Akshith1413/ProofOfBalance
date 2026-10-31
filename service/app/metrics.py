"""Prometheus metrics. Each process only increments the ones it owns."""
from prometheus_client import Counter, Gauge, Histogram

# service
PAYMENTS = Counter("pob_payments_total", "Payments seen by the API, by outcome", ["corridor", "status"])
IDEMPOTENT_REPLAYS = Counter("pob_idempotent_replays_total", "Requests that reused an idempotency key", ["corridor"])

# payout worker
SETTLEMENT_VALUE = Counter(
    "pob_settlement_value_usd_total",
    "USD value of payout attempts by outcome: paid / failed (final) / retry (timeout or 5xx, will be retried)",
    ["corridor", "status"],
)
SETTLEMENT_LATENCY = Histogram(
    "pob_settlement_latency_seconds",
    "Time for one rail transfer call",
    ["corridor"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
RAIL_RETRIES = Counter("pob_rail_retries_total", "Rail calls that failed transiently and will be retried", ["corridor", "reason"])

# reconciler
UNBALANCED = Gauge("pob_ledger_unbalanced_payments", "Payments whose postings do not sum to zero", ["currency"])
RECONCILE_OK = Gauge("pob_reconciler_last_success_timestamp_seconds", "Unix time of the last successful check")

KNOWN_CORRIDORS = ("USD-INR", "USD-MXN", "USD-BRL")
