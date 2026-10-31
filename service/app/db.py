"""Connection pool helpers."""
from __future__ import annotations

import logging

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

log = logging.getLogger(__name__)

# Errors that mean "the database is unavailable right now", not "your request is bad".
TRANSIENT_DB_ERRORS = (psycopg.OperationalError, PoolTimeout)


def make_pool(dsn: str, max_size: int = 10, name: str = "pob") -> ConnectionPool:
    """Open lazily so the process starts (and reports not-ready) while Postgres is down."""
    pool = ConnectionPool(
        dsn,
        min_size=1,
        max_size=max_size,
        name=name,
        open=False,
        timeout=10,
        kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 5},
        check=ConnectionPool.check_connection,
        reconnect_timeout=0,  # keep retrying forever; readiness reports the outage
    )
    pool.open(wait=False)
    return pool


def schema_ready(pool: ConnectionPool) -> bool:
    try:
        with pool.connection(timeout=3) as conn:
            conn.execute("SELECT 1 FROM payments LIMIT 0")
            conn.execute("SELECT attempts FROM payments LIMIT 0")
        return True
    except (*TRANSIENT_DB_ERRORS, psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
        log.debug("schema not ready: %s", exc)
        return False
