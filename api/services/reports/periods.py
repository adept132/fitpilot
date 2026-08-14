"""Границы отчётных периодов.

Чистый модуль без БД: отчёт обязан одинаково резать календарь и в фоновом
воркере, и в запросе клиента, поэтому логика границ живёт отдельно от
источника даты.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

PERIOD_WEEK = "week"
PERIOD_MONTH = "month"
PERIOD_YEAR = "year"
PERIOD_TYPES = (PERIOD_WEEK, PERIOD_MONTH, PERIOD_YEAR)

# [КОНФИГ] Сколько закрытых периодов догоняем при визите. Вернувшийся через
# полгода не должен получить пачку из двадцати шести отчётов — тот же приём,
# что MAX_BACKLOG_WINDOWS в api/services/volume/service.py.
CATCHUP_DEPTH = {PERIOD_WEEK: 4, PERIOD_MONTH: 3, PERIOD_YEAR: 1}


def period_bounds(period_type: str, anchor: date) -> tuple[date, date]:
    """Границы периода, в который попадает anchor. Обе даты включительно."""
    if period_type == PERIOD_WEEK:
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    if period_type == PERIOD_MONTH:
        last_day = calendar.monthrange(anchor.year, anchor.month)[1]
        return date(anchor.year, anchor.month, 1), date(anchor.year, anchor.month, last_day)
    if period_type == PERIOD_YEAR:
        return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
    raise ValueError(f"Неизвестный тип периода: {period_type}")


def _previous_anchor(period_type: str, start: date) -> date:
    """Любая дата внутри периода, предшествующего тому, что начинается start."""
    return start - timedelta(days=1)


def closed_periods(period_type: str, local_date: date) -> list[tuple[date, date]]:
    """Закрытые периоды от свежего к старому, не глубже CATCHUP_DEPTH.

    Текущий период не возвращается никогда: отчёт строится только по
    полностью прожитому отрезку.
    """
    if period_type not in PERIOD_TYPES:
        raise ValueError(f"Неизвестный тип периода: {period_type}")

    current_start, _ = period_bounds(period_type, local_date)
    result: list[tuple[date, date]] = []
    anchor = _previous_anchor(period_type, current_start)
    for _ in range(CATCHUP_DEPTH[period_type]):
        bounds = period_bounds(period_type, anchor)
        result.append(bounds)
        anchor = _previous_anchor(period_type, bounds[0])
    return result
