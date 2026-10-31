"""Deterministic money maths for ProofOfBalance.

Pure standard library on purpose: the traffic generator imports this module to
find "awkward" amounts, and the unit tests exercise it without a database.

Rules:
  * Decimal everywhere. Passing a float raises TypeError: a float is a bug.
  * Every rounded value goes through ``round_money`` with an explicit rounding mode.
  * Posting amounts are signed: debit < 0, credit > 0.
"""
from __future__ import annotations

import decimal
import re
from dataclasses import dataclass
from decimal import Decimal

CENT = Decimal("0.01")
BPS_DIVISOR = Decimal(10000)
HOME_CURRENCY = "USD"

ROUNDING_MODES: dict[str, str] = {
    name: getattr(decimal, name)
    for name in (
        "ROUND_HALF_EVEN",
        "ROUND_HALF_UP",
        "ROUND_HALF_DOWN",
        "ROUND_UP",
        "ROUND_DOWN",
        "ROUND_CEILING",
        "ROUND_FLOOR",
        "ROUND_05UP",
    )
}

_CORRIDOR_RE = re.compile(r"^USD-([A-Z]{3})$")

# Plenty of headroom: NUMERIC(20,8) * NUMERIC(20,2) needs ~30 significant digits.
_CTX = decimal.Context(prec=60, rounding=decimal.ROUND_HALF_EVEN, traps=[decimal.InvalidOperation])


class MoneyError(ValueError):
    """Input that can never produce a valid payment (bad corridor, amount too small...)."""


def parse_rounding_mode(value: str | None) -> str:
    """Accept 'ROUND_HALF_EVEN' or 'HALF_EVEN' (any case). Unknown values are an error."""
    if value is None or not value.strip():
        return decimal.ROUND_HALF_EVEN
    name = value.strip().upper()
    if not name.startswith("ROUND_"):
        name = "ROUND_" + name
    if name not in ROUNDING_MODES:
        raise MoneyError(f"unknown ROUNDING_MODE {value!r}; expected one of {sorted(ROUNDING_MODES)}")
    return ROUNDING_MODES[name]


def to_decimal(value: object) -> Decimal:
    """Convert str/int/Decimal to Decimal. Floats and bools are rejected."""
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"refusing to treat {type(value).__name__} {value!r} as money; use a string")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except decimal.InvalidOperation as exc:
            raise MoneyError(f"not a decimal number: {value!r}") from exc
    else:
        raise TypeError(f"unsupported money type {type(value).__name__}")
    if not d.is_finite():
        raise MoneyError(f"not a finite number: {value!r}")
    return d


def round_money(value: Decimal, rounding: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError("round_money needs a Decimal")
    return value.quantize(CENT, rounding=rounding, context=_CTX)


def destination_currency(corridor: str) -> str:
    m = _CORRIDOR_RE.match(corridor or "")
    if not m:
        raise MoneyError(f"corridor must look like 'USD-INR', got {corridor!r}")
    return m.group(1)


@dataclass(frozen=True)
class Quote:
    corridor: str
    usd_amount: Decimal
    fee: Decimal
    net_usd: Decimal
    rate: Decimal
    dest_currency: str
    dest_amount: Decimal


def quote(corridor: str, usd_amount: Decimal, rate: Decimal, fee_bps: int, rounding: str) -> Quote:
    """Price one payment. ``usd_amount`` must already be a positive 2-dp Decimal."""
    usd_amount = to_decimal(usd_amount)
    rate = to_decimal(rate)
    if usd_amount <= 0:
        raise MoneyError("usd_amount must be > 0")
    if usd_amount != usd_amount.quantize(CENT, context=_CTX):
        raise MoneyError("usd_amount must have at most 2 decimal places")
    if rate <= 0:
        raise MoneyError("rate must be > 0")
    if isinstance(fee_bps, bool) or not isinstance(fee_bps, int) or not 0 <= fee_bps < 10000:
        raise MoneyError("fee_bps must be an int in [0, 10000)")
    dest = destination_currency(corridor)

    with decimal.localcontext(_CTX):
        fee = round_money(usd_amount * fee_bps / BPS_DIVISOR, rounding)
        net = usd_amount - fee
        if net <= 0:
            raise MoneyError("amount is fully consumed by the fee")
        dest_amount = round_money(net * rate, rounding)
    if dest_amount <= 0:
        raise MoneyError("amount is too small to convert (rounds to zero)")
    return Quote(corridor, usd_amount, fee, net, rate, dest, dest_amount)


@dataclass(frozen=True)
class Posting:
    leg: str
    account: str
    currency: str
    amount: Decimal


def initial_postings(client_id: int, q: Quote) -> list[Posting]:
    """The five postings written when a payment is accepted."""
    return [
        Posting("usd_in", f"client:{client_id}:USD", HOME_CURRENCY, -q.usd_amount),
        Posting("usd_in", f"pool:{HOME_CURRENCY}", HOME_CURRENCY, q.net_usd),
        Posting("fee", f"revenue:fx_fee:{HOME_CURRENCY}", HOME_CURRENCY, q.fee),
        Posting("fx_out", f"pool:{q.dest_currency}", q.dest_currency, -q.dest_amount),
        Posting("fx_out", f"payout_clearing:{q.dest_currency}", q.dest_currency, q.dest_amount),
    ]


def payout_postings(beneficiary_id: int, currency: str, amount: Decimal) -> list[Posting]:
    """The two postings the payout worker adds once the rail has paid."""
    amount = to_decimal(amount)
    if amount <= 0:
        raise MoneyError("payout amount must be > 0")
    return [
        Posting("payout", f"payout_clearing:{currency}", currency, -amount),
        Posting("payout", f"beneficiary:{beneficiary_id}:{currency}", currency, amount),
    ]


def imbalances(postings: list[Posting]) -> dict[str, Decimal]:
    """Per-currency sums that are not zero. Empty dict means balanced."""
    totals: dict[str, Decimal] = {}
    for p in postings:
        totals[p.currency] = totals.get(p.currency, Decimal(0)) + to_decimal(p.amount)
    return {cur: total for cur, total in totals.items() if total != 0}


def conversion_is_tie(usd_amount: Decimal, rate: Decimal, fee_bps: int, rounding: str = decimal.ROUND_HALF_EVEN) -> bool:
    """True when the fee or the converted value lands exactly on half a cent.

    These are the amounts whose ledger changes when ROUNDING_MODE changes.
    """
    usd_amount, rate = to_decimal(usd_amount), to_decimal(rate)
    with decimal.localcontext(_CTX):
        raw_fee = usd_amount * fee_bps / BPS_DIVISOR
        fee = round_money(raw_fee, rounding)
        raw_dest = (usd_amount - fee) * rate
    half = Decimal("0.005")
    return (raw_fee % CENT) == half or (raw_dest % CENT) == half
