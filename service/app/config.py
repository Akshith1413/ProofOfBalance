"""Environment-driven settings. Invalid config fails fast at startup."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .money import MoneyError, parse_rounding_mode, to_decimal


class ConfigError(ValueError):
    pass


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ConfigError(f"{name}={value} out of range [{lo}, {hi}]")
    return value


def _float(name: str, default: float, lo: float = 0.0) -> float:
    # Only used for timeouts and intervals, never for money.
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value < lo:
        raise ConfigError(f"{name} must be >= {lo}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    rates_file: str
    rounding: str
    settlement_tz: str
    max_usd_amount: object  # Decimal
    rail_url: str
    rail_timeout_s: float
    worker_threads: int
    worker_poll_s: float
    max_attempts: int
    retry_base_s: float
    metrics_port: int
    reconcile_interval_s: float
    db_pool_max: int
    log_level: str


def load_settings() -> Settings:
    db = os.environ.get("DATABASE_URL", "").strip()
    if not db:
        raise ConfigError("DATABASE_URL is required, e.g. postgresql://pob:pob@localhost:5432/pob")

    try:
        rounding = parse_rounding_mode(os.environ.get("ROUNDING_MODE"))
    except MoneyError as exc:
        raise ConfigError(str(exc)) from exc

    tz = os.environ.get("SETTLEMENT_TZ", "UTC").strip() or "UTC"
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"SETTLEMENT_TZ {tz!r} is not a known IANA time zone") from None

    try:
        max_amount = to_decimal(os.environ.get("MAX_USD_AMOUNT", "10000000.00"))
    except (TypeError, MoneyError) as exc:
        raise ConfigError(f"MAX_USD_AMOUNT: {exc}") from exc
    if max_amount <= 0:
        raise ConfigError("MAX_USD_AMOUNT must be > 0")

    return Settings(
        database_url=db,
        rates_file=os.environ.get("RATES_FILE", "/etc/pob/rates.yaml"),
        rounding=rounding,
        settlement_tz=tz,
        max_usd_amount=max_amount,
        rail_url=os.environ.get("RAIL_URL", "http://localhost:8081").rstrip("/"),
        rail_timeout_s=_float("RAIL_TIMEOUT_MS", 5000, lo=1) / 1000,
        worker_threads=_int("WORKER_THREADS", 4, 1, 64),
        worker_poll_s=_float("WORKER_POLL_MS", 200, lo=10) / 1000,
        max_attempts=_int("MAX_ATTEMPTS", 5, 1, 100),
        retry_base_s=_float("RETRY_BASE_MS", 500, lo=0) / 1000,
        metrics_port=_int("METRICS_PORT", 9100, 1, 65535),
        reconcile_interval_s=_float("RECONCILE_INTERVAL_S", 30, lo=1),
        db_pool_max=_int("DB_POOL_MAX", 10, 1, 100),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
