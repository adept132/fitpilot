"""Подрезка объёма по вердикту готовности (P0-07 §7.3).

Вторая ось адаптации: вес удерживает apply_readiness_cap, число подходов
режет эта функция. Трим ТОЛЬКО вычитает — симметричный вариант "при
отличной готовности добавить подход" отклонён сознательно: он ломает
асимметрию P0-06 "растём по выполнению, а не по самоощущению".
"""

from __future__ import annotations

from dataclasses import replace

from api.services.progression import params
from api.services.progression.types import Prescription, SchemeContext

# Источник уровня -> причина объёма. Держится отдельно от причины веса:
# иначе пользователь с крепатурой перестал бы видеть, что у него вдобавок
# плато.
_VOLUME_REASON_BY_SOURCE: dict[str, str] = {
    "pain": "pain_volume",
    "soreness": "soreness_volume",
    "global": "readiness_volume",
}


def apply_volume_trim(
    prescription: Prescription, ctx: SchemeContext
) -> Prescription:
    """Снять подходы с конца по уровню готовности упражнения."""
    if not prescription.sets:
        return prescription
    if ctx.readiness_source is None:
        return prescription

    wanted = params.VOLUME_TRIM_BY_LEVEL.get(ctx.readiness_level, 0)
    if wanted <= 0:
        return prescription

    sets = list(prescription.sets)

    # Пол: ниже двух рабочих подходов упражнение перестаёт что-либо
    # стимулировать — честнее предложить пропустить его целиком, чем
    # оставить огрызок.
    room = len(sets) - params.VOLUME_MIN_SETS
    # AMRAP неприкосновенен: у percent_1rm это единственный вход,
    # обновляющий working_e1rm (P0-06 §6.4). Срезать его — обезглавить
    # схему: она перестанет получать обратную связь и застынет навсегда.
    removable = sum(1 for s in sets if s.kind != "amrap")

    to_remove = min(wanted, room, removable)
    if to_remove <= 0:
        return prescription

    for _ in range(to_remove):
        for index in range(len(sets) - 1, -1, -1):
            if sets[index].kind != "amrap":
                sets.pop(index)
                break

    # Перенумеровать обязательно: evaluate() сопоставляет факт с
    # предписанием по set_number, и дыра в нумерации сломала бы оценку
    # следующей сессии.
    renumbered = tuple(
        replace(s, set_number=i) for i, s in enumerate(sets, start=1)
    )

    reason = _VOLUME_REASON_BY_SOURCE[ctx.readiness_source]
    return replace(
        prescription,
        sets=renumbered,
        volume_delta=-to_remove,
        volume_reason_code=reason,
        volume_reason_text=params.VOLUME_REASON_TEXTS[reason],
    )
