#!/usr/bin/env python3
"""Diff a ledger export against the committed baseline.

    python tools/diff_ledger.py --baseline baseline/ledger_baseline.csv --run run.csv --report report.md

* Rows are keyed by (idempotency_key, leg, account).
* Every difference is classified: missing, extra, amount changed, status
  changed, value date changed, tx hash changed, corridor changed, and
  duplicate payout (the rail received more transfers than in the baseline).
* The run is also checked for the ledger invariant: postings sum to zero per
  payment per currency (the ``rail`` rows are counts, not money, and are skipped).
* Writes a markdown report grouped by corridor, plus every differing row to
  --rows-out (default: diff_rows.csv next to the report).

"Net change" per corridor is the change in value owed to beneficiaries (the
fx_out credit to payout_clearing, in the destination currency) and in fee
revenue (USD). Those are the two numbers a rounding or rate change moves.

Exit codes: 0 identical and balanced, 1 any difference or imbalance, 2 bad input.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

COLUMNS = ["idempotency_key", "corridor", "status", "value_date", "tx_hash", "leg", "account", "currency", "amount"]
KIND_ORDER = ["missing", "extra", "amount changed", "duplicate payout", "status changed",
              "value date changed", "tx hash changed", "corridor changed"]
Key = tuple[str, str, str]


class LedgerFileError(ValueError):
    pass


def load(path: Path) -> dict[Key, dict]:
    if not path.is_file():
        raise LedgerFileError(f"{path}: file not found")
    rows: dict[Key, dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise LedgerFileError(f"{path}: empty file (no header)")
        if [c.strip() for c in reader.fieldnames] != COLUMNS:
            raise LedgerFileError(f"{path}: header {reader.fieldnames} does not match expected {COLUMNS}")
        for n, row in enumerate(reader, 2):
            if None in row or any(row.get(c) is None for c in COLUMNS):
                raise LedgerFileError(f"{path}:{n}: wrong number of columns")
            row = {c: row[c].strip() for c in COLUMNS}
            try:
                row["_amount"] = Decimal(row["amount"])
            except InvalidOperation:
                raise LedgerFileError(f"{path}:{n}: amount {row['amount']!r} is not a decimal") from None
            if not row["_amount"].is_finite():
                raise LedgerFileError(f"{path}:{n}: amount {row['amount']!r} is not finite")
            key = (row["idempotency_key"], row["leg"], row["account"])
            if key in rows:
                raise LedgerFileError(f"{path}:{n}: duplicate row for key {key}")
            rows[key] = row
    return rows


@dataclass
class Difference:
    key: Key
    corridor: str
    kinds: list[str]
    base: dict | None
    run: dict | None


def classify(base: dict | None, run: dict | None) -> list[str]:
    if base is None:
        return ["extra"]
    if run is None:
        return ["missing"]
    kinds = []
    if base["_amount"] != run["_amount"]:
        if base["leg"] == "rail" and run["_amount"] > base["_amount"]:
            kinds.append("duplicate payout")
        else:
            kinds.append("amount changed")
    if base["status"] != run["status"]:
        kinds.append("status changed")
    if base["value_date"] != run["value_date"]:
        kinds.append("value date changed")
    if base["tx_hash"] != run["tx_hash"]:
        kinds.append("tx hash changed")
    if base["corridor"] != run["corridor"]:
        kinds.append("corridor changed")
    return kinds


def compare(baseline: dict[Key, dict], run: dict[Key, dict]) -> list[Difference]:
    diffs = []
    for key in sorted(baseline.keys() | run.keys()):
        b, r = baseline.get(key), run.get(key)
        kinds = classify(b, r)
        if kinds:
            corridor = (r or b)["corridor"]
            diffs.append(Difference(key, corridor, kinds, b, r))
    return diffs


def unbalanced(run: dict[Key, dict]) -> dict[tuple[str, str], Decimal]:
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for (ikey, leg, _acct), row in run.items():
        if leg == "rail":
            continue
        totals[(ikey, row["currency"])] += row["_amount"]
    return {k: v for k, v in sorted(totals.items()) if v != 0}


def _is_value_row(row: dict | None) -> bool:
    if row is None:
        return False
    return (row["leg"] == "fx_out" and row["account"].startswith("payout_clearing:")) or \
           (row["leg"] == "fee" and row["account"].startswith("revenue:"))


@dataclass
class CorridorSummary:
    payments: set[str] = field(default_factory=set)
    kinds: set[str] = field(default_factory=set)
    net: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))


def summarise(diffs: list[Difference]) -> dict[str, CorridorSummary]:
    out: dict[str, CorridorSummary] = defaultdict(CorridorSummary)
    for d in diffs:
        s = out[d.corridor]
        s.payments.add(d.key[0])
        s.kinds.update(d.kinds)
        if _is_value_row(d.base) or _is_value_row(d.run):
            row = d.run or d.base
            before = d.base["_amount"] if d.base else Decimal(0)
            after = d.run["_amount"] if d.run else Decimal(0)
            s.net[row["currency"]] += after - before
    return dict(sorted(out.items()))


def _fmt_net(net: dict[str, Decimal]) -> str:
    parts = [f"{v:+,.2f} {cur}" for cur, v in sorted(net.items(), key=lambda kv: (kv[0] == "USD", kv[0])) if v != 0]
    return ", ".join(parts) if parts else "0.00"


def _fmt_kinds(kinds: set[str]) -> str:
    return ", ".join(k for k in KIND_ORDER if k in kinds)


def render_report(baseline_rows: int, run: dict[Key, dict], diffs: list[Difference],
                  imbalance: dict[tuple[str, str], Decimal], sample: int = 5) -> str:
    lines: list[str] = []
    payments = {k[0] for k in run}
    if not diffs and not imbalance:
        lines.append("### Ledger gate: PASSED — ledger identical to baseline")
        lines.append("")
        lines.append(f"{len(run)} rows across {len(payments)} payments match the baseline exactly "
                     "(every amount, status, value date and rail transfer count).")
        lines.append("")
        lines.append("Invariant check: passed (every payment still balances)")
        return "\n".join(lines) + "\n"

    summary = summarise(diffs)
    if diffs:
        n = len(summary)
        lines.append(f"### Ledger gate: FAILED — {n} corridor{'s' if n != 1 else ''} changed")
    else:
        lines.append("### Ledger gate: FAILED — ledger invariant broken")
    lines.append("")
    if diffs:
        lines.append("| Corridor | Payments affected | Net change | Kind |")
        lines.append("|---|---|---|---|")
        for corridor, s in summary.items():
            lines.append(f"| {corridor} | {len(s.payments)} | {_fmt_net(s.net)} | {_fmt_kinds(s.kinds)} |")
        lines.append("")
        counts: dict[str, int] = defaultdict(int)
        for d in diffs:
            for k in d.kinds:
                counts[k] += 1
        lines.append("Differing rows: " + ", ".join(f"{counts[k]} {k}" for k in KIND_ORDER if counts.get(k)) +
                     f" (baseline {baseline_rows} rows, run {len(run)} rows).")
        lines.append("")

    if imbalance:
        bad_payments = {k[0] for k in imbalance}
        lines.append(f"Invariant check: **FAILED** — {len(bad_payments)} payment(s) do not sum to zero per currency")
        lines.append("")
        lines.append("| Payment | Currency | Imbalance |")
        lines.append("|---|---|---|")
        for (ikey, cur), v in list(imbalance.items())[:10]:
            lines.append(f"| {ikey} | {cur} | {v:+,.2f} |")
    else:
        lines.append("Invariant check: passed (every payment still balances)")
    lines.append("")

    if diffs:
        lines.append(f"<details><summary>First {min(sample, len(diffs))} differing rows</summary>")
        lines.append("")
        lines.append("| idempotency_key | leg | account | kind | baseline | run |")
        lines.append("|---|---|---|---|---|---|")
        for d in diffs[:sample]:
            lines.append(f"| {d.key[0]} | {d.key[1]} | {d.key[2]} | {', '.join(d.kinds)} | "
                         f"{_cell(d.base)} | {_cell(d.run)} |")
        lines.append("")
        lines.append("</details>")
        lines.append("")
        lines.append("All differing rows are attached to this run as an artifact (`diff_rows.csv`).")
        lines.append("If this change is intended, update `baseline/ledger_baseline.csv` in a PR labelled `update-baseline`.")
    return "\n".join(lines) + "\n"


def _cell(row: dict | None) -> str:
    if row is None:
        return "—"
    return f"{row['amount']} {row['currency']} · {row['status']} · {row['value_date']}"


def write_rows(diffs: list[Difference], path: Path) -> None:
    cols = ["idempotency_key", "leg", "account", "corridor", "kind",
            "baseline_amount", "run_amount", "baseline_status", "run_status",
            "baseline_value_date", "run_value_date"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(cols)
        for d in diffs:
            b, r = d.base or {}, d.run or {}
            w.writerow([*d.key, d.corridor, "; ".join(d.kinds),
                        b.get("amount", ""), r.get("amount", ""), b.get("status", ""), r.get("status", ""),
                        b.get("value_date", ""), r.get("value_date", "")])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--rows-out", type=Path, help="CSV of every differing row (default: diff_rows.csv beside the report)")
    ap.add_argument("--sample", type=int, default=5, help="differing rows to show inline in the report")
    args = ap.parse_args(argv)
    rows_out = args.rows_out or args.report.with_name("diff_rows.csv")

    try:
        baseline = load(args.baseline)
        run = load(args.run)
    except (LedgerFileError, OSError, UnicodeDecodeError, csv.Error) as exc:
        msg = f"### Ledger gate: ERROR — could not read ledger files\n\n`{exc}`\n"
        try:
            args.report.write_text(msg, encoding="utf-8")
        except OSError:
            pass
        print(f"diff_ledger: {exc}", file=sys.stderr)
        return 2

    diffs = compare(baseline, run)
    imbalance = unbalanced(run)
    args.report.write_text(render_report(len(baseline), run, diffs, imbalance, args.sample), encoding="utf-8")
    write_rows(diffs, rows_out)

    if diffs or imbalance:
        print(f"diff_ledger: FAILED — {len(diffs)} differing rows in {len({d.corridor for d in diffs})} corridor(s), "
              f"{len({k[0] for k in imbalance})} unbalanced payment(s). Report: {args.report}")
        return 1
    print(f"diff_ledger: PASSED — {len(run)} rows identical to baseline. Report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
