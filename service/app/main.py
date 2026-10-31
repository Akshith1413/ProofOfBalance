"""Settlement API.

POST /payments       create a payment and its five postings (idempotent)
GET  /payments/{id}  payment + postings
GET  /stats          counts by status (the replay tool polls this)
GET  /healthz        process is alive
GET  /readyz         database reachable and schema migrated
GET  /metrics        Prometheus
"""
from __future__ import annotations

import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from . import ledger, money
from .config import load_settings
from .db import TRANSIENT_DB_ERRORS, make_pool, schema_ready
from .logs import setup_logging
from .metrics import IDEMPOTENT_REPLAYS, KNOWN_CORRIDORS, PAYMENTS
from .rates import RatesError, load_rates

log = logging.getLogger("pob.service")

_AMOUNT_RE = re.compile(r"^(0|[1-9][0-9]{0,15})(\.[0-9]{1,2})?$")
_KEY_RE = r"^[A-Za-z0-9._:\-]{1,128}$"
_MAX_ID = 2**53  # ids must survive a JSON round trip through JavaScript too


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: StrictStr = Field(pattern=_KEY_RE)
    corridor: StrictStr
    usd_amount: StrictStr  # a JSON number would arrive as a binary float
    client_id: StrictInt = Field(gt=0, le=_MAX_ID)
    beneficiary_id: StrictInt = Field(gt=0, le=_MAX_ID)
    occurred_at: datetime

    @field_validator("usd_amount")
    @classmethod
    def _amount(cls, v: str) -> str:
        if not _AMOUNT_RE.match(v):
            raise ValueError("must be a decimal string with at most 2 decimals, e.g. \"250.00\"")
        if Decimal(v) <= 0:
            raise ValueError("must be greater than 0")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("must include a timezone, e.g. 2026-11-02T00:00:41Z")
        return v.astimezone(timezone.utc)


def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, (Decimal, uuid.UUID)):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, list):
            out[k] = [_jsonable(x) for x in v]
        else:
            out[k] = v
    return out


def _corridor_label(corridor: str) -> str:
    # Bound label cardinality: arbitrary client input never becomes a metric label.
    return corridor if corridor in KNOWN_CORRIDORS else "other"


def _unavailable(exc: Exception) -> JSONResponse:
    log.warning("database unavailable: %s", exc)
    return JSONResponse({"detail": "ledger database unavailable, retry later"}, status_code=503, headers={"Retry-After": "2"})


def create_app() -> FastAPI:
    settings = load_settings()
    setup_logging("service", settings.log_level)
    rates = load_rates(settings.rates_file)
    log.info(
        "starting",
        extra={"rounding": settings.rounding, "settlement_tz": settings.settlement_tz, "corridors": sorted(rates.corridors)},
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = make_pool(settings.database_url, settings.db_pool_max, "service")
        try:
            yield
        finally:
            app.state.pool.close()

    app = FastAPI(title="ProofOfBalance settlement service", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        PAYMENTS.labels("other", "rejected").inc()
        errors = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.post("/payments")
    def post_payment(body: PaymentIn):
        label = _corridor_label(body.corridor)
        if body.corridor not in rates.corridors:
            PAYMENTS.labels(label, "rejected").inc()
            return JSONResponse({"detail": f"unknown corridor {body.corridor!r}", "known": sorted(rates.corridors)}, status_code=422)
        amount = Decimal(body.usd_amount)
        if amount > settings.max_usd_amount:
            PAYMENTS.labels(label, "rejected").inc()
            return JSONResponse({"detail": f"usd_amount exceeds limit {settings.max_usd_amount}"}, status_code=422)

        new = ledger.NewPayment(body.idempotency_key, body.corridor, amount, body.client_id, body.beneficiary_id, body.occurred_at)
        try:
            payment, created = ledger.create_payment(app.state.pool, new, rates, settings.rounding, settings.settlement_tz)
        except money.MoneyError as exc:
            PAYMENTS.labels(label, "rejected").inc()
            return JSONResponse({"detail": str(exc)}, status_code=422)
        except ledger.IdempotencyConflict as exc:
            PAYMENTS.labels(label, "conflict").inc()
            log.warning("idempotency conflict", extra={"payment_id": str(exc.existing["id"]), "corridor": body.corridor, "status": "conflict"})
            return JSONResponse(
                {"detail": "idempotency_key already used for a different payment", "payment": _jsonable(exc.existing)},
                status_code=409,
            )
        except TRANSIENT_DB_ERRORS as exc:
            return _unavailable(exc)

        fields = {"payment_id": str(payment["id"]), "corridor": body.corridor, "status": payment["status"]}
        if created:
            PAYMENTS.labels(label, "pending").inc()
            log.info("payment accepted", extra=fields)
            return JSONResponse(_jsonable(payment), status_code=201)
        IDEMPOTENT_REPLAYS.labels(label).inc()
        log.info("idempotent replay", extra=fields)
        return JSONResponse(_jsonable(payment), status_code=200, headers={"Idempotent-Replayed": "true"})

    @app.get("/payments/{pid}")
    def get_payment(pid: str):
        try:
            parsed = uuid.UUID(pid)
        except ValueError:
            return JSONResponse({"detail": "payment id must be a UUID"}, status_code=422)
        try:
            row = ledger.get_payment(app.state.pool, parsed)
        except TRANSIENT_DB_ERRORS as exc:
            return _unavailable(exc)
        if row is None:
            return JSONResponse({"detail": "payment not found"}, status_code=404)
        return _jsonable(row)

    @app.get("/stats")
    def stats():
        try:
            return ledger.status_counts(app.state.pool)
        except TRANSIENT_DB_ERRORS as exc:
            return _unavailable(exc)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        if schema_ready(app.state.pool):
            return {"status": "ready"}
        return JSONResponse({"status": "not ready", "detail": "database unreachable or not migrated"}, status_code=503)

    @app.get("/metrics")
    def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def main() -> None:
    import os

    import uvicorn

    try:
        app = create_app()
    except (RatesError, ValueError) as exc:
        setup_logging("service")
        log.critical("invalid configuration: %s", exc)
        raise SystemExit(2) from exc
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_config=None, access_log=False)


if __name__ == "__main__":
    main()
