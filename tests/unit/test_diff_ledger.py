import csv
from pathlib import Path

import pytest

import diff_ledger as dl

COLS = dl.COLUMNS


def payment(key="d1-000001", corridor="USD-INR", status="paid", vdate="2026-11-02", usd="100.00",
            fee="0.20", net="99.80", dest="8308.35", rail="1", payout=True):
    cur = corridor.split("-")[1]
    tx = "ab" * 32 if status == "paid" else ""
    rows = [
        (key, corridor, status, vdate, tx, "fee", "revenue:fx_fee:USD", "USD", fee),
        (key, corridor, status, vdate, tx, "fx_out", f"payout_clearing:{cur}", cur, dest),
        (key, corridor, status, vdate, tx, "fx_out", f"pool:{cur}", cur, f"-{dest}"),
        (key, corridor, status, vdate, tx, "usd_in", "client:42:USD", "USD", f"-{usd}"),
        (key, corridor, status, vdate, tx, "usd_in", "pool:USD", "USD", net),
        (key, corridor, status, vdate, tx, "rail", f"rail:transfers:{cur}", cur, rail),
    ]
    if payout:
        rows += [
            (key, corridor, status, vdate, tx, "payout", f"beneficiary:7:{cur}", cur, dest),
            (key, corridor, status, vdate, tx, "payout", f"payout_clearing:{cur}", cur, f"-{dest}"),
        ]
    return rows


def write(path: Path, rows):
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(COLS)
        w.writerows(sorted(rows, key=lambda r: (r[0], r[5], r[6])))
    return path


def run(tmp_path, base_rows, run_rows):
    b = write(tmp_path / "base.csv", base_rows)
    r = write(tmp_path / "run.csv", run_rows)
    rep = tmp_path / "report.md"
    rc = dl.main(["--baseline", str(b), "--run", str(r), "--report", str(rep)])
    return rc, rep.read_text(), (tmp_path / "diff_rows.csv")


BASE = payment() + payment("d1-000002", "USD-MXN", dest="1820.85") + payment("d1-000003", "USD-BRL", status="failed", payout=False, dest="540.17")


def test_identical_passes(tmp_path):
    rc, report, rows = run(tmp_path, BASE, BASE)
    assert rc == 0
    assert "PASSED" in report and "Invariant check: passed" in report
    assert rows.read_text().count("\n") == 1  # header only


def test_rounding_change_is_amount_changed_with_net(tmp_path):
    changed = payment(dest="8308.36") + payment("d1-000002", "USD-MXN", dest="1820.85") + \
        payment("d1-000003", "USD-BRL", status="failed", payout=False, dest="540.17")
    rc, report, rows = run(tmp_path, BASE, changed)
    assert rc == 1
    assert "FAILED — 1 corridor changed" in report
    assert "| USD-INR | 1 | +0.01 INR | amount changed |" in report
    assert "Invariant check: passed" in report
    assert rows.read_text().count("amount changed") == 4  # clearing, pool, beneficiary, payout clearing


def test_status_and_value_date_and_missing(tmp_path):
    changed = payment(vdate="2026-11-03") + payment("d1-000002", "USD-MXN", status="failed", payout=False, dest="1820.85")
    rc, report, _ = run(tmp_path, BASE, changed)
    assert rc == 1
    assert "FAILED — 3 corridors changed" in report
    assert "| USD-INR | 1 | 0.00 | value date changed |" in report
    assert "missing" in report and "status changed" in report and "tx hash changed" in report
    assert "| USD-BRL | 1 | -540.17 BRL, -0.20 USD | missing |" in report


def test_extra_payment(tmp_path):
    rc, report, _ = run(tmp_path, BASE, BASE + payment("d1-999999"))
    assert rc == 1 and "| USD-INR | 1 | +8,308.35 INR, +0.20 USD | extra |" in report


def test_duplicate_payout_detected_via_rail_row(tmp_path):
    dup = payment(rail="2") + BASE[8:]
    rc, report, _ = run(tmp_path, BASE, dup)
    assert rc == 1 and "duplicate payout" in report


def test_invariant_violation_fails_even_if_identical(tmp_path):
    broken = [r if r[6] != "pool:USD" else r[:8] + ("99.79",) for r in BASE]
    rc, report, _ = run(tmp_path, broken, broken)
    assert rc == 1
    assert "ledger invariant broken" in report and "| d1-000001 | USD | -0.01 |" in report


def test_rail_rows_do_not_count_for_invariant(tmp_path):
    rc, _, _ = run(tmp_path, BASE, BASE)
    assert rc == 0  # rail rows carry counts (1) which would otherwise unbalance every payment


def test_bad_inputs_exit_2(tmp_path):
    good = write(tmp_path / "good.csv", BASE)
    rep = tmp_path / "r.md"
    cases = {
        "missing.csv": None,
        "empty.csv": "",
        "header.csv": "a,b,c\n1,2,3\n",
        "amount.csv": ",".join(COLS) + "\nk,USD-INR,paid,2026-11-02,,fee,x,USD,abc\n",
        "dup.csv": ",".join(COLS) + "\n" + "k,USD-INR,paid,2026-11-02,,fee,x,USD,1\n" * 2,
        "short.csv": ",".join(COLS) + "\nk,USD-INR\n",
    }
    for name, text in cases.items():
        p = tmp_path / name
        if text is not None:
            p.write_text(text)
        assert dl.main(["--baseline", str(good), "--run", str(p), "--report", str(rep)]) == 2, name
        assert "ERROR" in rep.read_text()


def test_sample_rows_limited(tmp_path):
    changed = [r[:8] + ("1",) if r[5] == "rail" else r for r in BASE]
    changed = [r[:8] + ("2",) if r[5] == "rail" else r for r in changed]
    rc, report, _ = run(tmp_path, BASE, changed)
    assert rc == 1 and report.count("duplicate payout |") >= 1


@pytest.mark.parametrize("amount", ["8308.350", "8308.35"])
def test_amount_compared_numerically(tmp_path, amount):
    same = [r[:8] + (amount,) if r[6] == "payout_clearing:INR" and r[5] == "fx_out" else r for r in BASE]
    rc, _, _ = run(tmp_path, BASE, same)
    assert rc == 0
