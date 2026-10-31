#!/usr/bin/env python3
"""Export the ledger (plus what the rail saw) as a sorted, comparable CSV.

    python tools/export_ledger.py --dsn $DATABASE_URL --out run.csv [--rail-url http://localhost:8081]

Columns: idempotency_key, corridor, status, value_date, tx_hash, leg, account, currency, amount
Sorted by (idempotency_key, leg, account) in code-point order, so the result
does not depend on the database's collation.

Besides every posting, it adds one ``rail`` row per payment whose amount is
the number of transfers the rail received for it. The ledger's primary key
blocks a duplicate payout *posting*, but the rail may still have paid twice;
this row is what lets the diff catch it. Transfers the rail received for a
payment the ledger has never heard of are exported as ``rail-orphan:<id>``.

Exit codes: 0 ok, 2 database / rail / output error.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import httpx
import psycopg

COLUMNS = ["idempotency_key", "corridor", "status", "value_date", "tx_hash", "leg", "account", "currency", "amount"]

POSTINGS_SQL = """
SELECT p.idempotency_key, p.corridor, p.status, p.value_date::text AS value_date,
       coalesce(p.tx_hash, '') AS tx_hash, s.leg, s.account, s.currency, s.amount::text AS amount
FROM payments p
JOIN postings s ON s.payment_id = p.id
ORDER BY p.idempotency_key COLLATE "C", s.leg COLLATE "C", s.account COLLATE "C"
"""

PAYMENTS_SQL = """
SELECT p.id::text AS id, p.idempotency_key, p.corridor, p.status, p.value_date::text AS value_date,
       coalesce(p.tx_hash, '') AS tx_hash
FROM payments p
"""


class ExportError(RuntimeError):
    pass


def fetch_ledger(dsn: str) -> tuple[list[dict], list[dict]]:
    try:
        with psycopg.connect(dsn, connect_timeout=10, row_factory=psycopg.rows.dict_row) as conn:
            postings = conn.execute(POSTINGS_SQL).fetchall()
            payments = conn.execute(PAYMENTS_SQL).fetchall()
    except psycopg.Error as exc:
        raise ExportError(f"cannot read ledger from database: {exc}") from exc
    return postings, payments


def fetch_rail(rail_url: str) -> dict[str, int]:
    try:
        r = httpx.get(f"{rail_url.rstrip('/')}/transfers", timeout=30)
        r.raise_for_status()
        items = r.json()["transfers"]
        return {str(t["payment_id"]).lower(): int(t["count"]) for t in items}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ExportError(f"cannot read transfers from rail at {rail_url}: {exc}") from exc


def rail_rows(payments: list[dict], transfers: dict[str, int]) -> list[dict]:
    rows = []
    known = set()
    for p in payments:
        pid = p["id"].lower()
        known.add(pid)
        currency = p["corridor"].split("-")[-1]
        rows.append({
            "idempotency_key": p["idempotency_key"], "corridor": p["corridor"], "status": p["status"],
            "value_date": p["value_date"], "tx_hash": p["tx_hash"], "leg": "rail",
            "account": f"rail:transfers:{currency}", "currency": currency, "amount": str(transfers.get(pid, 0)),
        })
    for pid, count in transfers.items():
        if pid not in known:
            rows.append({
                "idempotency_key": f"rail-orphan:{pid}", "corridor": "UNKNOWN", "status": "unknown",
                "value_date": "", "tx_hash": "", "leg": "rail", "account": "rail:transfers:UNKNOWN",
                "currency": "", "amount": str(count),
            })
    return rows


def sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["idempotency_key"], r["leg"], r["account"]))


def write_csv(rows: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        for row in rows:
            w.writerow({k: (row[k].strip() if isinstance(row[k], str) else row[k]) for k in COLUMNS})
    tmp.replace(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="Postgres DSN (default $DATABASE_URL)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rail-url", default=os.environ.get("RAIL_URL", "http://localhost:8081"))
    ap.add_argument("--no-rail", action="store_true", help="skip the rail rows (not recommended)")
    args = ap.parse_args(argv)
    if not args.dsn:
        ap.error("--dsn or $DATABASE_URL is required")

    try:
        postings, payments = fetch_ledger(args.dsn)
        rows = list(postings)
        if not args.no_rail:
            rows += rail_rows(payments, fetch_rail(args.rail_url))
        rows = sort_rows(rows)
        write_csv(rows, args.out)
    except (ExportError, OSError) as exc:
        print(f"export_ledger: {exc}", file=sys.stderr)
        return 2
    print(f"export_ledger: wrote {len(rows)} rows for {len(payments)} payments to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
