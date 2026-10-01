"""受控单位换算：只处理同量纲倍率，保留原值与换算依据。"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from jiadun.core.engine.money import NotANumberError, to_decimal

D = Decimal
_ALIASES = {
    '吨': 't', '千克': 'kg', '公斤': 'kg',
    '平方米': 'm2', '平米': 'm2', '立方米': 'm3', '立米': 'm3',
    '米': 'm', '延长米': 'm',
}
_UNITS = {'t': ('t', D(1)), 'kg': ('t', D('0.001')), 'g': ('t', D('0.000001')),
          'm': ('m', D(1)), 'cm': ('m', D('0.01')), 'mm': ('m', D('0.001')),
          'm2': ('m2', D(1)), 'm3': ('m3', D(1))}


def unit_basis(value: str | None) -> tuple[str, Decimal] | None:
    raw = ''.join(unicodedata.normalize('NFKC', value or '').split()).lower().replace('^', '')
    if not raw:
        return None
    raw = _ALIASES.get(raw, raw)
    if raw in _UNITS:
        return _UNITS[raw]
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(m2|m3|m|kg|t)', raw)
    if match:
        factor = D(match[1])
        if factor <= 0:
            return None
        unit, scale = _UNITS[match[2]]
        return unit, factor * scale
    # 未知单位只允许原文一致；不推断“项/套/个”或措施计量口径。
    return raw, D(1)


def feature_key(value: str | None) -> str:
    return ''.join(unicodedata.normalize('NFKC', value or '').split()).casefold()


@dataclass(frozen=True)
class QuantityResult:
    unit: str
    quantity: Decimal | None
    status: str
    conversions: tuple[dict, ...]


def normalize_quantities(rows) -> QuantityResult:
    bases = [unit_basis(row['unit']) for row in rows]
    features = {feature_key(row['feature']) for row in rows}
    known = {basis[0] for basis in bases if basis is not None}
    conversions = []
    total = None
    missing = False
    for row, basis in zip(rows, bases, strict=True):
        try:
            quantity = to_decimal(row['quantity'])
        except (NotANumberError, TypeError, ValueError):
            quantity = None
        standard = quantity * basis[1] if quantity is not None and basis is not None else None
        conversions.append({'line_item_id': int(row['id']), 'original_quantity': row['quantity'],
                            'original_unit': row['unit'], 'standard_unit': basis[0] if basis else '',
                            'factor': str(basis[1]) if basis else None,
                            'standard_quantity': str(standard) if standard is not None else None,
                            'basis': '同量纲固定倍率' if basis else '单位待补'})
        if standard is None:
            missing = True
        else:
            total = standard if total is None else total + standard
    if len(known) > 1 or len(features) > 1:
        return QuantityResult('', None, 'incomparable', tuple(conversions))
    unit = next(iter(known), '')
    if missing or not rows:
        return QuantityResult(unit, None, 'incomplete', tuple(conversions))
    return QuantityResult(unit, total, 'ok', tuple(conversions))
