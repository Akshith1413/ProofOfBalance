"""Load the fixed FX rates file (mounted from a ConfigMap; never a live API).

Format::

    fee_bps: "20"
    corridors:
      USD-INR: "83.25"

Values must be strings (or ints). A YAML float such as ``83.25`` without
quotes is rejected, because YAML would hand us a binary float.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from .money import MoneyError, destination_currency, to_decimal


class RatesError(ValueError):
    pass


@dataclass(frozen=True)
class Rates:
    fee_bps: int
    corridors: dict[str, Decimal]

    def rate(self, corridor: str) -> Decimal:
        try:
            return self.corridors[corridor]
        except KeyError:
            raise RatesError(f"unknown corridor {corridor!r}; known: {sorted(self.corridors)}") from None


def _strict_decimal(value: object, where: str) -> Decimal:
    if isinstance(value, float):
        raise RatesError(f"{where}: {value!r} is a YAML float; quote it, e.g. \"{value}\"")
    try:
        return to_decimal(value)
    except (TypeError, MoneyError) as exc:
        raise RatesError(f"{where}: {exc}") from exc


def parse_rates(data: object) -> Rates:
    if not isinstance(data, dict):
        raise RatesError("rates file must be a mapping with 'fee_bps' and 'corridors'")
    unknown = set(data) - {"fee_bps", "corridors"}
    if unknown:
        raise RatesError(f"unknown keys in rates file: {sorted(unknown)}")

    fee = _strict_decimal(data.get("fee_bps"), "fee_bps")
    if fee != fee.to_integral_value() or not 0 <= fee < 10000:
        raise RatesError("fee_bps must be a whole number in [0, 10000)")

    corridors_raw = data.get("corridors")
    if not isinstance(corridors_raw, dict) or not corridors_raw:
        raise RatesError("'corridors' must be a non-empty mapping")
    corridors: dict[str, Decimal] = {}
    for name, raw in corridors_raw.items():
        try:
            destination_currency(str(name))
        except MoneyError as exc:
            raise RatesError(str(exc)) from exc
        rate = _strict_decimal(raw, f"corridors.{name}")
        if rate <= 0:
            raise RatesError(f"corridors.{name}: rate must be > 0")
        if rate.as_tuple().exponent < -8:
            raise RatesError(f"corridors.{name}: at most 8 decimal places (NUMERIC(20,8))")
        corridors[str(name)] = rate
    return Rates(fee_bps=int(fee), corridors=corridors)


def load_rates(path: str | Path) -> Rates:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise RatesError(f"cannot read rates file {p}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RatesError(f"rates file {p} is not valid YAML: {exc}") from exc
    return parse_rates(data)
