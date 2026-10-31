import uuid
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest
import yaml

from app import config, ledger
from app.rates import RatesError, load_rates, parse_rates


# ---- rates -----------------------------------------------------------------

def test_committed_rates_file_loads(root):
    r = load_rates(root / "service" / "rates.yaml")
    assert r.fee_bps == 20
    assert r.rate("USD-INR") == Decimal("83.25")
    assert set(r.corridors) == {"USD-INR", "USD-MXN", "USD-BRL"}


def test_unquoted_yaml_float_is_rejected():
    with pytest.raises(RatesError, match="YAML float"):
        parse_rates(yaml.safe_load("fee_bps: '20'\ncorridors:\n  USD-INR: 83.25\n"))


@pytest.mark.parametrize("doc,msg", [
    ("[]", "mapping"),
    ("fee_bps: '20'\n", "corridors"),
    ("fee_bps: '20'\ncorridors: {}\n", "corridors"),
    ("fee_bps: '20.5'\ncorridors: {USD-INR: '1'}\n", "whole number"),
    ("fee_bps: '20'\ncorridors: {USD-INR: '-1'}\n", "> 0"),
    ("fee_bps: '20'\ncorridors: {INR: '1'}\n", "USD-INR"),
    ("fee_bps: '20'\ncorridors: {USD-INR: '1.123456789'}\n", "8 decimal"),
    ("fee_bps: '20'\ncorridors: {USD-INR: '1'}\nextra: 1\n", "unknown keys"),
])
def test_invalid_rates(doc, msg):
    with pytest.raises(RatesError, match=msg):
        parse_rates(yaml.safe_load(doc))


def test_unknown_corridor_lookup(root):
    with pytest.raises(RatesError, match="unknown corridor"):
        load_rates(root / "service" / "rates.yaml").rate("USD-EUR")


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(RatesError, match="cannot read"):
        load_rates(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("corridors: [unclosed")
    with pytest.raises(RatesError, match="not valid YAML"):
        load_rates(bad)


# ---- config ----------------------------------------------------------------

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@y/z")
    for k in ("ROUNDING_MODE", "SETTLEMENT_TZ", "RAIL_TIMEOUT_MS", "WORKER_THREADS", "MAX_USD_AMOUNT"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_defaults(env):
    s = config.load_settings()
    assert s.settlement_tz == "UTC" and s.rounding == "ROUND_HALF_EVEN" and s.rail_timeout_s == 5.0


def test_rounding_mode_from_env(env):
    env.setenv("ROUNDING_MODE", "ROUND_HALF_UP")
    assert config.load_settings().rounding == ROUND_HALF_UP


@pytest.mark.parametrize("key,value", [
    ("ROUNDING_MODE", "NEAREST"), ("SETTLEMENT_TZ", "Mars/Olympus"), ("RAIL_TIMEOUT_MS", "abc"),
    ("WORKER_THREADS", "0"), ("MAX_USD_AMOUNT", "-5"), ("MAX_USD_AMOUNT", "lots"),
])
def test_invalid_env_fails_fast(env, key, value):
    env.setenv(key, value)
    with pytest.raises(config.ConfigError):
        config.load_settings()


def test_database_url_required(env):
    env.delenv("DATABASE_URL")
    with pytest.raises(config.ConfigError, match="DATABASE_URL"):
        config.load_settings()


# ---- ids and dates ------------------------------------------------------------

def test_payment_id_is_uuid5_and_stable():
    a, b = ledger.payment_id("d1-000001"), ledger.payment_id("d1-000001")
    assert a == b and a.version == 5 and a != ledger.payment_id("d1-000002")
    # Pinned: if this ever changes, every payment id in every ledger changes.
    assert str(ledger.PAYMENT_NAMESPACE) == str(uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Akshith1413/ProofOfBalance/payments"))


@pytest.mark.parametrize("ts,tz,expected", [
    ("2026-11-02T18:29:59+00:00", "UTC", date(2026, 11, 2)),
    ("2026-11-02T18:29:59+00:00", "Asia/Kolkata", date(2026, 11, 2)),   # 23:59:59 IST
    ("2026-11-02T18:30:00+00:00", "Asia/Kolkata", date(2026, 11, 3)),   # 00:00 IST next day
    ("2026-11-02T03:00:00+00:00", "America/Mexico_City", date(2026, 11, 1)),
])
def test_value_date_follows_settlement_tz(ts, tz, expected):
    assert ledger.value_date(datetime.fromisoformat(ts), tz) == expected


def test_value_date_rejects_naive():
    with pytest.raises(ValueError):
        ledger.value_date(datetime(2026, 11, 2), "UTC")


def test_same_body_comparison():
    p = ledger.NewPayment("k", "USD-INR", Decimal("1.00"), 1, 2, datetime(2026, 11, 2, tzinfo=timezone.utc))
    existing = {"corridor": "USD-INR", "usd_amount": Decimal("1.0"), "client_id": 1, "beneficiary_id": 2,
                "occurred_at": datetime(2026, 11, 2, tzinfo=timezone.utc)}
    assert ledger._same_body(existing, p)
    assert not ledger._same_body({**existing, "beneficiary_id": 3}, p)
