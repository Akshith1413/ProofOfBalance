import csv
import json
import threading

import httpx
import pytest

import export_ledger as ex
import generator
import replay


# ---- export ---------------------------------------------------------------

def test_rail_rows_one_per_payment_including_zero_and_orphans():
    payments = [
        {"id": "AAAAAAAA-0000-0000-0000-000000000001", "idempotency_key": "k1", "corridor": "USD-INR",
         "status": "paid", "value_date": "2026-11-02", "tx_hash": "h"},
        {"id": "aaaaaaaa-0000-0000-0000-000000000002", "idempotency_key": "k2", "corridor": "USD-BRL",
         "status": "pending", "value_date": "2026-11-02", "tx_hash": ""},
    ]
    transfers = {"aaaaaaaa-0000-0000-0000-000000000001": 2, "ffffffff-0000-0000-0000-000000000009": 1}
    rows = {r["idempotency_key"]: r for r in ex.rail_rows(payments, transfers)}
    assert rows["k1"]["amount"] == "2" and rows["k1"]["account"] == "rail:transfers:INR"
    assert rows["k2"]["amount"] == "0"
    assert rows["rail-orphan:ffffffff-0000-0000-0000-000000000009"]["corridor"] == "UNKNOWN"


def test_sort_is_codepoint_order_not_collation():
    rows = [{"idempotency_key": k, "leg": l, "account": a} for k, l, a in
            [("d1-2", "fee", "x"), ("d1-1", "usd_in", "pool:USD"), ("d1-1", "usd_in", "client:42:USD"), ("D1-9", "fee", "x")]]
    keys = [(r["idempotency_key"], r["account"]) for r in ex.sort_rows(rows)]
    assert keys == [("D1-9", "x"), ("d1-1", "client:42:USD"), ("d1-1", "pool:USD"), ("d1-2", "x")]


def test_write_csv_is_atomic_and_unix_newlines(tmp_path):
    out = tmp_path / "sub" / "run.csv"
    ex.write_csv([{c: "v" for c in ex.COLUMNS}], out)
    assert out.read_bytes().count(b"\r") == 0 and not (tmp_path / "sub" / "run.csv.tmp").exists()


def test_export_errors_are_exit_2(tmp_path, capsys):
    rc = ex.main(["--dsn", "postgresql://x:y@127.0.0.1:1/z?connect_timeout=1", "--out", str(tmp_path / "o.csv")])
    assert rc == 2 and "cannot read ledger" in capsys.readouterr().err


