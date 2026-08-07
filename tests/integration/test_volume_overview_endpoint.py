"""Контракт GET /api/progress/volume-overview."""
from datetime import timedelta

import pytest

from api.services.models import AppUserProfile, UserCalendarDay
from api.services.volume.repository import utc_today

pytestmark = pytest.mark.asyncio


async def test_overview_returns_window_shape(
    client, auth_headers, db, test_user, active_block, seeded_plan
):
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    ))
    start = utc_today() - timedelta(days=2)
    for offset in range(6):
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=start + timedelta(days=offset),
            block_id=active_block.id, plan_id=seeded_plan.id, day_tag="push",
            micro_tag="medium", meso_tag="medium", microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=False, status="planned",
        ))
    await db.commit()

    resp = await client.get("/api/progress/volume-overview", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["shape_version"] == 2
    assert body["window"]["day"] == 3
    assert body["window"]["length"] == 6
    chest = body["muscles"]["chest"]
    assert chest["target"] == 12.0
    assert chest["mrv"] == 18
    assert chest["forecast"] >= chest["performed_direct"]


async def test_overview_includes_performed_sets_compat_shim(
    client, auth_headers, db, test_user, active_block, seeded_plan
):
    """P0-09 I4 (Important): сборки ДО shape v2 делали
    `setPerformedSets(data.performed_sets)`, а на рендере —
    `Object.values(performedSets)`. Поле убрали в v2 — на старом клиенте это
    TypeError на экране прогресса, а не деградация вида. Значение и есть
    та же эффективная сумма, что фигурирует в muscles[muscle] через
    performed_direct/performed_indirect."""
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget={"weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6}}},
    ))
    start = utc_today() - timedelta(days=2)
    for offset in range(6):
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=start + timedelta(days=offset),
            block_id=active_block.id, plan_id=seeded_plan.id, day_tag="push",
            micro_tag="medium", meso_tag="medium", microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=False, status="planned",
        ))
    await db.commit()

    resp = await client.get("/api/progress/volume-overview", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    assert "performed_sets" in body
    assert isinstance(body["performed_sets"], dict)
    assert "chest" in body["performed_sets"]
    chest = body["muscles"]["chest"]
    # MuscleContribution.effective = direct + indirect * INDIRECT_WEIGHT (0.5),
    # НЕ простая сумма (см. api/services/volume/measure.py).
    assert body["performed_sets"]["chest"] == pytest.approx(
        chest["performed_direct"] + chest["performed_indirect"] * 0.5
    )


async def test_overview_without_calendar_returns_empty_window(
    client, auth_headers, db, test_user
):
    # Нет блока и календаря — контур молчит, но эндпоинт не падает.
    resp = await client.get("/api/progress/volume-overview", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["shape_version"] == 2
    assert body["window"] is None
    assert body["muscles"] == {}


async def test_overview_carries_raw_budget_from_profile(
    client, auth_headers, db, test_user, active_block, seeded_plan
):
    """Критичная находка ревью P0-09 Task 16: редактор бюджета (VolumeMatrixWidget)
    правит сам volume_budget, а окно отдаёт только производные величины по
    мышцам. Без сырого budget в ответе редактору нечем было наполниться —
    он молча оставался мёртвым (currentBudget=null). budget должен прийти
    как есть из профиля на обоих путях возврата (с окном и без)."""
    raw_budget = {
        "version": "1",
        "meta": {
            "focus_muscles": ["chest"],
            "distribution_type": "even",
            "total_weekly_sets": 12,
        },
        "constraints": {
            "systemic_cap_per_week": 60,
            "max_sets_per_session_per_muscle": 6,
        },
        "weekly_targets": {"chest": {"target_sets": 12, "min_floor": 6, "is_focus": True}},
    }
    db.add(AppUserProfile(
        app_user_id=test_user.id, experience_level="intermediate",
        volume_budget=raw_budget,
    ))
    start = utc_today() - timedelta(days=2)
    for offset in range(6):
        db.add(UserCalendarDay(
            app_user_id=test_user.id, target_date=start + timedelta(days=offset),
            block_id=active_block.id, plan_id=seeded_plan.id, day_tag="push",
            micro_tag="medium", meso_tag="medium", microcycle_day_number=offset + 1,
            is_rest_day=False, is_blackout=False, status="planned",
        ))
    await db.commit()

    resp = await client.get("/api/progress/volume-overview", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"] is not None
    assert body["budget"] == raw_budget


async def test_overview_budget_is_none_without_profile(
    client, auth_headers, db, test_user
):
    # Нет профиля — редактору нечего показывать, budget честно None
    # (не {} — {} читался бы редактором как «валидный пустой бюджет»).
    resp = await client.get("/api/progress/volume-overview", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"] is None
    assert body["budget"] is None
