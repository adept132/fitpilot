"""Существующий пользователь со сплитом, но без структуры, чинится сам (P1-03 ч.1, §5.5)."""
import pytest
from sqlalchemy import select

from api.seed_splits import ensure_system_splits
from api.services.models import (
    AppUserMesocycle, AppUserMicrocycle, Mesocycle, SplitBlueprint, UserSplit,
)
from api.services.structure.bootstrap import ensure_structure
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _activate_split(user_id: int) -> None:
    async with SessionLocal() as db:
        await ensure_system_splits(db)
        blueprint = (await db.execute(
            select(SplitBlueprint).where(
                SplitBlueprint.is_system.is_(True),
                SplitBlueprint.name == "Upper / Lower (4 Дня)",
            )
        )).scalars().first()
        db.add(UserSplit(
            app_user_id=user_id, blueprint_id=blueprint.id,
            is_active=True, current_day=1,
        ))
        await db.commit()


async def test_opening_the_workout_center_fills_missing_structure(
    client, auth_headers, test_user,
):
    await _activate_split(test_user.id)

    r = await client.get("/workout-center/context", headers=auth_headers)
    assert r.status_code == 200, r.text

    async with SessionLocal() as db:
        active_meso = (await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == test_user.id,
                AppUserMesocycle.is_active.is_(True),
            )
        )).scalars().first()
        micros = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id
            )
        )).scalars().all()

    assert active_meso is not None
    assert len(micros) == 5
    assert any(m.is_active for m in micros)


async def test_user_with_existing_structure_is_not_touched(
    client, auth_headers, test_user,
):
    await _activate_split(test_user.id)

    # Сетап должен довести пользователя до состояния "структура уже полная":
    # активный мезоцикл И активный микроцикл ОДНОВРЕМЕННО. Иначе ранний выход
    # ensure_structure (_has_active_structure требует ОБОИХ активных) не
    # срабатывает, функция создаёт пять мезоциклов и активирует дефолтный —
    # а тест, проверявший только микроциклы, этого не замечал. Проще всего
    # получить такую структуру — вызвать саму ensure_structure и закоммитить.
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

        active_meso_before = (await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == test_user.id,
                AppUserMesocycle.is_active.is_(True),
            )
        )).scalars().first()
        active_micro_before = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )).scalars().first()
        assert active_meso_before is not None
        assert active_micro_before is not None
        active_meso_id = active_meso_before.id
        active_micro_id = active_micro_before.id

        mesocycles_count_before = len((await db.execute(
            select(Mesocycle.id).where(Mesocycle.author_id == test_user.id)
        )).scalars().all())
        microcycles_count_before = len((await db.execute(
            select(AppUserMicrocycle.id).where(
                AppUserMicrocycle.app_user_id == test_user.id
            )
        )).scalars().all())

    r = await client.get("/workout-center/context", headers=auth_headers)
    assert r.status_code == 200, r.text

    async with SessionLocal() as db:
        active_mesos_after = (await db.execute(
            select(AppUserMesocycle.id).where(
                AppUserMesocycle.app_user_id == test_user.id,
                AppUserMesocycle.is_active.is_(True),
            )
        )).scalars().all()
        active_micros_after = (await db.execute(
            select(AppUserMicrocycle.id).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )).scalars().all()
        mesocycles_count_after = len((await db.execute(
            select(Mesocycle.id).where(Mesocycle.author_id == test_user.id)
        )).scalars().all())
        microcycles_count_after = len((await db.execute(
            select(AppUserMicrocycle.id).where(
                AppUserMicrocycle.app_user_id == test_user.id
            )
        )).scalars().all())

    # Активными остались ТЕ ЖЕ записи по id — не просто "какой-то" активный.
    assert active_mesos_after == [active_meso_id]
    assert active_micros_after == [active_micro_id]
    # Число мезоциклов и микроциклов не выросло — ensure_structure ничего не
    # пересоздала.
    assert mesocycles_count_after == mesocycles_count_before
    assert microcycles_count_after == microcycles_count_before


async def test_unassigning_mesocycle_leaves_none_active(
    client, auth_headers, test_user,
):
    """Регрессия ревью Задачи 8 (P1-03 ч.1): PATCH .../mesocycle с
    mesocycle_id=null — легальный способ снять мезоцикл ("Без мезоцикла" в
    селекторе клиента) — раньше возвращал мезоцикл обратно в ответе того же
    запроса, потому что ensure_structure сидела в build_context и видела
    отсутствие активного мезоцикла как повод дочинить структуру. Если этот
    тест проходит и ДО фикса (ensure_structure в build_context), значит
    регрессию не воспроизвели.
    """
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    r = await client.patch(
        "/workout-center/context/mesocycle",
        json={"mesocycle_id": None},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["selected_periodization"] is None

    async with SessionLocal() as db:
        active_meso = (await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == test_user.id,
                AppUserMesocycle.is_active.is_(True),
            )
        )).scalars().first()

    assert active_meso is None


async def test_unassigning_mesocycle_survives_reopening_the_screen(
    client, auth_headers, test_user,
):
    """Правка Critical (P1-03 ч.1, Задача 8): test_unassigning_mesocycle_leaves_none_active
    выше проверяет только ответ ТОГО ЖЕ PATCH-запроса — этого недостаточно,
    потому что регрессия была именно в ПОВТОРНОМ GET /workout-center/context
    (ensure_structure видела «нет активного мезоцикла» и реактивировала
    дефолт при каждом возвращении на вкладку тренировки). Здесь после снятия
    мезоцикла делается отдельный повторный GET, и активного по-прежнему быть
    не должно.
    """
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    r = await client.patch(
        "/workout-center/context/mesocycle",
        json={"mesocycle_id": None},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    r2 = await client.get("/workout-center/context", headers=auth_headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["selected_periodization"] is None

    async with SessionLocal() as db:
        active_meso = (await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == test_user.id,
                AppUserMesocycle.is_active.is_(True),
            )
        )).scalars().first()

    assert active_meso is None


async def test_unassigning_microcycle_survives_reopening_the_screen(
    client, auth_headers, test_user,
):
    """Тот же сценарий, что и test_unassigning_mesocycle_survives_reopening_the_screen,
    для микроцикла: PATCH .../microcycle с microcycle_id=null («Без
    микроцикла» в селекторе), затем повторный GET — активного микроцикла
    по-прежнему быть не должно.
    """
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    r = await client.patch(
        "/workout-center/context/microcycle",
        json={"microcycle_id": None},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    r2 = await client.get("/workout-center/context", headers=auth_headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["selected_microcycle"] is None

    async with SessionLocal() as db:
        active_micro = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )).scalars().first()

    assert active_micro is None
