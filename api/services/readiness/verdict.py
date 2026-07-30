"""Свёртка чек-ина в вердикт и его резолв на конкретное упражнение.

Чистые функции: ни одного обращения к БД. Это же делает их портируемыми
на устройство (спека §9) — офлайн-накладка считает ровно то же самое.
"""

from __future__ import annotations

from typing import Optional

from api.services.readiness import params
from api.services.readiness.types import (
    CheckinSignals,
    ExerciseReadiness,
    ExerciseTarget,
    MuscleFlag,
    ReadinessVerdict,
)


def _global_level(signals: CheckinSignals) -> str:
    """Один плохой из {сон, стресс} -> caution, оба -> limit."""
    bad = 0
    if signals.sleep is not None and signals.sleep <= params.SLEEP_BAD_AT_OR_BELOW:
        bad += 1
    if signals.stress is not None and signals.stress >= params.STRESS_BAD_AT_OR_ABOVE:
        bad += 1
    if bad >= 2:
        return params.LEVEL_LIMIT
    if bad == 1:
        return params.LEVEL_CAUTION
    return params.LEVEL_OK


def _muscle_flags(signals: CheckinSignals) -> tuple[MuscleFlag, ...]:
    flags: list[MuscleFlag] = []
    for muscle, value in sorted((signals.soreness or {}).items()):
        if value is None:
            continue
        if value >= params.SORENESS_LIMIT:
            flags.append(MuscleFlag(muscle, params.LEVEL_LIMIT, "soreness_limit"))
        elif value >= params.SORENESS_CAUTION:
            flags.append(MuscleFlag(muscle, params.LEVEL_CAUTION, "soreness_caution"))
    return tuple(flags)


def build_verdict(signals: CheckinSignals) -> Optional[ReadinessVerdict]:
    """Вердикт из ответов чек-ина. None — не ответили вообще ни на что.

    None критичен: он означает "движок ведёт себя как P0-06", и именно он
    возвращается, когда пользователь пропустил чек-ин или выключил его
    в настройках.
    """
    answered = (
        signals.sleep is not None
        or signals.stress is not None
        or bool(signals.soreness)
        or bool(signals.pain)
    )
    if not answered:
        return None

    level = _global_level(signals)
    reason_code = {
        params.LEVEL_OK: "readiness_ok",
        params.LEVEL_CAUTION: "readiness_caution",
        params.LEVEL_LIMIT: "readiness_limit",
    }[level]

    pain_places = tuple(
        place
        for place, value in sorted((signals.pain or {}).items())
        if value is not None and value > params.PAIN_MIN
    )

    completeness = (
        params.COMPLETENESS_FULL
        if signals.sleep is not None and signals.stress is not None
        else params.COMPLETENESS_PARTIAL
    )

    return ReadinessVerdict(
        level=level,
        reason_code=reason_code,
        reason_text=params.VERDICT_REASON_TEXTS[reason_code],
        muscle_flags=_muscle_flags(signals),
        pain_places=pain_places,
        completeness=completeness,
        observed_at=signals.observed_at,
    )


def _pain_hit(place: str, target: ExerciseTarget) -> Optional[str]:
    """Как боль в этом месте задевает упражнение: 'main' | 'secondary' | None."""
    secondary = set(target.secondary_muscles or ())

    if place not in params.JOINT_KEYS:
        # Боль в самой мышце: паттерн движения ни при чём.
        if target.main_muscle and place == target.main_muscle:
            return "main"
        if place in secondary:
            return "secondary"
        return None

    muscles, actions = params.JOINT_IMPACT[place]
    # unknown снимает требование по паттерну: ошибаемся в сторону осторожности.
    if target.action != "unknown" and target.action not in actions:
        return None
    if target.main_muscle and target.main_muscle in muscles:
        return "main"
    if secondary & muscles:
        return "secondary"
    return None


def level_for_exercise(
    verdict: Optional[ReadinessVerdict], target: ExerciseTarget
) -> ExerciseReadiness:
    """Уровень, разрешённый для одного упражнения.

    max(глобальный, крепатура, боль); вспомогательная мышца понижает
    уровень на ступень. Боль считается ПОСЛЕДНЕЙ и при равенстве
    перебивает крепатуру: пользователю информативнее услышать про боль.
    """
    if verdict is None:
        return ExerciseReadiness()

    level = verdict.level
    source = params.SOURCE_GLOBAL if level != params.LEVEL_OK else None
    secondary = set(target.secondary_muscles or ())

    for flag in verdict.muscle_flags:
        if target.main_muscle and flag.muscle == target.main_muscle:
            candidate = flag.level
        elif flag.muscle in secondary:
            candidate = params.downgrade(flag.level)
        else:
            continue
        if params.LEVEL_ORDER[candidate] > params.LEVEL_ORDER[level]:
            level, source = candidate, params.SOURCE_SORENESS

    pain_level = params.LEVEL_OK
    for place in verdict.pain_places:
        hit = _pain_hit(place, target)
        if hit is None:
            continue
        candidate = params.LEVEL_LIMIT if hit == "main" else params.LEVEL_CAUTION
        pain_level = params.higher(pain_level, candidate)

    # >= намеренно: при РАВЕНСТВЕ уровней источником становится боль —
    # пользователю она информативнее крепатуры. Строго больше здесь дало бы
    # "крепатура в квадрицепсах" на упражнении, которое отдаётся в колено.
    if (
        pain_level != params.LEVEL_OK
        and params.LEVEL_ORDER[pain_level] >= params.LEVEL_ORDER[level]
    ):
        level, source = pain_level, params.SOURCE_PAIN

    if level == params.LEVEL_OK:
        return ExerciseReadiness()
    return ExerciseReadiness(level=level, source=source)
