#!/usr/bin/env python3
"""Replay recorded traffic against the settlement service.

    python tools/replay.py --file traffic/day1.jsonl --url http://localhost:8080 --workers 8

* Validates the whole file before sending anything.
* Sends every line as POST /payments, WORKERS at a time, dispatched in seq
  order. Running in parallel is deliberate: it makes retried keys race.
* Retries transient failures (connection errors, 5xx) with backoff.
* Then polls GET /stats until nothing is pending (default timeout 5 min).

Exit codes: 0 ok, 1 send errors or pending timeout, 2 bad input / service never ready.
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BODY_FIELDS = ("idempotency_key", "corridor", "usd_amount", "client_id", "beneficiary_id", "occurred_at")


class InputError(ValueError):
    pass


def load_traffic(path: Path) -> list[dict]:
    if not path.is_file():
        raise InputError(f"traffic file not found: {path}")
    lines: list[dict] = []
    last_seq = 0
    with path.open(encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise InputError(f"{path}:{n}: invalid JSON: {exc}") from None
            if not isinstance(obj, dict):
                raise InputError(f"{path}:{n}: expected a JSON object")
            missing = [f for f in ("seq", *BODY_FIELDS) if f not in obj]
            if missing:
                raise InputError(f"{path}:{n}: missing fields {missing}")
            seq = obj["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
                raise InputError(f"{path}:{n}: seq must be an increasing integer (got {seq!r} after {last_seq})")
            if not isinstance(obj["usd_amount"], str):
                raise InputError(f"{path}:{n}: usd_amount must be a string, not {type(obj['usd_amount']).__name__}")
            last_seq = seq
            lines.append(obj)
    if not lines:
        raise InputError(f"{path} contains no payments")
    return lines


@dataclass
class Stats:
    sent: int = 0
    accepted: int = 0
    duplicate: int = 0
    conflict: int = 0
    rejected: int = 0
    errors: int = 0
    retries: int = 0
    failures: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, kind: str, note: str | None = None) -> None:
        with self.lock:
            self.sent += 1
            setattr(self, kind, getattr(self, kind) + 1)
            if note and len(self.failures) < 20:
                self.failures.append(note)


def send_one(client: httpx.Client, url: str, line: dict, stats: Stats, max_retries: int) -> None:
    body = {k: line[k] for k in BODY_FIELDS}
    delay = 0.2
    for attempt in range(max_retries + 1):
        try:
            r = client.post(f"{url}/payments", json=body)
        except httpx.TransportError as exc:
            err = f"seq {line['seq']}: {type(exc).__name__}: {exc}"
        else:
            if r.status_code == 201:
                return stats.add("accepted")
            if r.status_code == 200:
                return stats.add("duplicate")
            if r.status_code == 409:
                return stats.add("conflict", f"seq {line['seq']}: 409 {r.text[:200]}")
            if 400 <= r.status_code < 500:
                return stats.add("rejected", f"seq {line['seq']}: {r.status_code} {r.text[:200]}")
            err = f"seq {line['seq']}: HTTP {r.status_code}"
        if attempt < max_retries:
            with stats.lock:
                stats.retries += 1
            time.sleep(delay)
            delay = min(delay * 2, 5)
    stats.add("errors", err)


def wait_ready(client: httpx.Client, url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        try:
            r = client.get(f"{url}/readyz")
            if r.status_code == 200:
                return
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except httpx.TransportError as exc:
            last = str(exc)
        time.sleep(1)
    raise InputError(f"service at {url} not ready after {timeout:.0f}s ({last})")


def wait_settled(client: httpx.Client, url: str, timeout: float, interval: float) -> dict:
    deadline = time.monotonic() + timeout
    counts: dict = {}
    while True:
        try:
            r = client.get(f"{url}/stats")
            if r.status_code == 200:
                counts = r.json()
                if counts.get("pending", 1) == 0:
                    return counts
        except (httpx.TransportError, ValueError):
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"payments still pending after {timeout:.0f}s: {counts}")
        time.sleep(interval)


def replay(lines: list[dict], url: str, workers: int, max_retries: int, timeout: float) -> Stats:
    stats = Stats()
    work: queue.Queue = queue.Queue()
    for line in lines:  # FIFO queue: lines are dispatched in seq order
        work.put(line)

    def run():
        with httpx.Client(timeout=timeout) as client:
            while True:
                try:
                    line = work.get_nowait()
                except queue.Empty:
                    return
                send_one(client, url, line, stats, max_retries)

    threads = [threading.Thread(target=run, name=f"replay-{i}") for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path, required=True)
    ap.add_argument("--url", required=True, help="settlement service base URL")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-retries", type=int, default=5)
    ap.add_argument("--request-timeout", type=float, default=30)
    ap.add_argument("--ready-timeout", type=float, default=120)
    ap.add_argument("--settle-timeout", type=float, default=300, help="seconds to wait for pending == 0")
    ap.add_argument("--poll-interval", type=float, default=2)
    ap.add_argument("--no-wait", action="store_true", help="do not wait for payouts to settle")
    ap.add_argument("--summary-json", type=Path)
    args = ap.parse_args(argv)
    if not 1 <= args.workers <= 256:
        ap.error("--workers must be between 1 and 256")
    url = args.url.rstrip("/")

    try:
        lines = load_traffic(args.file)
        with httpx.Client(timeout=10) as c:
            wait_ready(c, url, args.ready_timeout)
    except InputError as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 2

    t0 = time.monotonic()
    stats = replay(lines, url, args.workers, args.max_retries, args.request_timeout)
    sent_s = time.monotonic() - t0
    print(f"replay: sent={stats.sent} accepted={stats.accepted} duplicate={stats.duplicate} "
          f"conflict={stats.conflict} rejected={stats.rejected} errors={stats.errors} "
          f"retries={stats.retries} in {sent_s:.1f}s")
    for note in stats.failures:
        print(f"replay:   {note}", file=sys.stderr)

    counts, rc = {}, 0
    if not args.no_wait:
        try:
            with httpx.Client(timeout=10) as c:
                counts = wait_settled(c, url, args.settle_timeout, args.poll_interval)
            print(f"replay: settled paid={counts.get('paid')} failed={counts.get('failed')} "
                  f"total={counts.get('total')} after {time.monotonic() - t0:.1f}s")
        except TimeoutError as exc:
            print(f"replay: {exc}", file=sys.stderr)
            rc = 1
    if stats.errors:
        rc = 1

    if args.summary_json:
        args.summary_json.write_text(json.dumps({
            "sent": stats.sent, "accepted": stats.accepted, "duplicate": stats.duplicate,
            "conflict": stats.conflict, "rejected": stats.rejected, "errors": stats.errors,
            "retries": stats.retries, "seconds": round(time.monotonic() - t0, 1), "settled": counts,
        }, indent=2) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
