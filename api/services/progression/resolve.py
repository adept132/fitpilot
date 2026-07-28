"""Выбор схемы прогрессии: ручной override -> фаза мезоцикла -> эвристика.

percent_1rm эвристикой не выбирается никогда: он осмыслен только внутри
осознанного силового блока с AMRAP-тестами (спека P0-06 §7).
"""

from __future__ import annotations

from typing import Any, Optional

from api.services import equipment as equip
from api.services.progression import params
from api.services.progression.types import SchemeContext

KNOWN_SCHEMES = frozenset(
    {
        params.SCHEME_E1RM_FACTOR,
        params.SCHEME_DOUBLE,
        params.SCHEME_FIXED_INCREMENT,
        params.SCHEME_PERCENT_1RM,
    }
)

_STRENGTH_PHASES = frozenset({"prefailure", "failure"})
_BARBELL_LIKE = frozenset({equip.BARBELL, equip.SMITH})


def override_for(settings: Optional[dict[str, Any]], exercise_id: int) -> Optional[str]:
    """Ручной выбор схемы из настроек профиля.

    Живёт в settings, а не в кэш-таблице состояния: кэш производный и чинится
    пересчётом, а пользовательский выбор пересчётом чиниться не должен.
    """
    if not settings:
        return None
    overrides = (settings.get("progression") or {}).get("overrides") or {}
    raw = overrides.get(str(exercise_id)) or overrides.get(exercise_id)
    return raw if raw in KNOWN_SCHEMES else None


def _has_prescription_history(ctx: SchemeContext) -> bool:
    return any(s.prescription is not None for s in ctx.history.sessions)


def resolve_scheme(ctx: SchemeContext, override: Optional[str] = None) -> str:
    """Имя схемы для этого упражнения в этом контексте."""
    # Слой 1: ручной выбор пользователя.
    if override in KNOWN_SCHEMES:
        return override

    # Бутстрап: сравнивать факт не с чем, сохраняем прежнее поведение.
    if not _has_prescription_history(ctx):
        return params.SCHEME_E1RM_FACTOR

    # Слой 2: силовая фаза мезоцикла на тяжёлой базе.
    if ctx.phase_effort_tier in _STRENGTH_PHASES and ctx.fatigue_tier == 1:
        return params.SCHEME_PERCENT_1RM

    # Слой 3: эвристика.
    if ctx.rep_min >= ctx.rep_max:
        # Нулевой зазор: расти внутри диапазона некуда.
        return params.SCHEME_FIXED_INCREMENT

    normalized = set(equip.normalize_equipment_list(list(ctx.equipment)))
    is_barbell_like = bool(normalized & _BARBELL_LIKE)
    is_beginner = (ctx.experience_level or "").strip().lower() == "beginner"

    if ctx.fatigue_tier == 1 and is_barbell_like and is_beginner:
        return params.SCHEME_FIXED_INCREMENT

    return params.SCHEME_DOUBLE
