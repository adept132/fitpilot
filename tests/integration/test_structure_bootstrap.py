"""Bootstrap структуры: копии пресетов, активация, заведение блока (P1-03 ч.1, §5.5)."""
import asyncio

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

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
    имя пресета. Ранний выход по факту наличия хотя бы одной записи каждого
    типа (had_mesocycles/had_microcycles, не привязанный к активности)
    закрывает это."""
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


async def test_bootstrap_does_nothing_when_there_is_nothing_to_pick(
    test_user, monkeypatch,
):
    """Каталог пуст (сид не проигран) — подобрать сплит не из чего.

    Раньше этот тест закреплял «нет активного сплита — ничего не делаем».
    Критерий §10.1 это отменил: теперь сплит подбирается автоматически, и
    единственный оставшийся случай бездействия — пустой каталог.
    """
    import api.services.structure.bootstrap as bootstrap_module

    async def _no_candidates(*args, **kwargs):
        return []

    monkeypatch.setattr(bootstrap_module, "rank_splits", _no_candidates)

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

        # Патчим именно db.flush (метод инстанса), а не что-то на уровне
        # класса/engine: неявный autoflush AsyncSession перед execute() идёт
        # через отдельный синхронный путь SQLAlchemy и этот патч не заметит —
        # сработать обязан явный `await session.flush()` внутри цикла
        # создания мезоциклов (см. его докстринг в bootstrap.py, "Не про
        # получение id"). При смене версии SQLAlchemy стоит перепроверить,
        # что autoflush по-прежнему не заходит через db.flush.
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


async def test_block_is_created_for_a_user_who_never_picked_a_split(test_user):
    """Критерий §10.1: блок существует сразу после онбординга, «не открыв ни
    одного экрана настройки». До правки ensure_structure выходила рано без
    активного сплита, и блок появлялся только после ручного выбора."""
    async with SessionLocal() as db:
        await ensure_system_splits(db)
        await db.commit()

    async with SessionLocal() as db:
        assert await ensure_active_block(db, test_user.id, date.today()) is None

    async with SessionLocal() as db:
        result = await ensure_structure(db, test_user.id)
        await db.commit()

    assert result["activated"] is True

    async with SessionLocal() as db:
        active_split = (await db.execute(
            select(UserSplit).where(
                UserSplit.app_user_id == test_user.id, UserSplit.is_active.is_(True),
            )
        )).scalars().first()
        assert active_split is not None, "сплит должен быть подобран автоматически"

        block = await ensure_active_block(db, test_user.id, date.today())
        assert block is not None


async def test_autoselect_does_not_replace_a_split_whose_blueprint_is_broken(test_user):
    """Активный сплит есть, но слотов у него нет (блюпринт пуст). Подбирать
    замену нельзя — это молча подменило бы выбор человека."""
    async with SessionLocal() as db:
        await ensure_system_splits(db)
        empty = SplitBlueprint(name="Пустой", length_days=7, is_system=False,
                               author_id=test_user.id)
        db.add(empty)
        await db.flush()
        db.add(UserSplit(app_user_id=test_user.id, blueprint_id=empty.id,
                         is_active=True, current_day=1))
        await db.commit()
        broken_id = empty.id

    async with SessionLocal() as db:
        result = await ensure_structure(db, test_user.id)
        await db.commit()

    assert result["activated"] is False
    assert await _counts(test_user.id) == (0, 0)

    async with SessionLocal() as db:
        active = (await db.execute(
            select(UserSplit).where(
                UserSplit.app_user_id == test_user.id, UserSplit.is_active.is_(True),
            )
        )).scalars().all()
    assert len(active) == 1 and active[0].blueprint_id == broken_id


async def test_autoselect_repairs_existing_microcycles_before_early_return(test_user):
    """Existing presets still need transition repair after a new autoselection."""
    await _activate_split(test_user.id, "Верх / низ на восьмидневке")
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    async with SessionLocal() as db:
        before = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id
            )
        )).scalars().all()
        assert before and all(row.length_days == 8 for row in before)
        await db.execute(
            update(UserSplit)
            .where(UserSplit.app_user_id == test_user.id)
            .values(is_active=False)
        )
        await db.commit()

    async with SessionLocal() as db:
        result = await ensure_structure(db, test_user.id)
        await db.commit()

    assert result == {
        "mesocycles_created": 0, "microcycles_created": 0, "activated": True,
    }
    async with SessionLocal() as db:
        active = (await db.execute(
            select(UserSplit)
            .where(UserSplit.app_user_id == test_user.id, UserSplit.is_active.is_(True))
            .options(selectinload(UserSplit.blueprint))
        )).scalars().one()
        after = (await db.execute(
            select(AppUserMicrocycle).where(
                AppUserMicrocycle.app_user_id == test_user.id
            )
        )).scalars().all()
    assert active.blueprint.length_days == 7
    assert all(row.length_days == 7 for row in after)


async def test_concurrent_autoselect_creates_only_one_active_split(
    test_user, monkeypatch,
):
    """Serialize the no-split check so two bootstrap requests cannot both insert."""
    import api.services.structure.bootstrap as bootstrap_module

    # Seed an existing structure, then remove only the active split.  With no
    # unique preset inserts left to accidentally serialize the requests, the
    # old implementation deterministically created two active UserSplit rows.
    await _activate_split(test_user.id)
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.execute(
            update(UserSplit)
            .where(UserSplit.app_user_id == test_user.id)
            .values(is_active=False)
        )
        await db.commit()

    original_rank = bootstrap_module.rank_splits
    both_ranked = asyncio.Event()
    rank_calls = 0

    async def synchronized_rank(*args, **kwargs):
        nonlocal rank_calls
        rank_calls += 1
        if rank_calls == 2:
            both_ranked.set()
        else:
            try:
                await asyncio.wait_for(both_ranked.wait(), timeout=0.25)
            except TimeoutError:
                # With the AppUser row lock the second request cannot reach
                # ranking until the first commits, so one call is expected.
                pass
        return await original_rank(*args, **kwargs)

    monkeypatch.setattr(bootstrap_module, "rank_splits", synchronized_rank)

    async def bootstrap_once():
        async with SessionLocal() as db:
            result = await ensure_structure(db, test_user.id)
            await db.commit()
            return result

    await asyncio.wait_for(
        asyncio.gather(bootstrap_once(), bootstrap_once()), timeout=5,
    )

    async with SessionLocal() as db:
        active = (await db.execute(
            select(UserSplit).where(
                UserSplit.app_user_id == test_user.id,
                UserSplit.is_active.is_(True),
            )
        )).scalars().all()
    assert len(active) == 1
    assert rank_calls == 1
