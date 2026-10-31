"""Payout worker decisions, with a fake connection and a mocked rail."""
import uuid
from decimal import Decimal

import httpx
import pytest

from app import config, worker


class FakeConn:
    def __init__(self):
        self.executed = []
        self.many = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        return self

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def executemany(self, sql, rows):
                conn.many.extend(rows)
        return Cur()


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@y/z")
    monkeypatch.setenv("MAX_ATTEMPTS", "3")
    return config.load_settings()


ROW = {"id": uuid.uuid4(), "corridor": "USD-INR", "usd_amount": Decimal("100.00"), "beneficiary_id": 7,
       "attempts": 0, "currency": "INR", "payout_amount": Decimal("8308.35")}


def make(settings, handler):
    return worker.Worker(settings, pool=object(), client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_paid_writes_two_payout_legs(settings):
    sent = {}

    def handler(req):
        sent.update(__import__("json").loads(req.content))
        return httpx.Response(200, json={"tx_hash": "abc"})

    conn = FakeConn()
    make(settings, handler)._settle(conn, dict(ROW))
    assert sent["amount"] == "8308.35" and sent["currency"] == "INR"
    assert "SET status = 'paid'" in conn.executed[0][0]
    assert [(r[1], r[2], r[4]) for r in conn.many] == [
        ("payout", "payout_clearing:INR", Decimal("-8308.35")), ("payout", "beneficiary:7:INR", Decimal("8308.35"))]


def test_rail_rejection_marks_failed_without_payout(settings):
    conn = FakeConn()
    make(settings, lambda r: httpx.Response(422, json={"reason": "beneficiary_rejected"}))._settle(conn, dict(ROW))
    assert "SET status = 'failed'" in conn.executed[0][0] and "beneficiary_rejected" in conn.executed[0][1][0]
    assert conn.many == []


@pytest.mark.parametrize("response", [
    httpx.Response(503), httpx.Response(200, json={"no": "hash"}), httpx.Response(200, content=b"not json")])
def test_transient_or_bad_response_schedules_retry(settings, response):
    conn = FakeConn()
    make(settings, lambda r: response)._settle(conn, dict(ROW))
    sql, params = conn.executed[0]
    assert "retry_after" in sql and params[0] == 1 and conn.many == []


def test_timeout_retries_then_fails_after_max_attempts(settings):
    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    w = make(settings, timeout)
    conn = FakeConn()
    w._settle(conn, dict(ROW, attempts=1))
    assert "retry_after" in conn.executed[0][0] and conn.executed[0][1][1] == "rail_timeout"
    conn = FakeConn()
    w._settle(conn, dict(ROW, attempts=2))  # 3rd attempt == MAX_ATTEMPTS
    assert "SET status = 'failed'" in conn.executed[0][0] and "after 3 attempts" in conn.executed[0][1][0]


def test_unreachable_rail_is_retried(settings):
    def refused(req):
        raise httpx.ConnectError("refused", request=req)

    conn = FakeConn()
    make(settings, refused)._settle(conn, dict(ROW))
    assert conn.executed[0][1][1] == "rail_unreachable"


def test_backoff_doubles(settings):
    conn = FakeConn()
    w = make(settings, lambda r: httpx.Response(500))
    w._settle(conn, dict(ROW, attempts=0))
    w._settle(conn, dict(ROW, attempts=1))
    assert conn.executed[0][1][2] == pytest.approx(settings.retry_base_s)
    assert conn.executed[1][1][2] == pytest.approx(settings.retry_base_s * 2)


def test_claim_sql_picks_pending_rows():
    assert "status = 'pending'" in worker.CLAIM_SQL


def _value(corridor, status):
    from prometheus_client import REGISTRY
    return REGISTRY.get_sample_value("pob_settlement_value_usd_total", {"corridor": corridor, "status": status}) or 0.0


def test_retry_counts_attempt_value_against_this_pod(settings):
    before = _value("USD-INR", "retry"), _value("USD-INR", "failed")
    make(settings, lambda r: httpx.Response(503))._settle(FakeConn(), dict(ROW, attempts=0))
    assert _value("USD-INR", "retry") - before[0] == pytest.approx(100.0)
    assert _value("USD-INR", "failed") == before[1]  # not final yet
