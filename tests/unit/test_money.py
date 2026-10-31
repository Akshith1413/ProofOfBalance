from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

import pytest

from app import money
from app.money import MoneyError

D = Decimal


def test_spec_example_usd_inr_100():
    q = money.quote("USD-INR", D("100.00"), D("83.25"), 20, ROUND_HALF_EVEN)
    assert (q.fee, q.net_usd, q.dest_amount, q.dest_currency) == (D("0.20"), D("99.80"), D("8308.35"), "INR")
    rows = [(p.leg, p.account, p.currency, p.amount) for p in money.initial_postings(42, q)]
    assert rows == [
        ("usd_in", "client:42:USD", "USD", D("-100.00")),
        ("usd_in", "pool:USD", "USD", D("99.80")),
        ("fee", "revenue:fx_fee:USD", "USD", D("0.20")),
        ("fx_out", "pool:INR", "INR", D("-8308.35")),
        ("fx_out", "payout_clearing:INR", "INR", D("8308.35")),
    ]
    payout = [(p.leg, p.account, p.amount) for p in money.payout_postings(7, "INR", q.dest_amount)]
    assert payout == [("payout", "payout_clearing:INR", D("-8308.35")), ("payout", "beneficiary:7:INR", D("8308.35"))]


@pytest.mark.parametrize("amount", ["0.01", "0.05", "2.50", "999999.99", "1234.57", "100.00"])
@pytest.mark.parametrize("corridor,rate", [("USD-INR", "83.25"), ("USD-MXN", "18.2450"), ("USD-BRL", "5.4125")])
@pytest.mark.parametrize("rounding", [ROUND_HALF_EVEN, ROUND_HALF_UP])
def test_every_payment_balances_per_currency(amount, corridor, rate, rounding):
    q = money.quote(corridor, D(amount), D(rate), 20, rounding)
    postings = money.initial_postings(1, q) + money.payout_postings(2, q.dest_currency, q.dest_amount)
    assert money.imbalances(postings) == {}
    assert all(p.amount == p.amount.quantize(D("0.01")) for p in postings)


def test_fee_half_cent_depends_on_rounding_mode():
    # 2.50 * 20 bps = 0.005 exactly
    assert money.quote("USD-INR", D("2.50"), D("83.25"), 20, ROUND_HALF_EVEN).fee == D("0.00")
    assert money.quote("USD-INR", D("2.50"), D("83.25"), 20, ROUND_HALF_UP).fee == D("0.01")


def test_conversion_half_cent_depends_on_rounding_mode():
    # fee 1.06 * 20 bps = 0.00212 -> 0.00; net 1.06 * 83.25 = 88.245 exactly
    usd = D("1.06")  # fee 0.00212 -> 0.00, net 1.06
    assert money.quote("USD-INR", usd, D("83.25"), 20, ROUND_HALF_EVEN).dest_amount == D("88.24")
    assert money.quote("USD-INR", usd, D("83.25"), 20, ROUND_HALF_UP).dest_amount == D("88.25")
    assert money.conversion_is_tie(usd, D("83.25"), 20)


def test_tiny_amount_has_zero_fee_but_still_converts():
    q = money.quote("USD-INR", D("0.01"), D("83.25"), 20, ROUND_HALF_EVEN)
    assert q.fee == D("0.00") and q.dest_amount == D("0.83")


def test_largest_amount_is_exact():
    q = money.quote("USD-INR", D("999999.99"), D("83.25"), 20, ROUND_HALF_EVEN)
    assert q.fee == D("2000.00") and q.net_usd == D("997999.99")
    assert q.dest_amount == D("83083499.17")  # 997999.99 * 83.25 = 83083499.1675


@pytest.mark.parametrize("bad", [1.5, True])
def test_floats_and_bools_are_rejected(bad):
    with pytest.raises(TypeError):
        money.to_decimal(bad)
    with pytest.raises(TypeError):
        money.quote("USD-INR", bad, D("83.25"), 20, ROUND_HALF_EVEN)


@pytest.mark.parametrize("amount", ["0", "-1.00", "1.001"])
def test_invalid_amounts(amount):
    with pytest.raises(MoneyError):
        money.quote("USD-INR", D(amount), D("83.25"), 20, ROUND_HALF_EVEN)


@pytest.mark.parametrize("value", ["abc", "NaN", "Infinity", ""])
def test_non_numbers(value):
    with pytest.raises(MoneyError):
        money.to_decimal(value)


def test_bad_corridor_and_fee():
    with pytest.raises(MoneyError):
        money.quote("INR-USD", D("1.00"), D("1"), 20, ROUND_HALF_EVEN)
    with pytest.raises(MoneyError):
        money.quote("USD-INR", D("1.00"), D("1"), 10000, ROUND_HALF_EVEN)
    with pytest.raises(MoneyError):
        money.quote("USD-INR", D("1.00"), D("0"), 20, ROUND_HALF_EVEN)


def test_amount_fully_consumed_or_rounds_to_zero():
    with pytest.raises(MoneyError, match="fee"):
        money.quote("USD-INR", D("0.01"), D("83.25"), 9999, ROUND_HALF_UP)  # fee 0.009999 -> 0.01
    with pytest.raises(MoneyError, match="too small"):
        money.quote("USD-XXX", D("0.01"), D("0.1"), 0, ROUND_HALF_EVEN)  # 0.001 -> 0.00


@pytest.mark.parametrize("raw,expected", [
    (None, ROUND_HALF_EVEN), ("", ROUND_HALF_EVEN), ("ROUND_HALF_UP", ROUND_HALF_UP),
    ("half_up", ROUND_HALF_UP), (" round_half_even ", ROUND_HALF_EVEN),
])
def test_parse_rounding_mode(raw, expected):
    assert money.parse_rounding_mode(raw) == expected


def test_parse_rounding_mode_rejects_unknown():
    with pytest.raises(MoneyError):
        money.parse_rounding_mode("BANKERS")


def test_imbalances_detects_broken_payment():
    bad = [money.Posting("usd_in", "a", "USD", D("-1.00")), money.Posting("usd_in", "b", "USD", D("0.99"))]
    assert money.imbalances(bad) == {"USD": D("-0.01")}


def test_payout_must_be_positive():
    with pytest.raises(MoneyError):
        money.payout_postings(1, "INR", D("0"))
