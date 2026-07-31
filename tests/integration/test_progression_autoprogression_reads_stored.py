"""Read-only эндпоинт автопрогрессии обязан отдавать СОХРАНЁННОЕ предписание.

Пересчёт с нуля не знает про чек-ин готовности (P0-07): вердикт приходит
только в момент создания сессии. Если этот путь пересчитывает, пользователь
читает в карточке одно число, а целью, записанной в БД, оказывается другое —
вес без потолка и причина без учёта чек-ина.
"""

import pytest

from api.services.autoprogression import compute_autoprogression
from api.services.progression.types import Prescription, SetPrescription


def capped_prescription(weight=100.0) -> Prescription:
    """Как после субъективного потолка и подрезки объёма: вес удержан,
    подходов осталось два, причины проставлены."""
    return Prescription(
        scheme="double",
        sets=(
            SetPrescription(1, weight, 8, 12, 2, "normal"),
            SetPrescription(2, weight, 8, 12, 2, "normal"),
        ),
        reason_code="readiness_limit",
        reason_text="Сон и стресс не в порядке — вес не повышаем.",
        volume_delta=-2,
        volume_reason_code="readiness_volume",
        volume_reason_text="Восстановление неполное — сократили число подходов.",
    )


@pytest.mark.asyncio
async def test_returns_stored_prescription_instead_of_recomputing(
    db_session, app_user, session_exercise
):
    stored = capped_prescription()
    session_exercise.prescription = stored.to_dict()
    await db_session.flush()

    data = await compute_autoprogression(
        db_session, session_exercise, app_user.id, "intermediate", {}
    )

    # Число в карточке — это удержанный вес, а не пересчёт.
    assert data["target_weight"] == pytest.approx(100.0)
    assert data["reason_code"] == "readiness_limit"
    assert data["prescription"]["volume_reason_code"] == "readiness_volume"
    assert len(data["prescription"]["sets"]) == 2


@pytest.mark.asyncio
async def test_recomputes_when_nothing_is_stored(
    db_session, app_user, session_exercise
):
    # Предписания нет (свободное добавление упражнения) — считать надо.
    assert not session_exercise.prescription

    data = await compute_autoprogression(
        db_session, session_exercise, app_user.id, "intermediate", {}
    )
    assert "has_basis" in data
    assert data["reason_code"]


@pytest.mark.asyncio
async def test_pickers_still_recompute_over_a_stored_prescription(
    db_session, app_user, session_exercise
):
    """Пикеры свободной тренировки — вопрос «а если», а не показ цели.

    Единственный случай, где пересчёт поверх сохранённого предписания
    осмыслен: пользователь сам крутит повторы и усилие.
    """
    session_exercise.prescription = capped_prescription().to_dict()
    await db_session.flush()

    data = await compute_autoprogression(
        db_session,
        session_exercise,
        app_user.id,
        "intermediate",
        {},
        target_reps=5,
    )

    # Не отдали сохранённое дословно — значит пересчитали.
    assert data["reason_code"] != "readiness_limit"
