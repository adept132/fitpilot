"""Координата внутри блока. Чистая арифметика, БД не участвует."""
from datetime import date

from api.services.periodization.position import position
from api.services.periodization.types import BlockState, PhaseSnapshot

START = date(2026, 8, 3)


def _block(*tiers: str, length: int = 7) -> BlockState:
    return BlockState(
        block_index=1,
        phases=tuple(
            PhaseSnapshot(phase_number=i + 1, name=t, effort_tier=t, length_days=length)
            for i, t in enumerate(tiers)
        ),
        start_date=START,
        microcycle_length=length,
    )


def test_first_day_is_first_phase():
    pos = position(_block("easy", "medium", "deload"), START)
    assert pos.phase_number == 1
    assert pos.effort_tier == "easy"
    assert pos.phase_ordinal == 1
    assert pos.day_in_block == 1
    assert pos.is_complete is False


def test_day_boundary_moves_to_next_phase():
    block = _block("easy", "medium", "deload")
    assert position(block, date(2026, 8, 9)).phase_number == 1   # 7-й день
    assert position(block, date(2026, 8, 10)).phase_number == 2  # 8-й день


def test_days_to_deload_counts_from_today():
    block = _block("easy", "medium", "deload")
    # Разгрузка начинается на 15-й день блока, сегодня первый.
    assert position(block, START).days_to_deload == 14


def test_days_to_deload_is_zero_inside_deload():
    block = _block("easy", "deload")
    assert position(block, date(2026, 8, 10)).days_to_deload == 0


def test_no_deload_in_block_gives_none():
    assert position(_block("easy", "medium"), START).days_to_deload is None


def test_block_is_complete_past_last_phase():
    block = _block("easy", "medium")
    assert position(block, date(2026, 8, 16)).is_complete is False  # 14-й день
    assert position(block, date(2026, 8, 17)).is_complete is True   # 15-й


def test_completed_block_reports_last_phase():
    """После конца блока координата не уезжает в никуда: держим последнюю фазу."""
    pos = position(_block("easy", "medium"), date(2026, 8, 20))
    assert pos.phase_number == 2
    assert pos.is_last_phase is True


def test_stable_phase_numbers_are_respected():
    """Порядок задаёт список, а не phase_number: вставленная разгрузка имеет
    максимальный номер, но стоит в середине."""
    block = BlockState(
        block_index=1,
        phases=(
            PhaseSnapshot(1, "Накопление", "medium", 7),
            PhaseSnapshot(3, "Разгрузка", "deload", 7),
            PhaseSnapshot(2, "Тяжёлая", "prefailure", 7),
        ),
        start_date=START,
        microcycle_length=7,
    )
    pos = position(block, date(2026, 8, 10))
    assert pos.phase_number == 3
    assert pos.effort_tier == "deload"
    assert pos.phase_ordinal == 2


def test_date_before_start_clamps_to_first_day():
    """Дата раньше start_date клампится к первому дню блока."""
    pos = position(_block("easy", "medium", "deload"), date(2026, 8, 1))
    assert pos.day_in_block == 1
    assert pos.phase_ordinal == 1
    assert pos.is_complete is False


def test_next_phase_is_deload_true_when_following_phase_in_list_is_deload():
    block = _block("easy", "medium", "deload")
    pos = position(block, date(2026, 8, 10))  # 8-й день — фаза "medium"
    assert pos.effort_tier == "medium"
    assert pos.next_phase_is_deload is True


def test_next_phase_is_deload_false_when_following_phase_is_not_deload():
    block = _block("easy", "medium", "deload")
    pos = position(block, START)  # 1-й день — фаза "easy", дальше "medium"
    assert pos.effort_tier == "easy"
    assert pos.next_phase_is_deload is False


def test_next_phase_is_deload_false_for_last_phase():
    block = _block("easy", "medium")
    pos = position(block, date(2026, 8, 10))  # 8-й день — последняя фаза
    assert pos.is_last_phase is True
    assert pos.next_phase_is_deload is False


def test_next_phase_is_deload_follows_list_order_not_phase_number():
    """Порядок в списке решает, а не phase_number соседа."""
    block = BlockState(
        block_index=1,
        phases=(
            PhaseSnapshot(1, "Накопление", "medium", 7),
            PhaseSnapshot(3, "Разгрузка", "deload", 7),
            PhaseSnapshot(2, "Тяжёлая", "prefailure", 7),
        ),
        start_date=START,
        microcycle_length=7,
    )
    pos = position(block, START)  # 1-й день — "Накопление", дальше по списку "Разгрузка"
    assert pos.phase_number == 1
    assert pos.next_phase_is_deload is True

    pos_deload = position(block, date(2026, 8, 10))  # сама "Разгрузка"
    assert pos_deload.phase_number == 3
    assert pos_deload.effort_tier == "deload"
    # дальше по списку "Тяжёлая", не разгрузка
    assert pos_deload.next_phase_is_deload is False
