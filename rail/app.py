"""Mock stablecoin rail.

POST /transfer   pay out one payment. tx_hash = sha256(payment_id).
GET  /transfers  every payment_id received and how many times.
GET  /healthz, GET /metrics

Deterministic by design:
  * failure is decided by sha256(payment_id) % 1000 < FAIL_PERMILLE, so the
    same payments fail on every run;
  * DELAY_MS adds latency (after the transfer is recorded: the money has left
    even if the caller times out, which is how real duplicate payouts happen);
  * DELAY_MS_BY_CURRENCY (e.g. "MXN=300,BRL=50") overrides it per currency:
    some real rails are slower than others, and CI never sees that.

The rail is deliberately NOT idempotent: it remembers every payment_id and
counts repeats. Duplicates are found here, by the "outside world".
State is in memory; run exactly one replica.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import threading
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

log = logging.getLogger("pob.rail")
logging.basicConfig(level=logging.INFO, format='{"component":"rail","level":"%(levelname)s","msg":"%(message)s"}')

TRANSFERS = Counter("pob_rail_transfers_total", "Transfer requests received", ["currency"])
DUPLICATES = Counter("pob_rail_duplicate_transfers_total", "Transfers for a payment_id that was already paid", ["currency"])

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise SystemExit(f"{name}={value} must be within [{lo}, {hi}]")
    return value


def parse_currency_delays(raw: str) -> dict[str, int]:
    """'MXN=300, BRL=50' -> {'MXN': 300, 'BRL': 50}. Empty -> {}."""
    delays: dict[str, int] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        currency, sep, ms = item.partition("=")
        currency = currency.strip()
        if not sep or not _CURRENCY_RE.match(currency) or not ms.strip().isdigit() or int(ms) > 600_000:
            raise ValueError(f"bad DELAY_MS_BY_CURRENCY entry {item!r}: expected CUR=milliseconds, e.g. MXN=300")
        delays[currency] = int(ms)
    return delays


DELAY_MS = _env_int("DELAY_MS", 0, 0, 600_000)
FAIL_PERMILLE = _env_int("FAIL_PERMILLE", 0, 0, 1000)
try:
    DELAY_MS_BY_CURRENCY = parse_currency_delays(os.environ.get("DELAY_MS_BY_CURRENCY", ""))
except ValueError as exc:
    raise SystemExit(str(exc)) from None


def tx_hash(payment_id: str) -> str:
    return hashlib.sha256(payment_id.encode()).hexdigest()


def should_fail(payment_id: str, fail_permille: int) -> bool:
    return int(tx_hash(payment_id), 16) % 1000 < fail_permille


class TransferIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    payment_id: StrictStr
    currency: StrictStr
    amount: StrictStr
    beneficiary_id: StrictInt


class Ledger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, dict] = {}

    def record(self, payment_id: str, currency: str, amount: str) -> int:
        with self._lock:
            entry = self._seen.setdefault(payment_id, {"payment_id": payment_id, "currency": currency, "amount": amount, "count": 0})
            entry["count"] += 1
            return entry["count"]

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(v) for _, v in sorted(self._seen.items())]


def create_app(delay_ms: int = DELAY_MS, fail_permille: int = FAIL_PERMILLE,
               delay_ms_by_currency: dict[str, int] | None = None) -> FastAPI:
    app = FastAPI(title="Mock stablecoin rail")
    currency_delays = dict(DELAY_MS_BY_CURRENCY if delay_ms_by_currency is None else delay_ms_by_currency)
    ledger = Ledger()
    app.state.ledger = ledger

    @app.exception_handler(RequestValidationError)
    async def _bad(_req, exc: RequestValidationError):
        return JSONResponse({"status": "invalid", "detail": [e.get("msg") for e in exc.errors()]}, status_code=400)

    @app.post("/transfer")
    async def transfer(body: TransferIn):
        try:
            pid = str(uuid.UUID(body.payment_id))  # canonical lowercase form
        except ValueError:
            return JSONResponse({"status": "invalid", "detail": "payment_id must be a UUID"}, status_code=400)
        if not _CURRENCY_RE.match(body.currency):
            return JSONResponse({"status": "invalid", "detail": "currency must be 3 upper-case letters"}, status_code=400)
        try:
            amount = Decimal(body.amount)
        except InvalidOperation:
            return JSONResponse({"status": "invalid", "detail": "amount must be a decimal string"}, status_code=400)
        if not amount.is_finite() or amount <= 0:
            return JSONResponse({"status": "invalid", "detail": "amount must be > 0"}, status_code=400)

        count = ledger.record(pid, body.currency, body.amount)
        TRANSFERS.labels(body.currency).inc()
        if count > 1:
            DUPLICATES.labels(body.currency).inc()
            log.warning("duplicate transfer payment_id=%s count=%d", pid, count)

        delay = currency_delays.get(body.currency, delay_ms)
        if delay:
            await asyncio.sleep(delay / 1000)

        if should_fail(pid, fail_permille):
            return JSONResponse({"status": "failed", "reason": "beneficiary_rejected", "payment_id": pid}, status_code=422)
        return {"status": "paid", "payment_id": pid, "tx_hash": tx_hash(pid)}

    @app.get("/transfers")
    async def transfers():
        items = ledger.snapshot()
        return {"count": len(items), "transfers": items}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "delay_ms": delay_ms, "fail_permille": fail_permille,
                "delay_ms_by_currency": currency_delays}

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8081")), access_log=False)
