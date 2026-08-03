"""Чистые операции над снимком фаз блока.

Главный инвариант: phase_number существующих фаз НИКОГДА не меняется.
Завершённые сессии хранят его в WorkoutSession.mesocycle_phase, и любая
перенумерация задним числом переклеила бы их на чужую фазу — то есть выдала
бы deload за тяжёлую неделю в уже закрытой истории.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Sequence

from api.services.periodization import params
from api.services.periodization.types import PhaseSnapshot

Phases = tuple[PhaseSnapshot, ...]


def to_json(phases: Sequence[PhaseSnapshot]) -> list[dict]:
    return [
        {
            "phase_number": p.phase_number,
            "name": p.name,
            "effort_tier": p.effort_tier,
            "length_days": p.length_days,
        }
        for p in phases
    ]


def from_json(raw: Sequence[dict]) -> Phases:
    return tuple(
        PhaseSnapshot(
            phase_number=int(item["phase_number"]),
            name=str(item["name"]),
            effort_tier=str(item["effort_tier"]),
            length_days=int(item["length_days"]),
        )
        for item in (raw or [])
    )


def tier_for(phases: Sequence[PhaseSnapshot], phase_number: int) -> Optional[str]:
    for phase in phases:
        if phase.phase_number == phase_number:
            return phase.effort_tier
    return None


def insert_deload(
    phases: Sequence[PhaseSnapshot],
    after_phase_number: int,
    length_days: int,
    name: str = "Разгрузка",
) -> Phases:
    """Вставить фазу разгрузки сразу после указанной.

    Номер новой фазы — max + 1, а не «номер соседа + 1»: номера не
    перенумеровываются (см. докстринг модуля), поэтому порядок номеров и
    порядок фаз в списке расходятся, и это нормально.
    """
    ordered = tuple(phases)
    index = next(
        (i for i, p in enumerate(ordered) if p.phase_number == after_phase_number), None
    )
    if index is None:
        raise ValueError(f"Фаза {after_phase_number} не найдена в блоке")

    fresh_number = max(p.phase_number for p in ordered) + 1
    deload = PhaseSnapshot(
        phase_number=fresh_number,
        name=name,
        effort_tier=params.DELOAD_TIER,
        length_days=length_days,
    )
    return ordered[: index + 1] + (deload,) + ordered[index + 1:]


def postpone_deload(phases: Sequence[PhaseSnapshot], extra_days: int) -> Phases:
    """Сдвинуть ближайшую разгрузку вперёд, продлив предыдущую фазу.

    Разгрузки в блоке нет или она первая — делать нечего: продлевать нечего,
    и выдумывать фазу перед ней мы не имеем права.
    """
    ordered = tuple(phases)
    index = next(
        (i for i, p in enumerate(ordered) if p.effort_tier == params.DELOAD_TIER), None
    )
    if index is None or index == 0:
        return ordered

    previous = ordered[index - 1]
    extended = replace(previous, length_days=previous.length_days + extra_days)
    return ordered[: index - 1] + (extended,) + ordered[index:]


def split_after(
    phases: Sequence[PhaseSnapshot], phase_number: int
) -> tuple[Phases, Phases]:
    """Разрезать список на «до включительно» и «после».

    Используется при закрытии блока досрочной разгрузкой: хвост переезжает
    в следующий блок.
    """
    ordered = tuple(phases)
    index = next(
        (i for i, p in enumerate(ordered) if p.phase_number == phase_number), None
    )
    if index is None:
        raise ValueError(f"Фаза {phase_number} не найдена в блоке")
    return ordered[: index + 1], ordered[index + 1:]
