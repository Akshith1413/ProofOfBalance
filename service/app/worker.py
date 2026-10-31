"""Payout worker.

Claims one pending payment at a time with FOR UPDATE SKIP LOCKED, calls the
rail while holding the row lock, then marks it paid (adding the two payout
postings) or failed, all in the same transaction.

Outcomes of a rail call:
  200          paid   -> status 'paid', payout legs written
  422          failed -> status 'failed' (the rail refused; retrying cannot help)
  timeout/5xx  unknown -> attempts += 1, retry later with backoff; after
               MAX_ATTEMPTS mark 'failed'. If the rail actually paid before the
               timeout, the retry pays again: that duplicate is exactly what the
               rail's duplicate counter and the canary analysis are there to catch.
"""
from __future__ import annotations

import logging
import signal
import threading
import time

import httpx
import psycopg
from prometheus_client import start_http_server

from . import money
from .config import Settings, load_settings
from .db import TRANSIENT_DB_ERRORS, make_pool, schema_ready
from .logs import setup_logging
from .metrics import RAIL_RETRIES, SETTLEMENT_LATENCY, SETTLEMENT_VALUE

log = logging.getLogger("pob.worker")

CLAIM_SQL = """
SELECT p.id, p.corridor, p.usd_amount, p.beneficiary_id, p.attempts,
       c.currency, c.amount AS payout_amount
FROM payments p
JOIN postings c ON c.payment_id = p.id AND c.leg = 'fx_out' AND c.amount > 0
WHERE p.status = 'pending' AND (p.retry_after IS NULL OR p.retry_after <= now())
ORDER BY p.occurred_at, p.id
LIMIT 1
FOR UPDATE OF p SKIP LOCKED
"""


