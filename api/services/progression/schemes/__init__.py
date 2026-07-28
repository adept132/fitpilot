"""Реестр схем прогрессии.

Схемы умеют только повышать нагрузку. Снижение — общий слой reduction.py.
"""

from __future__ import annotations

from typing import Callable

from api.services.progression import params
from api.services.progression.schemes import e1rm_factor
from api.services.progression.types import Prescription, SchemeContext

SCHEMES: dict[str, Callable[[SchemeContext], Prescription]] = {
    params.SCHEME_E1RM_FACTOR: e1rm_factor.plan,
}


def plan_with(name: str, ctx: SchemeContext) -> Prescription:
    """Построить предписание указанной схемой. Неизвестное имя — KeyError."""
    return SCHEMES[name](ctx)