def test_fetch_rail_errors(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(ex.ExportError, match="cannot read transfers"):
        ex.fetch_rail("http://rail")


# ---- replay --------------------------------------------------------------

def _line(seq, key="k", **over):
    return {"seq": seq, "idempotency_key": key, "corridor": "USD-INR", "usd_amount": "1.00",
            "client_id": 1, "beneficiary_id": 1, "occurred_at": "2026-11-02T00:00:00Z", **over}


@pytest.mark.parametrize("content,msg", [
    ("", "no payments"),
    ("{bad json\n", "invalid JSON"),
    ("[1,2]\n", "JSON object"),
    (json.dumps({"seq": 1}) + "\n", "missing fields"),
    (json.dumps(_line(2)) + "\n" + json.dumps(_line(1)) + "\n", "increasing"),
    (json.dumps(_line(1, usd_amount=1.0)) + "\n", "must be a string"),
])
def test_load_traffic_validation(tmp_path, content, msg):
    p = tmp_path / "t.jsonl"
    p.write_text(content)
    with pytest.raises(replay.InputError, match=msg):
        replay.load_traffic(p)


def test_load_traffic_missing_file(tmp_path):
    with pytest.raises(replay.InputError, match="not found"):
        replay.load_traffic(tmp_path / "nope.jsonl")


def test_committed_traffic_loads(root):
    lines = replay.load_traffic(root / "traffic" / "day1.jsonl")
    assert len(lines) == 5000


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_one_classifies_and_strips_seq():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response({"a": 201, "b": 200, "c": 409, "d": 422}[body["idempotency_key"]])

    stats = replay.Stats()
    with _client(handler) as c:
        for k in "abcd":
            replay.send_one(c, "http://svc", _line(1, k), stats, max_retries=0)
    assert (stats.accepted, stats.duplicate, stats.conflict, stats.rejected, stats.errors) == (1, 1, 1, 1, 0)
    assert all("seq" not in b for b in seen)


def test_send_one_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(replay.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down")
        if calls["n"] == 2:
            return httpx.Response(503)
        return httpx.Response(201)

    stats = replay.Stats()
    with _client(handler) as c:
        replay.send_one(c, "http://svc", _line(1), stats, max_retries=3)
    assert stats.accepted == 1 and stats.retries == 2 and stats.errors == 0


def test_send_one_gives_up(monkeypatch):
    monkeypatch.setattr(replay.time, "sleep", lambda s: None)
    stats = replay.Stats()
    with _client(lambda r: httpx.Response(500)) as c:
        replay.send_one(c, "http://svc", _line(1), stats, max_retries=2)
    assert stats.errors == 1 and stats.sent == 1 and "HTTP 500" in stats.failures[0]


def test_wait_settled_times_out(monkeypatch):
    monkeypatch.setattr(replay.time, "sleep", lambda s: None)
    with _client(lambda r: httpx.Response(200, json={"pending": 3})) as c:
        with pytest.raises(TimeoutError, match="still pending"):
            replay.wait_settled(c, "http://svc", timeout=0, interval=0)


def test_replay_runs_in_parallel_and_sends_everything(monkeypatch):
    lock, active, peak, got = threading.Lock(), [0], [0], []

    def fake_send(client, url, line, stats, retries):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        import time
        time.sleep(0.01)
        with lock:
            active[0] -= 1
            got.append(line["seq"])
        stats.add("accepted")

    monkeypatch.setattr(replay, "send_one", fake_send)
    stats = replay.replay([_line(i, f"k{i}") for i in range(1, 41)], "http://svc", 8, 0, 5)
    assert stats.sent == 40 and sorted(got) == list(range(1, 41)) and peak[0] > 1


def test_replay_cli_bad_input_exit_2(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("nope\n")
    assert replay.main(["--file", str(p), "--url", "http://127.0.0.1:1"]) == 2


# ---- generator ---------------------------------------------------------------

def test_generator_is_byte_for_byte_reproducible(root):
    assert generator.render(generator.generate()) == (root / "traffic" / "day1.jsonl").read_text()


def test_generator_shape():
    lines = generator.generate()
    keys = [l["idempotency_key"] for l in lines]
    assert len(lines) == 5000 and [l["seq"] for l in lines] == list(range(1, 5001))
    assert len(keys) - len(set(keys)) == 150  # 3% retries
    by_key = {}
    for l in lines:
        body = {k: v for k, v in l.items() if k != "seq"}
        assert by_key.setdefault(l["idempotency_key"], body) == body  # retries have identical bodies
    share = {c: sum(l["corridor"] == c for l in lines) / len(lines) for c in generator.CORRIDORS}
    assert 0.45 < share["USD-INR"] < 0.55 and 0.25 < share["USD-MXN"] < 0.35 and 0.15 < share["USD-BRL"] < 0.25
    amounts = {l["usd_amount"] for l in lines}
    assert {"0.01", "0.05", "999999.99"} <= amounts
    assert sum(l["occurred_at"][11:13] == "18" for l in lines) > 400
    assert all(isinstance(l["usd_amount"], str) for l in lines)


def test_generator_different_seed_differs():
    assert generator.render(generator.generate(seed=1, count=500)) != generator.render(generator.generate(seed=2, count=500))


def test_generator_check_mode(tmp_path):
    out = tmp_path / "d.jsonl"
    assert generator.main(["--out", str(out), "--count", "500"]) == 0
    assert generator.main(["--out", str(out), "--count", "500", "--check"]) == 0
    out.write_text(out.read_text().replace("USD-INR", "USD-BRL", 1))
    assert generator.main(["--out", str(out), "--count", "500", "--check"]) == 1


def test_generator_rejects_tiny_count():
    with pytest.raises(ValueError):
        generator.generate(count=499)
