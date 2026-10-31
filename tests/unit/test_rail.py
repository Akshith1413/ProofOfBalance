import hashlib
import uuid

import pytest

from fastapi.testclient import TestClient


def _body(pid, **over):
    return {"payment_id": str(pid), "currency": "INR", "amount": "8308.35", "beneficiary_id": 7, **over}


def test_tx_hash_is_sha256_of_payment_id(rail_module):
    pid = "4be42e40-96b1-5307-b86f-2321920a6023"
    assert rail_module.tx_hash(pid) == hashlib.sha256(pid.encode()).hexdigest()


def test_failure_is_deterministic_and_proportional(rail_module):
    ids = [str(uuid.uuid5(uuid.NAMESPACE_DNS, str(i))) for i in range(20000)]
    fails = [i for i in ids if rail_module.should_fail(i, 50)]
    assert fails == [i for i in ids if rail_module.should_fail(i, 50)]
    assert 0.04 < len(fails) / len(ids) < 0.06
    assert not any(rail_module.should_fail(i, 0) for i in ids[:1000])
    assert all(rail_module.should_fail(i, 1000) for i in ids[:1000])


def test_pays_and_counts_duplicates(rail_module):
    c = TestClient(rail_module.create_app(delay_ms=0, fail_permille=0))
    pid = uuid.uuid4()
    r1 = c.post("/transfer", json=_body(pid))
    assert r1.status_code == 200 and r1.json()["tx_hash"] == rail_module.tx_hash(str(pid))
    c.post("/transfer", json=_body(str(pid).upper()))  # same payment, different case
    transfers = c.get("/transfers").json()
    assert transfers["count"] == 1 and transfers["transfers"][0]["count"] == 2
    assert "pob_rail_duplicate_transfers_total" in c.get("/metrics").text


def test_failed_transfer_is_422_and_still_recorded(rail_module):
    c = TestClient(rail_module.create_app(delay_ms=0, fail_permille=1000))
    pid = uuid.uuid4()
    r = c.post("/transfer", json=_body(pid))
    assert r.status_code == 422 and r.json()["status"] == "failed"
    assert c.get("/transfers").json()["transfers"][0]["count"] == 1


def test_invalid_requests_are_400_and_not_recorded(rail_module):
    c = TestClient(rail_module.create_app(delay_ms=0, fail_permille=0))
    for body in (_body("nope"), _body(uuid.uuid4(), currency="inr"), _body(uuid.uuid4(), amount="-1"),
                 _body(uuid.uuid4(), amount="NaN"), _body(uuid.uuid4(), amount="x"),
                 {"payment_id": str(uuid.uuid4())}):
        assert c.post("/transfer", json=body).status_code == 400, body
    assert c.get("/transfers").json()["count"] == 0


def test_health_reports_knobs(rail_module):
    c = TestClient(rail_module.create_app(delay_ms=5, fail_permille=7))
    assert c.get("/healthz").json() == {"status": "ok", "delay_ms": 5, "fail_permille": 7, "delay_ms_by_currency": {}}


def test_currency_delay_overrides_default(rail_module):
    import time
    c = TestClient(rail_module.create_app(delay_ms=0, fail_permille=0, delay_ms_by_currency={"MXN": 300}))
    t0 = time.monotonic(); c.post("/transfer", json=_body(uuid.uuid4(), currency="INR")); fast = time.monotonic() - t0
    t0 = time.monotonic(); r = c.post("/transfer", json=_body(uuid.uuid4(), currency="MXN")); slow = time.monotonic() - t0
    assert r.status_code == 200 and slow >= 0.3 > fast
    assert c.get("/healthz").json()["delay_ms_by_currency"] == {"MXN": 300}


@pytest.mark.parametrize("raw,expected", [("", {}), ("MXN=300", {"MXN": 300}), (" MXN = 300 , BRL=50,", {"MXN": 300, "BRL": 50})])
def test_parse_currency_delays(rail_module, raw, expected):
    assert rail_module.parse_currency_delays(raw) == expected


@pytest.mark.parametrize("raw", ["MXN", "mxn=300", "MXN=-1", "MXN=abc", "MXN=600001", "=300"])
def test_parse_currency_delays_rejects_garbage(rail_module, raw):
    with pytest.raises(ValueError):
        rail_module.parse_currency_delays(raw)