class Worker:
    def __init__(self, settings: Settings, pool=None, client: httpx.Client | None = None):
        self.s = settings
        self.pool = pool or make_pool(settings.database_url, settings.worker_threads + 1, "worker")
        self.client = client or httpx.Client(timeout=settings.rail_timeout_s)
        self.stop = threading.Event()

    # -- one unit of work -------------------------------------------------
    def process_one(self) -> bool:
        """Settle at most one payment. Returns False when there was nothing to do."""
        with self.pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(CLAIM_SQL).fetchone()
                if row is None:
                    return False
                self._settle(conn, row)
        return True

    def _settle(self, conn: psycopg.Connection, row: dict) -> None:
        pid, corridor = str(row["id"]), row["corridor"]
        fields = {"payment_id": pid, "corridor": corridor}
        body = {
            "payment_id": pid,
            "currency": row["currency"],
            "amount": str(row["payout_amount"]),
            "beneficiary_id": row["beneficiary_id"],
        }
        started = time.monotonic()
        try:
            resp = self.client.post(f"{self.s.rail_url}/transfer", json=body)
        except httpx.TimeoutException:
            SETTLEMENT_LATENCY.labels(corridor).observe(time.monotonic() - started)
            return self._retry_or_fail(conn, row, "rail_timeout")
        except httpx.TransportError as exc:
            log.warning("rail unreachable: %s", exc, extra=fields)
            return self._retry_or_fail(conn, row, "rail_unreachable")
        SETTLEMENT_LATENCY.labels(corridor).observe(time.monotonic() - started)

        if resp.status_code == 200:
            try:
                tx_hash = str(resp.json()["tx_hash"])
            except (ValueError, KeyError, TypeError):
                return self._retry_or_fail(conn, row, "rail_bad_response")
            conn.execute("UPDATE payments SET status = 'paid', tx_hash = %s, last_error = NULL WHERE id = %s", (tx_hash, row["id"]))
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO postings (payment_id, leg, account, currency, amount) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    [(row["id"], p.leg, p.account, p.currency, p.amount)
                     for p in money.payout_postings(row["beneficiary_id"], row["currency"], row["payout_amount"])],
                )
            SETTLEMENT_VALUE.labels(corridor, "paid").inc(float(row["usd_amount"]))  # metric only, never the ledger
            log.info("payout paid", extra={**fields, "status": "paid", "tx_hash": tx_hash})
        elif resp.status_code == 422:
            reason = _reason(resp)
            self._mark_failed(conn, row, f"rail_rejected:{reason}")
        else:
            self._retry_or_fail(conn, row, f"rail_http_{resp.status_code}")

    def _retry_or_fail(self, conn, row: dict, reason: str) -> None:
        attempts = row["attempts"] + 1
        if attempts >= self.s.max_attempts:
            self._mark_failed(conn, row, f"{reason} after {attempts} attempts", attempts)
            return
        delay = self.s.retry_base_s * (2 ** (attempts - 1))
        conn.execute(
            "UPDATE payments SET attempts = %s, last_error = %s, retry_after = now() + make_interval(secs => %s) WHERE id = %s",
            (attempts, reason, delay, row["id"]),
        )
        RAIL_RETRIES.labels(row["corridor"], reason).inc()
        # A timed-out attempt counts against the pod that made it, even if another
        # (stable) pod pays the payment later; otherwise a canary that times out
        # every payout would look healthy to the value-weighted analysis.
        SETTLEMENT_VALUE.labels(row["corridor"], "retry").inc(float(row["usd_amount"]))
        log.warning("payout retry scheduled", extra={"payment_id": str(row["id"]), "corridor": row["corridor"],
                                                     "status": "pending", "reason": reason, "attempts": attempts})

    def _mark_failed(self, conn, row: dict, reason: str, attempts: int | None = None) -> None:
        conn.execute(
            "UPDATE payments SET status = 'failed', last_error = %s, attempts = %s WHERE id = %s",
            (reason, attempts if attempts is not None else row["attempts"] + 1, row["id"]),
        )
        SETTLEMENT_VALUE.labels(row["corridor"], "failed").inc(float(row["usd_amount"]))
        log.warning("payout failed", extra={"payment_id": str(row["id"]), "corridor": row["corridor"], "status": "failed", "reason": reason})

    # -- loop ---------------------------------------------------------------
    def loop(self) -> None:
        backoff = self.s.worker_poll_s
        while not self.stop.is_set():
            try:
                did_work = self.process_one()
                backoff = self.s.worker_poll_s
                if not did_work:
                    self.stop.wait(self.s.worker_poll_s)
            except (*TRANSIENT_DB_ERRORS, psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
                log.warning("database not available, backing off %.1fs: %s", backoff, exc)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 10)
            except Exception:  # keep the thread alive; the row lock was released by the rollback
                log.exception("unexpected error while settling; will retry")
                self.stop.wait(1)

    def run(self) -> None:
        threads = [threading.Thread(target=self.loop, name=f"payout-{i}", daemon=True) for i in range(self.s.worker_threads)]
        for t in threads:
            t.start()
        while not self.stop.is_set():
            self.stop.wait(1)
        for t in threads:
            t.join(timeout=self.s.rail_timeout_s + 5)
        self.pool.close()
        self.client.close()


def _reason(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("reason", "unknown"))
    except ValueError:
        return "unknown"


def main() -> None:
    settings = load_settings()
    setup_logging("worker", settings.log_level)
    start_http_server(settings.metrics_port)
    worker = Worker(settings)

    def _graceful(signum, _frame):
        log.info("signal %s: finishing in-flight payouts then exiting", signum)
        worker.stop.set()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    deadline = time.monotonic() + 300
    while not schema_ready(worker.pool) and not worker.stop.is_set():
        if time.monotonic() > deadline:
            log.warning("schema still not ready after 300s; continuing to wait")
            deadline = time.monotonic() + 300
        worker.stop.wait(2)
    log.info("payout worker started", extra={"threads": settings.worker_threads, "rail": settings.rail_url,
                                              "rail_timeout_s": settings.rail_timeout_s})
    worker.run()


if __name__ == "__main__":
    main()
