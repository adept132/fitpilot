"""Bootstrap структуры: копии пресетов, активация, заведение блока (P1-03 ч.1, §5.5)."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.seed_splits import ensure_system_splits
from api.services.models import (
    AppUserMesocycle, AppUserMicrocycle, Mesocycle, SplitBlueprint, UserSplit,
)
from api.services.periodization.repository import ensure_active_block
from api.services.structure.bootstrap import ensure_structure
from app.database import SessionLocal
from datetime import date

pytestmark = pytest.mark.asyncio


async def _activate_split(user_id: int, name: str = "Upper / Lower (4 Дня)") -> int:
    async with SessionLocal() as db:
        await ensure_system_splits(db)
        blueprint = (await db.execute(
            select(SplitBlueprint).where(
                SplitBlueprint.is_system.is_(True), SplitBlueprint.name == name,
            )
        )).scalars().first()
        db.add(UserSplit(
            app_user_id=user_id, blueprint_id=blueprint.id,
            is_active=True, current_day=1,
        ))
        await db.commit()
        return blueprint.length_days


async def _counts(user_id: int) -> tuple[int, int]:
    async with SessionLocal() as db:
        mesos = (await db.execute(
            select(Mesocycle).where(Mesocycle.author_id == user_id)
        )).scalars().all()
        micros = (await db.execute(
            select(AppUserMicrocycle).where(AppUserMicrocycle.app_user_id == user_id)
        )).scalars().all()
    return len(mesos), len(micros)


async def test_bootstrap_creates_five_of_each_and_activates_one(test_user):
    length = await _activate_split(test_user.id)

    async with SessionLocal() as db:
        result = await ensure_structure(db, test_user.id)
        await db.commit()

    assert result["mesocycles_created"] == 5
    assert result["microcycles_created"] == 5
    assert result["activated"] is True
    assert await _counts(test_user.id) == (5, 5)

    async with SessionLocal() as db:
        active_meso = (await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == test_user.id,
                AppUserMesocycle.is_active.is_(True),
            )
        )).scalars().first()
        active_micro = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )).scalars().first()

    assert active_meso is not None
    assert active_meso.current_phase == 1
    assert active_meso.microcycle_length == length
    assert active_micro is not None
    assert active_micro.length_days == length


async def _active_ids(user_id: int) -> tuple:
    async with SessionLocal() as db:
        active_meso = (await db.execute(
            select(AppUserMesocycle).where(
                AppUserMesocycle.app_user_id == user_id,
                AppUserMesocycle.is_active.is_(True),
            )
        )).scalars().first()
        active_micro = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == user_id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )).scalars().first()
    return active_meso.id, active_micro.id


async def test_bootstrap_is_idempotent(test_user):
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    first_active_meso_id, first_active_micro_id = await _active_ids(test_user.id)

    async with SessionLocal() as db:
        second = await ensure_structure(db, test_user.id)
        await db.commit()

    assert second["mesocycles_created"] == 0
    assert second["microcycles_created"] == 0
    assert await _counts(test_user.id) == (5, 5)

    # Повторный вызов не только не создаёт лишних строк, но и не переключает
    # активные — это должны остаться РОВНО те же записи, что и после первого.
    second_active_meso_id, second_active_micro_id = await _active_ids(test_user.id)
    assert second_active_meso_id == first_active_meso_id
    assert second_active_micro_id == first_active_micro_id


async def test_bootstrap_renaming_active_microcycle_does_not_duplicate(test_user):
    """Повторное ревью, Находка 1: PUT /microcycles/{id} разрешает переименовать
    личную копию, пока она не активна. Раньше ensure_structure искала
    существующие микроциклы по имени и заводила шестую копию, не найдя старое
    имя пресета. Ранний выход по паре активных мезо/микро закрывает это."""
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    async with SessionLocal() as db:
        micros = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.is_active.is_(False),
            )
        )).scalars().all()
        target = micros[0]
        target.name = "Переименованный вручную профиль"
        await db.commit()

    async with SessionLocal() as db:
        second = await ensure_structure(db, test_user.id)
        await db.commit()

    assert second == {
        "mesocycles_created": 0, "microcycles_created": 0, "activated": True,
    }
    assert await _counts(test_user.id) == (5, 5)


async def test_bootstrap_recreates_microcycles_after_all_deleted(test_user):
    """Самовосстановление (спека §5.5) должно пережить ранний выход из
    Находки 1: если пользователь удалил ВСЕ свои микроциклы, активного нет,
    и структура пересоздаётся заново."""
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    async with SessionLocal() as db:
        await db.execute(
            AppUserMicrocycle.__table__.delete().where(
                AppUserMicrocycle.app_user_id == test_user.id
            )
        )
        await db.commit()

    assert await _counts(test_user.id) == (5, 0)

    async with SessionLocal() as db:
        result = await ensure_structure(db, test_user.id)
        await db.commit()

    assert result["mesocycles_created"] == 0
    assert result["microcycles_created"] == 5
    assert result["activated"] is True
    assert await _counts(test_user.id) == (5, 5)

    async with SessionLocal() as db:
        active_micro = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
                AppUserMicrocycle.is_active.is_(True),
            )
        )).scalars().first()
    assert active_micro is not None


async def test_bootstrap_without_an_active_split_does_nothing(test_user):
    async with SessionLocal() as db:
        result = await ensure_structure(db, test_user.id)
        await db.commit()

    assert result == {
        "mesocycles_created": 0, "microcycles_created": 0, "activated": False,
    }
    assert await _counts(test_user.id) == (0, 0)


async def test_bootstrap_reraises_integrity_error_unrelated_to_the_race(
    test_user, monkeypatch,
):
    """Правка: проброс чужого IntegrityError. except IntegrityError в
    ensure_structure раньше глотал ЛЮБОЙ IntegrityError и возвращал нулевой
    ответ — неотличимый от легального "нет активного сплита". Симулируем
    IntegrityError на первом session.flush() внутри цикла создания
    мезоциклов: у пользователя нет и не появится активной структуры (сплит
    активирован, но до создания копий дело не дошло), значит после rollback
    _has_active_structure подтвердит, что это была не гонка, и исключение
    обязано вылететь наружу, а не превратиться в нулевой ответ."""
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        async def _boom(*args, **kwargs):
            raise IntegrityError("simulated", {}, Exception("not the race"))

        monkeypatch.setattr(db, "flush", _boom)

        with pytest.raises(IntegrityError):
            await ensure_structure(db, test_user.id)

    assert await _counts(test_user.id) == (0, 0)


async def test_block_is_created_after_bootstrap(test_user):
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        block_before = await ensure_active_block(db, test_user.id, date.today())
    assert block_before is None

    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    async with SessionLocal() as db:
        block_after = await ensure_active_block(db, test_user.id, date.today())
    assert block_after is not None


async def test_microcycle_name_unique_per_user(test_user):
    """uq_app_user_microcycle_name: ensure_structure's race protection (см.
    докстринг в bootstrap.py и в миграции 20260822_02) держится на том, что
    второй микроцикл с тем же именем у того же пользователя не проходит
    INSERT."""
    await _activate_split(test_user.id)

    async with SessionLocal() as db:
        result = await ensure_structure(db, test_user.id)
        await db.commit()
    assert result["microcycles_created"] == 5

    async with SessionLocal() as db:
        existing = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id,
            ).limit(1)
        )).scalars().first()
        db.add(AppUserMicrocycle(
            app_user_id=test_user.id,
            name=existing.name,
            length_days=existing.length_days,
            days_mapping={},
        ))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_endpoint_runs_bootstrap(client, auth_headers, test_user):
    await _activate_split(test_user.id)

    r = await client.post("/profile/structure/bootstrap", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["activated"] is True
    assert await _counts(test_user.id) == (5, 5)
