"""Ledger writes: one payment plus its postings in a single transaction."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from psycopg_pool import ConnectionPool

from . import money
from .rates import Rates

# Fixed forever: changing it would give every payment a new id.
PAYMENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Akshith1413/ProofOfBalance/payments")


def payment_id(idempotency_key: str) -> uuid.UUID:
    return uuid.uuid5(PAYMENT_NAMESPACE, idempotency_key)


def value_date(occurred_at: datetime, tz: str) -> date:
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    return occurred_at.astimezone(ZoneInfo(tz)).date()


@dataclass(frozen=True)
class NewPayment:
    idempotency_key: str
    corridor: str
    usd_amount: Decimal
    client_id: int
    beneficiary_id: int
    occurred_at: datetime


class IdempotencyConflict(Exception):
    """Same idempotency key, different payment body."""

    def __init__(self, existing: dict):
        super().__init__("idempotency key reused with a different body")
        self.existing = existing


_INSERT_PAYMENT = """
INSERT INTO payments (id, idempotency_key, corridor, usd_amount, rate, status,
                      occurred_at, value_date, client_id, beneficiary_id)
VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s)
ON CONFLICT DO NOTHING
RETURNING id
"""

_INSERT_POSTING = """
INSERT INTO postings (payment_id, leg, account, currency, amount)
VALUES (%s, %s, %s, %s, %s)
"""

_SELECT_BY_KEY = """
SELECT id, idempotency_key, corridor, usd_amount, rate, status, tx_hash,
       occurred_at, value_date, client_id, beneficiary_id
FROM payments WHERE idempotency_key = %s
"""


def _same_body(existing: dict, p: NewPayment) -> bool:
    return (
        existing["corridor"] == p.corridor
        and existing["usd_amount"] == p.usd_amount
        and existing["client_id"] == p.client_id
        and existing["beneficiary_id"] == p.beneficiary_id
        and existing["occurred_at"] == p.occurred_at
    )


def create_payment(pool: ConnectionPool, p: NewPayment, rates: Rates, rounding: str, tz: str) -> tuple[dict, bool]:
    """Return (payment, created). A repeated key returns the original and writes nothing."""
    rate = rates.rate(p.corridor)
    q = money.quote(p.corridor, p.usd_amount, rate, rates.fee_bps, rounding)
    pid = payment_id(p.idempotency_key)
    vdate = value_date(p.occurred_at, tz)

    with pool.connection() as conn:
        with conn.transaction():
            row = conn.execute(
                _INSERT_PAYMENT,
                (pid, p.idempotency_key, p.corridor, p.usd_amount, rate, p.occurred_at, vdate, p.client_id, p.beneficiary_id),
            ).fetchone()
            if row is not None:
                with conn.cursor() as cur:
                    cur.executemany(
                        _INSERT_POSTING,
                        [(pid, x.leg, x.account, x.currency, x.amount) for x in money.initial_postings(p.client_id, q)],
                    )
                return get_payment_by_key(conn, p.idempotency_key), True

        # Conflict: the other transaction has committed by the time ON CONFLICT
        # fires, so a fresh statement sees its row.
        existing = get_payment_by_key(conn, p.idempotency_key)
        if existing is None:  # pragma: no cover - would need a concurrent delete
            raise RuntimeError(f"idempotency key {p.idempotency_key!r} conflicted but no row found")
        if not _same_body(existing, p):
            raise IdempotencyConflict(existing)
        return existing, False


def get_payment_by_key(conn, key: str) -> dict | None:
    return conn.execute(_SELECT_BY_KEY, (key,)).fetchone()


def get_payment(pool: ConnectionPool, pid: uuid.UUID) -> dict | None:
    with pool.connection() as conn:
        row = conn.execute(
            """SELECT id, idempotency_key, corridor, usd_amount, rate, status, tx_hash,
                      occurred_at, value_date, client_id, beneficiary_id
               FROM payments WHERE id = %s""",
            (pid,),
        ).fetchone()
        if row is None:
            return None
        row["postings"] = conn.execute(
            "SELECT leg, account, currency, amount FROM postings WHERE payment_id = %s ORDER BY leg, account",
            (pid,),
        ).fetchall()
        return row


def status_counts(pool: ConnectionPool) -> dict[str, int]:
    with pool.connection() as conn:
        rows = conn.execute("SELECT status, count(*) AS n FROM payments GROUP BY status").fetchall()
    counts = {"pending": 0, "paid": 0, "failed": 0}
    counts.update({r["status"]: r["n"] for r in rows})
    counts["total"] = sum(counts[s] for s in ("pending", "paid", "failed"))
    return counts
