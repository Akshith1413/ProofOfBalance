"""Trickle: slow, endless live traffic so a canary always has payments to judge.

Replays TRICKLE_FILE (day1.jsonl or day1.jsonl.gz, mounted from a ConfigMap)
in a loop at TRICKLE_RPS, giving every line a fresh idempotency key per loop.
Retries inside the file keep pointing at the same fresh key, so they still
exercise idempotency. If the file is missing it synthesises similar traffic.

Never used by the CI gate: its keys and timestamps are not deterministic.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import random
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .logs import setup_logging

log = logging.getLogger("pob.trickle")

FIELDS = ("idempotency_key", "corridor", "usd_amount", "client_id", "beneficiary_id", "occurred_at")


def load_lines(path: str) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    opener = gzip.open if p.suffix == ".gz" else open
    out = []
    with opener(p, "rt", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                out.append({k: obj[k] for k in FIELDS})
            except (ValueError, KeyError) as exc:
                log.warning("skipping bad line %d in %s: %s", n, p, exc)
    return out


def synthetic(rng: random.Random) -> dict:
    corridor = rng.choices(["USD-INR", "USD-MXN", "USD-BRL"], weights=[50, 30, 20])[0]
    cents = int(10 ** rng.uniform(2, 6))  # $1 .. $10,000
    return {
        "idempotency_key": f"syn-{rng.getrandbits(48):012x}",
        "corridor": corridor,
        "usd_amount": f"{cents // 100}.{cents % 100:02d}",
        "client_id": rng.randint(1, 500),
        "beneficiary_id": rng.randint(1, 2000),
        "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main() -> None:
    setup_logging("trickle", os.environ.get("LOG_LEVEL", "INFO").upper())
    url = os.environ.get("SERVICE_URL", "http://localhost:8080").rstrip("/")
    rps = max(0.1, float(os.environ.get("TRICKLE_RPS", "2")))
    lines = load_lines(os.environ.get("TRICKLE_FILE", "/traffic/day1.jsonl.gz"))
    run_id = f"{os.environ.get('HOSTNAME', 'local')[-8:]}-{int(time.time())}"
    rng = random.Random()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("trickle started", extra={"url": url, "rps": rps, "lines": len(lines), "run_id": run_id})

    client = httpx.Client(timeout=10)
    loop_no, i, backoff, sent = 0, 0, 1.0, 0
    while not stop.is_set():
        if lines:
            if i >= len(lines):
                i, loop_no = 0, loop_no + 1
            body = dict(lines[i])
            body["idempotency_key"] = f"tr-{run_id}-{loop_no}-{body['idempotency_key']}"[:128]
            i += 1
        else:
            body = synthetic(rng)
        try:
            r = client.post(f"{url}/payments", json=body)
            if r.status_code >= 500:
                raise httpx.HTTPStatusError("server error", request=r.request, response=r)
            sent += 1
            backoff = 1.0
            if sent % 500 == 0:
                log.info("trickle progress", extra={"sent": sent, "loop": loop_no})
        except httpx.HTTPError as exc:
            log.warning("service unavailable, backing off %.0fs: %s", backoff, exc)
            stop.wait(backoff)
            backoff = min(backoff * 2, 30)
            continue
        stop.wait(1 / rps)
    client.close()


if __name__ == "__main__":
    main()
