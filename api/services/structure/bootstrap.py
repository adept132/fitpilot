"""Заведение структуры пользователя (P1-03 часть 1, §5.5).

Единственный файл модуля structure/, ходящий в БД: остальные — чистые данные
и алгоритмы.

Пресеты применяются созданием ЛИЧНЫХ копий, а не ссылкой на системную строку
(решение 3): копия правится и удаляется существующими эндпоинтами без единой
правки роутеров и без миграции.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from api.services.models import (
    AppUser,
    AppUserMesocycle,
    AppUserMicrocycle,
    AppUserProfile,
    Mesocycle,
    MesocyclePhase,
    SplitBlueprint,
    SplitDaySlot,
    UserSplit,
)
from api.services.structure import mesocycle_presets as meso_presets
from api.services.structure import microcycle_profiles as micro_profiles
from api.services.structure.repository import rank_splits


async def _active_split_slots(
    session: AsyncSession, app_user_id: int
) -> list[micro_profiles.SlotView] | None:
    """Слоты активного сплита в порядке day_order, либо None."""
    active = (await session.execute(
        select(UserSplit).where(
            UserSplit.app_user_id == app_user_id, UserSplit.is_active.is_(True),
        )
    )).scalars().first()
    if active is None:
        return None

    blueprint = (await session.execute(
        select(SplitBlueprint)
        .where(SplitBlueprint.id == active.blueprint_id)
        .options(selectinload(SplitBlueprint.slots).selectinload(SplitDaySlot.day))
    )).scalars().first()
    if blueprint is None or not blueprint.slots:
        return None

    return [
        micro_profiles.SlotView(template_type=slot.day.template_type.value)
        for slot in sorted(blueprint.slots, key=lambda s: s.day_order)
    ]


async def _experience_level(session: AsyncSession, app_user_id: int) -> str:
    profile = (await session.execute(
        select(AppUserProfile).where(AppUserProfile.app_user_id == app_user_id)
    )).scalars().first()
    return (getattr(profile, "experience_level", None) or "beginner").strip().lower()


async def _has_active_user_split(session: AsyncSession, app_user_id: int) -> bool:
    """Есть ли у пользователя активный сплит — безотносительно его слотов.

    Нужна, чтобы отличить «сплита нет вовсе» (подбираем) от «сплит есть, но
    блюпринт пуст или удалён» (не трогаем: подбор подменил бы выбор
    человека). `_active_split_slots` оба случая схлопывает в None.
    """
    return (await session.execute(
        select(UserSplit.id).where(
            UserSplit.app_user_id == app_user_id,
            UserSplit.is_active.is_(True),
        ).limit(1)
    )).scalars().first() is not None


async def _autoselect_split(
    session: AsyncSession, app_user_id: int
) -> list[micro_profiles.SlotView] | None:
    """Подобрать и активировать сплит пользователю, у которого его нет.

    Критерий готовности §10.1 требует, чтобы блок существовал сразу после
    онбординга, «не открыв ни одного экрана настройки». Без активного сплита
    длину микроцикла взять неоткуда, структура не заводится, а вместе с ней
    мертвы фазы, разгрузки, окна план/факт и автопилот цели.

    Отступление от решения 8 спеки («автоподбор предлагает, а не назначает»)
    осознанное и узкое: активация происходит ТОЛЬКО когда активного сплита
    нет вовсе — перебивать нечего, а альтернатива это неработающий движок.
    Сменить сплит человек может в любой момент через селектор на экране
    тренировки или виджет «Расписание»; опции «без сплита» в интерфейсе нет
    (PATCH /workout-center/split требует существующий блюпринт, иначе 404),
    поэтому осознанного отказа, который тут можно было бы молча откатить, не
    существует — в отличие от «Без мезоцикла», из-за которого признак
    активации переделывали в Задаче 8.
    """
    # Serialize the check-and-insert sequence on the stable parent row.  A
    # uniqueness constraint on UserSplit cannot express "only one active"
    # without a new partial index, while every bootstrap already has exactly
    # one AppUser row to lock.  Recheck after acquiring the lock because a
    # concurrent request may have committed its selection while we waited.
    locked_user_id = (await session.execute(
        select(AppUser.id)
        .where(AppUser.id == app_user_id)
        .with_for_update()
    )).scalars().first()
    if locked_user_id is None:
        return None

    active_slots = await _active_split_slots(session, app_user_id)
    if active_slots is not None:
        return active_slots
    if await _has_active_user_split(session, app_user_id):
        return None

    candidates = await rank_splits(session, app_user_id, limit=1)
    if not candidates:
        return None

    session.add(UserSplit(
        app_user_id=app_user_id,
        blueprint_id=uuid.UUID(candidates[0].id),
        is_active=True,
        current_day=1,
    ))
    # flush, а не commit: коммит — забота вызывающей стороны (см. докстринг
    # ensure_structure). Без flush следующий SELECT не увидит новую строку.
    await session.flush()
    return await _active_split_slots(session, app_user_id)


async def _has_any_mesocycle(session: AsyncSession, app_user_id: int) -> bool:
    """Есть ли у пользователя хоть одна ЛИЧНАЯ копия мезоцикла (активная или нет)."""
    return (await session.execute(
        select(Mesocycle.id).where(Mesocycle.author_id == app_user_id).limit(1)
    )).scalars().first() is not None


async def _has_any_microcycle(session: AsyncSession, app_user_id: int) -> bool:
    """Есть ли у пользователя хоть один микроцикл (активный или нет)."""
    return (await session.execute(
        select(AppUserMicrocycle.id)
        .where(AppUserMicrocycle.app_user_id == app_user_id)
        .limit(1)
    )).scalars().first() is not None


async def ensure_structure(session: AsyncSession, app_user_id: int) -> dict:
    """Создать недостающие копии пресетов и активировать по одной.

    Идемпотентна: повторный вызов ничего не создаёт и не переключает активные.
    Не коммитит — это забота вызывающей стороны, как и в остальном модуле.

    ВНИМАНИЕ: функция может откатить транзакцию сессии (см. session.rollback()
    в обработчике IntegrityError ниже, повторное ревью, Находка 2) — как и
    ensure_active_block (api/services/periodization/repository.py), вызывать
    её нужно ДО того, как вызывающий код начал накапливать собственные
    незакоммиченные изменения в этой сессии, иначе они уедут в откат вместе с
    неудачной попыткой создать копии.
    """
    slots = await _active_split_slots(session, app_user_id)
    if slots is None:
        if await _has_active_user_split(session, app_user_id):
            # Активный сплит ЕСТЬ, но слотов у него не нашлось (блюпринт
            # удалён или пуст). Подбирать замену нельзя: это молча подменило
            # бы выбор человека. Длину микроцикла взять неоткуда — выходим.
            return {
                "mesocycles_created": 0, "microcycles_created": 0, "activated": False,
            }
        slots = await _autoselect_split(session, app_user_id)
        if slots is None:
            # Каталог пуст (сид не проигран) — заводить структуру не из чего.
            return {
                "mesocycles_created": 0, "microcycles_created": 0, "activated": False,
            }
        # Existing profile rows may have been built for a previously selected
        # split.  A freshly autoselected split is a real transition even when
        # both preset collections already exist, so repair/deactivate before
        # the idempotent early return below.
        await rebuild_profile_microcycles(session, app_user_id, slots)

    # Задача 8, правка Critical: признак «пора заводить структуру» — не
    # «нет АКТИВНОГО мезоцикла/микроцикла», а «нет НИ ОДНОЙ записи вовсе»,
    # отдельно для каждого типа. Активный мезоцикл/микроцикл — это выбор
    # пользователя (в том числе выбор снять его опцией «Без мезоцикла»/«Без
    # микроцикла» в селекторе), а не признак того, заводить ли структуру
    # заново. Раз хоть одна личная копия есть — пресеты уже применены когда-то,
    # и трогать активацию нельзя, что бы сейчас ни было активным.
    #
    # Этот же признак закрывает и прежнюю Находку 1 (переименование неактивной
    # копии PUT /microcycles/{id} не должно заводить шестую копию при поиске
    # по имени) — «есть хоть одна запись» истинно и для переименованной
    # строки, отдельная проверка на активность для этого была не нужна.
    #
    # Самовосстановление сохранено: если пользователь удалил ВСЕ свои
    # микроциклы (или мезоциклы), записей для этого типа больше нет, и
    # функция идёт дальше как обычно и пересоздаёт недостающее.
    had_mesocycles = await _has_any_mesocycle(session, app_user_id)
    had_microcycles = await _has_any_microcycle(session, app_user_id)
    if had_mesocycles and had_microcycles:
        return {"mesocycles_created": 0, "microcycles_created": 0, "activated": True}

    length = len(slots)
    level = await _experience_level(session, app_user_id)
    beginner = level == "beginner"

    try:
        # --- Мезоциклы ---
        existing_meso = {
            row.code: row
            for row in (await session.execute(
                select(Mesocycle).where(Mesocycle.author_id == app_user_id)
            )).scalars().all()
        }
        mesocycles_created = 0
        for preset in meso_presets.MESOCYCLE_PRESETS:
            if preset.code in existing_meso:
                continue
            meso = Mesocycle(
                author_id=app_user_id,
                name=preset.name,
                code=preset.code,
                description=preset.description,
                phases_in_cycle=len(preset.tiers),
            )
            session.add(meso)
            await session.flush()
            for number, tier in enumerate(preset.tiers, start=1):
                session.add(MesocyclePhase(
                    mesocycle_id=meso.id,
                    phase_number=number,
                    name=meso_presets.phase_name(tier),
                    effort_tier=tier,
                ))
            existing_meso[preset.code] = meso
            mesocycles_created += 1

        # --- Микроциклы ---
        existing_micro = {
            row.name: row
            for row in (await session.execute(
                select(AppUserMicrocycle).where(
                    AppUserMicrocycle.app_user_id == app_user_id
                )
            )).scalars().all()
        }
        microcycles_created = 0
        for profile in micro_profiles.MICROCYCLE_PROFILES:
            if profile.name in existing_micro:
                continue
            micro = AppUserMicrocycle(
                app_user_id=app_user_id,
                name=profile.name,
                length_days=length,
                days_mapping=micro_profiles.build_days_mapping(profile.code, slots),
            )
            session.add(micro)
            existing_micro[profile.name] = micro
            microcycles_created += 1
            # Не про получение id — micro.id тут не используется. Это про то,
            # чтобы конфликт uq_app_user_microcycle_name (app_user_id, name)
            # от параллельного вызова всплыл INSERT'ом ВНУТРИ этого try, а не
            # позже неявным autoflush на первом SELECT раздела «Активация»,
            # который уже вне try/except и оставил бы IntegrityError
            # необработанным. Не убирать.
            await session.flush()
    except IntegrityError:
        # Повторное ревью, Находка 2: два параллельных POST /profile/structure/bootstrap
        # (мобильный клиент умеет дублировать запросы) гоняются за одними и
        # теми же 5 пресетами мезоциклов. Мезоциклы защищены уникальным
        # индексом uq_mesocycle_author_code — проигравшая транзакция получает
        # IntegrityError прямо на INSERT, ДО того как успевает дойти до цикла
        # микроциклов (он строго после мезоциклов в этом же try) — поэтому
        # один и тот же except закрывает и мезоциклы, и микроциклы, хотя у
        # микроциклов своего уникального индекса нет и не будет (схему не
        # трогаем — тихий дубль-race там структурно возможен только если обе
        # стороны успели МИНОВАТЬ цикл мезоциклов одновременно, а это как раз
        # то, что здесь ловится). Откатываем свою неудачную вставку и
        # возвращаем факт: победившая транзакция уже сделала (или вот-вот
        # сделает) всё нужное, тот же контракт, что у ensure_active_block.
        await session.rollback()
        # Правка, финальное ревью P1-03, Important 3: раньше здесь стоял
        # _has_active_structure (требовал АКТИВНОГО мезоцикла и микроцикла
        # одновременно). Но легальное состояние «копии есть, ни одна не
        # активна» существует — пользователь мог снять мезоцикл опцией «Без
        # мезоцикла» (см. правку в разделе «Активация» выше). Проигравшая
        # параллельная транзакция в этом состоянии видела бы is_active=False
        # у обеих сущностей, _has_active_structure вернула бы False, и код
        # пробрасывал бы исключение — 500 вместо благополучного возврата,
        # хотя победившая транзакция уже создала (или создаёт) нужные записи.
        # Проверяем факт СУЩЕСТВОВАНИЯ записей, а не их активности — тот же
        # признак, на котором строится весь остальной ensure_structure.
        if not (
            await _has_any_mesocycle(session, app_user_id)
            and await _has_any_microcycle(session, app_user_id)
        ):
            # Записей по-прежнему нет — IntegrityError был не про эту гонку
            # (иначе конкурентная транзакция уже успела бы закоммитить свои
            # мезоцикл/микроцикл), а про что-то другое: нарушение NOT NULL,
            # битый внешний ключ, порчу данных. Маскировать чужую ошибку тем
            # же нулевым ответом, что и легальный "у пользователя нет
            # активного сплита", нельзя — тот же контракт, что у
            # ensure_active_block.
            raise
        return {
            "mesocycles_created": 0,
            "microcycles_created": 0,
            "activated": True,
        }

    # --- Активация ---
    # Активируем дефолт только для типа, у которого до этого вызова не было
    # НИ ОДНОЙ записи (had_mesocycles/had_microcycles выше) — тот же признак,
    # что и в раннем выходе, здесь просто его отрицание. Если записи уже
    # были (пусть и без активной — пользователь явно снял выбор), ничего не
    # активируем: это и есть исправление регрессии с «Без мезоцикла».
    if not had_mesocycles:
        code = (
            meso_presets.DEFAULT_FOR_BEGINNER if beginner
            else meso_presets.DEFAULT_FOR_EXPERIENCED
        )
        session.add(AppUserMesocycle(
            app_user_id=app_user_id,
            mesocycle_id=existing_meso[code].id,
            is_active=True,
            microcycle_length=length,
            # Без номера фазы calculate_exercise_recommendation не найдёт
            # effort_tier и молча откатится на medium.
            current_phase=1,
        ))

    if not had_microcycles:
        code = (
            micro_profiles.DEFAULT_FOR_BEGINNER if beginner
            else micro_profiles.DEFAULT_FOR_EXPERIENCED
        )
        existing_micro[micro_profiles.profile_by_code(code).name].is_active = True

    return {
        "mesocycles_created": mesocycles_created,
        "microcycles_created": microcycles_created,
        "activated": True,
    }


def _slots_from_mapping(days_mapping: dict) -> list[micro_profiles.SlotView]:
    """Слоты, из которых была построена раскладка.

    Признак «правлен руками» отдельным полем не хранится: достаточно
    восстановить прежние слоты из самой раскладки и сравнить её с тем, что
    даёт профиль на них (§5.6).
    """
    slots: list[micro_profiles.SlotView] = []
    for position in range(1, len(days_mapping) + 1):
        entry = days_mapping.get(str(position)) or {}
        if entry.get("type") == "rest" or not entry.get("tag"):
            slots.append(micro_profiles.SlotView(micro_profiles.REST_TYPE))
        else:
            slots.append(micro_profiles.SlotView(entry["tag"]))
    return slots


async def rebuild_profile_microcycles(
    session: AsyncSession, app_user_id: int, slots: list[micro_profiles.SlotView]
) -> dict:
    """Пересобрать неправленые микроциклы под новые слоты.

    Возвращает {"rebuilt": число пересобранных, "deactivated_microcycle":
    id снятого с активности микроцикла несовпадающей длины, либо None}.
    """
    by_name = {p.name: p for p in micro_profiles.MICROCYCLE_PROFILES}
    rows = (await session.execute(
        select(AppUserMicrocycle).where(AppUserMicrocycle.app_user_id == app_user_id)
    )).scalars().all()

    rebuilt = 0
    rebuilt_ids: set[int] = set()
    for row in rows:
        profile = by_name.get(row.name)
        if profile is None:
            continue  # чужой микроцикл, не из пресетов
        previous_slots = _slots_from_mapping(row.days_mapping or {})
        expected = micro_profiles.build_days_mapping(profile.code, previous_slots)
        if row.days_mapping != expected:
            continue  # человек правил — не трогаем (§5.6)
        row.days_mapping = micro_profiles.build_days_mapping(profile.code, slots)
        row.length_days = len(slots)
        rebuilt += 1
        rebuilt_ids.add(row.id)

    # Финальное ревью P1-03, правка 1, §10.5: активный микроцикл длины M при
    # активном сплите длины N != M — недостижимая пара (та же гарантия, что и
    # 409 в PATCH /workout-center/context/microcycle, см. её докстринг).
    # scheduling_engine.py считает день сплита и день микроцикла двумя
    # независимыми счётчиками — расхождение длин молча уводит раскладку
    # диапазонов повторов относительно дней. Микроциклы выше НЕ пересобраны
    # намеренно (правлены руками или чужие, не из пресетов) — переписывать их
    # раскладку насильно нельзя, поэтому единственный безопасный выход —
    # снять с активного флаг is_active. Раскладку он не теряет и остаётся в
    # списке, пользователь выберет подходящую в селекторе сам.
    deactivated_microcycle = None
    target_length = len(slots)
    for row in rows:
        if row.is_active and row.id not in rebuilt_ids and row.length_days != target_length:
            row.is_active = False
            deactivated_microcycle = row.id
            break

    return {"rebuilt": rebuilt, "deactivated_microcycle": deactivated_microcycle}


async def rebuild_for_active_split(session: AsyncSession, app_user_id: int) -> dict:
    """Перестроить микроциклы под текущий активный сплит."""
    slots = await _active_split_slots(session, app_user_id)
    if slots is None:
        return {"rebuilt": 0, "deactivated_microcycle": None}
    return await rebuild_profile_microcycles(session, app_user_id, slots)
