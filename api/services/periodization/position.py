"""Где мы внутри блока. Чистая арифметика по снимку фаз."""

from __future__ import annotations

from datetime import date
from typing import Optional

from api.services.periodization import params
from api.services.periodization.types import BlockPosition, BlockState


def total_length(block: BlockState) -> int:
    return sum(p.length_days for p in block.phases)


def position(block: BlockState, today: date) -> BlockPosition:
    """Координата на дату. Блок без фаз недопустим — это ошибка вызывающего.

    После планового конца блока координата не уезжает в никуда: держим
    последнюю фазу и поднимаем is_complete. Иначе UI пришлось бы учить
    отдельному состоянию «блок кончился, но ещё не закрыт», а закрытие
    требует решения пользователя и может произойти сильно позже.

    Дата раньше start_date клампится к первому дню блока (day_in_block == 1).
    Это осмысленно — планировать по уже идущему блоку, используя текущую координату.
    """
    if not block.phases:
        raise ValueError("Блок без фаз: координату считать не по чему")

    day_in_block = (today - block.start_date).days + 1
    if day_in_block < 1:
        day_in_block = 1

    total = total_length(block)
    is_complete = day_in_block > total

    # Ищем фазу, в которую попадает день, идя по СПИСКУ (порядок задаёт список).
    cursor = 0
    current_index = len(block.phases) - 1
    phase_start_day = total - block.phases[-1].length_days + 1
    for index, phase in enumerate(block.phases):
        start = cursor + 1
        end = cursor + phase.length_days
        if day_in_block <= end:
            current_index = index
            phase_start_day = start
            break
        cursor = end

    current = block.phases[current_index]

    return BlockPosition(
        phase_number=current.phase_number,
        effort_tier=current.effort_tier,
        phase_ordinal=current_index + 1,
        phases_total=len(block.phases),
        day_in_block=day_in_block,
        days_to_deload=_days_to_deload(block, day_in_block, current_index, phase_start_day),
        is_last_phase=current_index == len(block.phases) - 1,
        is_complete=is_complete,
    )


def _days_to_deload(
    block: BlockState, day_in_block: int, current_index: int, phase_start_day: int
) -> Optional[int]:
    """Дней до начала ближайшей разгрузки. 0 — она уже идёт, None — её нет."""
    if block.phases[current_index].effort_tier == params.DELOAD_TIER:
        return 0

    day_cursor = phase_start_day + block.phases[current_index].length_days
    for phase in block.phases[current_index + 1:]:
        if phase.effort_tier == params.DELOAD_TIER:
            return max(0, day_cursor - day_in_block)
        day_cursor += phase.length_days
    return None
