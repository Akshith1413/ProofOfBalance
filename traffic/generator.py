#!/usr/bin/env python3
"""Generate one day of synthetic payment traffic: traffic/day1.jsonl.

Seeded with random.Random(42), so the output is byte-for-byte reproducible:

    python traffic/generator.py            # (re)write traffic/day1.jsonl
    python traffic/generator.py --check    # exit 1 if the committed file differs

What is in it, and why:
  * ~5,000 lines; corridors ~50% INR / 30% MXN / 20% BRL.
  * Awkward amounts: 0.01, 0.05, 999,999.99, fees that land on half a cent
    (2.50, 7.50, ...) and converted values that land exactly on half a cent.
    These are what a ROUNDING_MODE change moves.
  * ~3% retries: later lines reusing an earlier idempotency_key with an
    identical body, a few lines later, so parallel replay makes them race.
    These expose duplicate payouts.
  * A burst between 18:00 and 19:00 UTC, which is past midnight in India:
    a SETTLEMENT_TZ change moves their value_date.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "service"))

from app import money  # noqa: E402  (single source of truth for the maths)
from app.rates import load_rates  # noqa: E402

CORRIDORS = ["USD-INR", "USD-MXN", "USD-BRL"]
CORRIDOR_WEIGHTS = [50, 30, 20]
SPECIAL_AMOUNTS = [("0.01", 12), ("0.05", 12), ("999999.99", 6)]
FEE_TIES = 60            # amounts whose 20 bps fee is exactly x.xx5
CONVERSION_TIES = 40     # per corridor, converted value exactly on x.xx5
MIDNIGHT_SHARE = 0.12    # share of payments in the 18:00-19:00 UTC window


def _fmt_cents(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _random_cents(rng: random.Random) -> int:
    band = rng.random()
    if band < 0.70:
        lo, hi = 100, 200_000          # $1 .. $2,000
    elif band < 0.95:
        lo, hi = 200_000, 5_000_000    # $2,000 .. $50,000
    else:
        lo, hi = 5_000_000, 50_000_000  # $50,000 .. $500,000
    # log-uniform inside the band

    return int(math.exp(rng.uniform(math.log(lo), math.log(hi))))


def _rounding_sensitive(amount: str, corridor: str, rates) -> bool:
    """True if HALF_EVEN and HALF_UP give a different ledger for this payment."""
    rate = rates.rate(corridor)
    a = money.quote(corridor, Decimal(amount), rate, rates.fee_bps, ROUND_HALF_EVEN)
    b = money.quote(corridor, Decimal(amount), rate, rates.fee_bps, ROUND_HALF_UP)
    return (a.fee, a.dest_amount) != (b.fee, b.dest_amount)


def generate(seed: int = 42, count: int = 5000, day: str = "2026-11-02", retry_ratio: float = 0.03,
             rates_file: Path = ROOT / "service" / "rates.yaml") -> list[dict]:
    if count < 500:
        raise ValueError("count must be >= 500 so every kind of awkward payment fits")
    rng = random.Random(seed)
    rates = load_rates(rates_file)
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)

    n_retry = round(count * retry_ratio)
    n_unique = count - n_retry

    corridors = rng.choices(CORRIDORS, weights=CORRIDOR_WEIGHTS, k=n_unique)
    amounts = [_fmt_cents(_random_cents(rng)) for _ in range(n_unique)]

    # Plant the awkward amounts at random positions (never the same slot twice).
    slots = rng.sample(range(n_unique), k=sum(n for _, n in SPECIAL_AMOUNTS) + FEE_TIES + CONVERSION_TIES * len(CORRIDORS))
    it = iter(slots)
    for value, n in SPECIAL_AMOUNTS:
        for _ in range(n):
            amounts[next(it)] = value
    for _ in range(FEE_TIES):
        amounts[next(it)] = _fmt_cents(500 * rng.randint(0, 2000) + 250)  # fee = x.xx5 at 20 bps
    for corridor in CORRIDORS:
        for _ in range(CONVERSION_TIES):
            while True:
                cand = _fmt_cents(rng.randint(100, 500_000))
                if money.conversion_is_tie(Decimal(cand), rates.rate(corridor), rates.fee_bps) and \
                        _rounding_sensitive(cand, corridor, rates):
                    break
            slot = next(it)
            corridors[slot], amounts[slot] = corridor, cand

    rows = []
    for i in range(n_unique):
        if rng.random() < MIDNIGHT_SHARE:
            offset = 18 * 3600 + rng.randrange(3600)
        else:
            offset = rng.randrange(86400)
        rows.append({
            "_t": offset,
            "_i": i,
            "corridor": corridors[i],
            "usd_amount": amounts[i],
            "client_id": rng.randint(1, 500),
            "beneficiary_id": rng.randint(1, 2000),
            "occurred_at": (start + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    rows.sort(key=lambda r: (r["_t"], r["_i"]))
    for n, r in enumerate(rows, 1):
        r["idempotency_key"] = f"d1-{n:06d}"

    # Retries land 1..12 lines after their original: close enough to race
    # across 8 parallel replay workers.
    ordered = [(float(pos), r) for pos, r in enumerate(rows)]
    for pos in rng.sample(range(n_unique), k=n_retry):
        ordered.append((pos + rng.randint(1, 12) + 0.5, rows[pos]))
    ordered.sort(key=lambda x: x[0])

    out = []
    for seq, (_, r) in enumerate(ordered, 1):
        out.append({
            "seq": seq,
            "idempotency_key": r["idempotency_key"],
            "corridor": r["corridor"],
            "usd_amount": r["usd_amount"],
            "client_id": r["client_id"],
            "beneficiary_id": r["beneficiary_id"],
            "occurred_at": r["occurred_at"],
        })
    return out


def render(lines: list[dict]) -> str:
    return "".join(json.dumps(line) + "\n" for line in lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "traffic" / "day1.jsonl")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--count", type=int, default=5000)
    ap.add_argument("--day", default="2026-11-02")
    ap.add_argument("--check", action="store_true", help="verify --out matches what would be generated")
    args = ap.parse_args(argv)

    text = render(generate(args.seed, args.count, args.day))
    if args.check:
        if not args.out.is_file():
            print(f"{args.out} does not exist", file=sys.stderr)
            return 1
        if args.out.read_text(encoding="utf-8") != text:
            print(f"{args.out} differs from a fresh generation (seed={args.seed}). "
                  "Either it was edited by hand or the generator/rates changed.", file=sys.stderr)
            return 1
        print(f"{args.out} is reproducible ({text.count(chr(10))} lines)")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(args.out)
    print(f"wrote {args.out} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
