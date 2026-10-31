"""Reconciler: every RECONCILE_INTERVAL_S seconds, count payments whose
postings do not sum to zero per currency, and export it as a gauge.

``python -m app.reconciler --once`` runs one check and exits 1 if anything is
unbalanced (handy in CI and game days).
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading
import time

from prometheus_client import start_http_server

from .config import load_settings
from .db import TRANSIENT_DB_ERRORS, make_pool
from .logs import setup_logging
from .metrics import RECONCILE_OK, UNBALANCED

log = logging.getLogger("pob.reconciler")

UNBALANCED_SQL = """
SELECT currency, count(*) AS n
FROM (
  SELECT payment_id, currency
  FROM postings
  GROUP BY payment_id, currency
  HAVING sum(amount) <> 0
) unbalanced
GROUP BY currency
"""

BASE_CURRENCIES = ("USD", "INR", "MXN", "BRL")


def check(pool) -> dict[str, int]:
    with pool.connection() as conn:
        rows = conn.execute(UNBALANCED_SQL).fetchall()
    return {r["currency"].strip(): r["n"] for r in rows}


def publish(found: dict[str, int], seen: set[str]) -> None:
    for cur in set(BASE_CURRENCIES) | seen | set(found):
        UNBALANCED.labels(cur).set(found.get(cur, 0))
    seen.update(found)
    RECONCILE_OK.set(time.time())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    settings = load_settings()
    setup_logging("reconciler", settings.log_level)
    pool = make_pool(settings.database_url, 2, "reconciler")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    if args.once:
        pool.wait(timeout=30)
        found = check(pool)
        log.info("reconciliation", extra={"unbalanced": found})
        raise SystemExit(1 if any(found.values()) else 0)

    start_http_server(settings.metrics_port)
    seen: set[str] = set()
    while not stop.is_set():
        try:
            found = check(pool)
            publish(found, seen)
            level = logging.ERROR if any(found.values()) else logging.INFO
            log.log(level, "reconciliation", extra={"unbalanced": found})
        except TRANSIENT_DB_ERRORS as exc:
            log.warning("reconciliation skipped, database unavailable: %s", exc)
        except Exception:
            log.exception("reconciliation failed")
        stop.wait(settings.reconcile_interval_s)
    pool.close()


if __name__ == "__main__":
    main()
