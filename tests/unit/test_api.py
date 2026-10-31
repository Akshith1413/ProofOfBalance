"""API behaviour that does not need a database (validation, error mapping)."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import ledger, main

GOOD = {
    "idempotency_key": "t-1", "corridor": "USD-INR", "usd_amount": "100.00",
    "client_id": 42, "beneficiary_id": 7, "occurred_at": "2026-11-02T10:00:00Z",
}


@pytest.fixture
def client(monkeypatch, root):
    monkeypatch.setenv("DATABASE_URL", "postgresql://pob:pob@127.0.0.1:1/pob?connect_timeout=1")
    monkeypatch.setenv("RATES_FILE", str(root / "service" / "rates.yaml"))
    app = main.create_app()
    with TestClient(app) as c:
        yield c


def _fake_payment(**over):
    row = {"id": ledger.payment_id("t-1"), "idempotency_key": "t-1", "corridor": "USD-INR",
           "usd_amount": Decimal("100.00"), "rate": Decimal("83.25"), "status": "pending", "tx_hash": None,
           "occurred_at": datetime(2026, 11, 2, 10, tzinfo=timezone.utc), "value_date": datetime(2026, 11, 2).date(),
           "client_id": 42, "beneficiary_id": 7}
    row.update(over)
    return row


@pytest.mark.parametrize("field,value", [
    ("usd_amount", 100.0),            # JSON number -> float -> refused
    ("usd_amount", "100.001"),
    ("usd_amount", "0.00"),
    ("usd_amount", "-5.00"),
    ("usd_amount", "1e3"),
    ("usd_amount", " 100.00"),
    ("usd_amount", "01.00"),
    ("client_id", "42"),              # strings are not ints
    ("client_id", 0),
    ("client_id", 1.0),
    ("beneficiary_id", -1),
    ("beneficiary_id", 2**60),
    ("idempotency_key", ""),
    ("idempotency_key", "x" * 129),
    ("idempotency_key", "has space"),
    ("occurred_at", "2026-11-02T10:00:00"),   # naive
    ("occurred_at", "yesterday"),
])
def test_rejects_bad_fields(client, field, value):
    r = client.post("/payments", json={**GOOD, field: value})
    assert r.status_code == 422, r.text


def test_rejects_missing_and_unknown_fields(client):
    body = dict(GOOD); body.pop("corridor")
    assert client.post("/payments", json=body).status_code == 422
    assert client.post("/payments", json={**GOOD, "seq": 1}).status_code == 422
    assert client.post("/payments", content=b"not json", headers={"content-type": "application/json"}).status_code == 422


def test_unknown_corridor(client):
    r = client.post("/payments", json={**GOOD, "corridor": "USD-EUR"})
    assert r.status_code == 422 and "unknown corridor" in r.json()["detail"]


def test_amount_over_limit(client):
    r = client.post("/payments", json={**GOOD, "usd_amount": "10000000.01"})
    assert r.status_code == 422 and "limit" in r.json()["detail"]


def test_created_and_replayed(client, monkeypatch):
    calls = []

    def fake_create(pool, p, rates, rounding, tz):
        calls.append(p)
        assert p.usd_amount == Decimal("100.00") and p.occurred_at.tzinfo is not None
        return _fake_payment(), len(calls) == 1

    monkeypatch.setattr(ledger, "create_payment", fake_create)
    r1 = client.post("/payments", json=GOOD)
    r2 = client.post("/payments", json=GOOD)
    assert r1.status_code == 201 and r1.json()["usd_amount"] == "100.00" and r1.json()["occurred_at"] == "2026-11-02T10:00:00Z"
    assert r2.status_code == 200 and r2.headers["idempotent-replayed"] == "true"


def test_offset_timestamp_normalised_to_utc(client, monkeypatch):
    seen = {}

    def fake_create(pool, p, *a):
        seen["ts"] = p.occurred_at
        return _fake_payment(), True

    monkeypatch.setattr(ledger, "create_payment", fake_create)
    client.post("/payments", json={**GOOD, "occurred_at": "2026-11-03T00:00:00+05:30"})
    assert seen["ts"] == datetime(2026, 11, 2, 18, 30, tzinfo=timezone.utc)


def test_conflict_is_409(client, monkeypatch):
    def fake_create(*a):
        raise ledger.IdempotencyConflict(_fake_payment(usd_amount=Decimal("5.00")))

    monkeypatch.setattr(ledger, "create_payment", fake_create)
    r = client.post("/payments", json=GOOD)
    assert r.status_code == 409 and r.json()["payment"]["usd_amount"] == "5.00"


def test_database_down_is_503(client, monkeypatch):
    def fake_create(*a):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(ledger, "create_payment", fake_create)
    r = client.post("/payments", json=GOOD)
    assert r.status_code == 503 and r.headers["retry-after"] == "2"


def test_get_payment_validation(client, monkeypatch):
    assert client.get("/payments/not-a-uuid").status_code == 422
    monkeypatch.setattr(ledger, "get_payment", lambda pool, pid: None)
    assert client.get(f"/payments/{uuid.uuid4()}").status_code == 404


def test_health_ready_metrics(client, monkeypatch):
    assert client.get("/healthz").json() == {"status": "ok"}
    monkeypatch.setattr(main, "schema_ready", lambda pool: False)
    assert client.get("/readyz").status_code == 503
    monkeypatch.setattr(main, "schema_ready", lambda pool: True)
    assert client.get("/readyz").status_code == 200
    body = client.get("/metrics").text
    assert "pob_payments_total" in body and "pob_idempotent_replays_total" in body


def test_bad_config_refuses_to_start(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@127.0.0.1:1/x")
    bad = tmp_path / "rates.yaml"
    bad.write_text("fee_bps: '20'\ncorridors:\n  USD-INR: 83.25\n")
    monkeypatch.setenv("RATES_FILE", str(bad))
    with pytest.raises(Exception, match="YAML float"):
        main.create_app()
