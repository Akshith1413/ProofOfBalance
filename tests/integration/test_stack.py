"""End-to-end checks against the running docker compose stack.

    docker compose up -d --build --wait
    pytest -m integration tests/integration
"""
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
import pytest

pytestmark = pytest.mark.integration

SERVICE = os.environ.get("POB_SERVICE_URL", "http://localhost:8080")
RAIL = os.environ.get("POB_RAIL_URL", "http://localhost:8081")
DSN = os.environ.get("DATABASE_URL", "postgresql://pob:pob@localhost:5432/pob")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def stack_up():
    try:
        if httpx.get(f"{SERVICE}/readyz", timeout=3).status_code != 200:
            pytest.skip("service not ready")
    except httpx.HTTPError:
        pytest.skip(f"stack not running at {SERVICE}; run: docker compose up -d --build --wait")


def body(key, **over):
    return {"idempotency_key": key, "corridor": "USD-INR", "usd_amount": "100.00", "client_id": 42,
            "beneficiary_id": 7, "occurred_at": "2026-11-02T10:00:00Z", **over}


def postings(key):
    with psycopg.connect(DSN) as conn:
        return conn.execute(
            "SELECT s.leg, s.account, s.amount FROM postings s JOIN payments p ON p.id = s.payment_id "
            "WHERE p.idempotency_key = %s ORDER BY s.leg, s.account", (key,)).fetchall()


def wait_status(pid, want=("paid", "failed"), timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{SERVICE}/payments/{pid}")
        if r.status_code == 200 and r.json()["status"] in want:
            return r.json()
        time.sleep(0.3)
    raise AssertionError(f"payment {pid} not settled in {timeout}s")


def test_same_payment_twice_is_one_payment_five_then_seven_postings():
    key = f"it-{uuid.uuid4()}"
    r1 = httpx.post(f"{SERVICE}/payments", json=body(key))
    r2 = httpx.post(f"{SERVICE}/payments", json=body(key))
    assert r1.status_code == 201 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["id"] == str(uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_URL,
                                  "https://github.com/Akshith1413/ProofOfBalance/payments"), key))
    paid = wait_status(r1.json()["id"])
    assert paid["status"] == "paid"
    legs = postings(key)
    assert len(legs) == 7
    assert ("payout", "beneficiary:7:INR", Decimal("8308.35")) in legs
    assert ("fx_out", "payout_clearing:INR", Decimal("8308.35")) in legs
    assert sum(a for _, acct, a in legs if acct.endswith("USD")) == 0
    assert sum(a for _, acct, a in legs if acct.endswith("INR")) == 0


def test_racing_duplicates_create_exactly_one_payment():
    key = f"race-{uuid.uuid4()}"
    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(lambda _: httpx.post(f"{SERVICE}/payments", json=body(key), timeout=30).status_code,
                              range(16)))
    assert codes.count(201) == 1 and codes.count(200) == 15
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT count(*) FROM payments WHERE idempotency_key = %s", (key,)).fetchone()[0] == 1
    pid = httpx.post(f"{SERVICE}/payments", json=body(key)).json()["id"]
    wait_status(pid)
    count = {t["payment_id"]: t["count"] for t in httpx.get(f"{RAIL}/transfers").json()["transfers"]}[pid]
    assert count == 1, "the rail was paid more than once"


def test_reused_key_with_different_body_is_409():
    key = f"conf-{uuid.uuid4()}"
    assert httpx.post(f"{SERVICE}/payments", json=body(key)).status_code == 201
    r = httpx.post(f"{SERVICE}/payments", json=body(key, usd_amount="100.01"))
    assert r.status_code == 409 and r.json()["payment"]["usd_amount"] == "100.00"
    assert len(postings(key)) in (5, 7)


@pytest.mark.parametrize("corridor,amount,dest", [("USD-MXN", "1.00", "18.24"), ("USD-BRL", "0.01", "0.05")])
def test_other_corridors(corridor, amount, dest):
    key = f"c-{uuid.uuid4()}"
    r = httpx.post(f"{SERVICE}/payments", json=body(key, corridor=corridor, usd_amount=amount))
    assert r.status_code == 201
    cur = corridor[-3:]
    assert (("fx_out", f"payout_clearing:{cur}", Decimal(dest))) in postings(key)


def test_value_date_uses_settlement_tz_default_utc():
    key = f"tz-{uuid.uuid4()}"
    r = httpx.post(f"{SERVICE}/payments", json=body(key, occurred_at="2026-11-02T23:59:59+00:00"))
    assert r.json()["value_date"] == "2026-11-02"


def test_get_payment_and_stats():
    key = f"get-{uuid.uuid4()}"
    pid = httpx.post(f"{SERVICE}/payments", json=body(key)).json()["id"]
    p = httpx.get(f"{SERVICE}/payments/{pid}").json()
    assert p["idempotency_key"] == key and len(p["postings"]) >= 5
    assert httpx.get(f"{SERVICE}/payments/{uuid.uuid4()}").status_code == 404
    stats = httpx.get(f"{SERVICE}/stats").json()
    assert stats["total"] == stats["pending"] + stats["paid"] + stats["failed"]


def test_metrics_exposed():
    svc = httpx.get(f"{SERVICE}/metrics").text
    assert 'pob_payments_total{corridor="USD-INR",status="pending"}' in svc
    assert "pob_idempotent_replays_total" in svc
    rail = httpx.get(f"{RAIL}/metrics").text
    assert "pob_rail_transfers_total" in rail and "pob_rail_duplicate_transfers_total" in rail


def _reconcile_once():
    env = {**os.environ, "DATABASE_URL": DSN}
    return subprocess.run([sys.executable, "-m", "app.reconciler", "--once"], cwd=ROOT / "service",
                          env=env, capture_output=True, text=True, timeout=60)


def test_reconciler_detects_an_unbalanced_payment():
    assert _reconcile_once().returncode == 0
    key = f"broken-{uuid.uuid4()}"
    pid = httpx.post(f"{SERVICE}/payments", json=body(key)).json()["id"]
    wait_status(pid)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("UPDATE postings SET amount = amount + 0.01 WHERE payment_id = %s AND account = 'pool:USD'", (pid,))
        try:
            res = _reconcile_once()
            assert res.returncode == 1, res.stdout + res.stderr
            assert "USD" in res.stdout
        finally:
            conn.execute("UPDATE postings SET amount = amount - 0.01 WHERE payment_id = %s AND account = 'pool:USD'", (pid,))
    assert _reconcile_once().returncode == 0
