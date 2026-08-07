"""Загрузка входов контура «план -> факт -> решение» и запись снимков."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from api.services.models import (
    Exercise,
    UserCalendarDay,
    VolumeWindow,
    WorkoutPlanExercise,
    WorkoutSession,
    WorkoutSessionExercise,
    WorkoutSessionSet,
)
from api.services.volume.landmarks import landmarks_for
from api.services.volume.measure import (
    MuscleContribution,
    accumulate,
    apply_adjustments,
    contribution,
)

logger = logging.getLogger(__name__)

# [КОНФИГ] Уровень усилия, при котором окно считается разгрузочным.
# Совпадает с periodization.params.DELOAD_TIER; дублируется здесь, чтобы
# пакет объёма не зависел от пакета периодизации ради одной строки.
DELOAD_TIER = "deload"


async def attach_session_to_day(
    session: AsyncSession,
    app_user_id: int,
    workout: WorkoutSession,
) -> Optional[UserCalendarDay]:
    """Закрыть плановый день завершённой сессией.

    Два правила, в порядке приоритета:
      1. явная привязка из старта (`workout.calendar_day_id`) — намерение
         пользователя, известное достоверно; переживает тренировку,
         затянувшуюся за полночь;
      2. доматчивание по дате старта — для сессий без привязки:
         спонтанных, импортированных из CSV и приехавших из офлайн-очереди.
         Без этого правила adherence был бы систематически занижен ровно у
         самых активных пользователей (офлайн — базовый сценарий, P0-02).

    День отдыха и blackout не закрываются: они не входят в знаменатель
    adherence, и «выполнить» их нечем. Уже закрытый день не перехватывается
    второй сессией — факт принадлежит первой.
    """
    day: Optional[UserCalendarDay] = None

    if workout.calendar_day_id:
        day = (await session.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.id == workout.calendar_day_id,
                UserCalendarDay.app_user_id == app_user_id,
            )
        )).scalar_one_or_none()

    if day is None:
        started_on: date = workout.started_at.date()
        day = (await session.execute(
            select(UserCalendarDay).where(
                UserCalendarDay.app_user_id == app_user_id,
                UserCalendarDay.target_date == started_on,
            )
        )).scalar_one_or_none()

    if day is None or day.is_rest_day or day.is_blackout:
        return None
    if day.actual_workout_session_id is not None:
        return None

    day.status = "completed"
    day.actual_workout_session_id = workout.id
    return day


async def guarded(session: AsyncSession, label: str, work) -> Any:
    """Выполнить надстроечную работу так, чтобы её падение не уронило
    основной путь и не отравило транзакцию вызывающего.

    Голого try/except здесь недостаточно: работа идёт в ТОЙ ЖЕ сессии,
    которую вызывающий эндпоинт потом коммитит, и ошибка уровня DBAPI
    помечает транзакцию как требующую отката — следующий session.commit()
    упадёт с PendingRollbackError, хотя исключение уже поймано и
    залогировано. SAVEPOINT изолирует падение.

    Форма без `async with` — сознательная: контекст-менеджер регистрирует
    себя владельцем транзакционного контекста и ломается, если обёрнутая
    работа успела закоммитить сессию сама. Подробный разбор — в докстринге
    safe_refresh_proposals (api/services/periodization/service.py).

    `work` — уже созданная корутина; корутины ленивы, поэтому создание её
    до begin_nested() безопасно.
    """
    nested = await session.begin_nested()
    try:
        result = await work
    except Exception:  # noqa: BLE001
        logger.exception("P0-09: %s — упало, основной путь продолжается", label)
        if nested.is_active:
            await nested.rollback()
        return None
    else:
        if nested.is_active:
            await nested.commit()
        return result


def utc_today() -> date:
    """Сегодняшняя дата в UTC — единственный базис дат на сервере.

    Не `date.today()`: на Render сервер и так идёт в UTC, но на машине
    разработчика в другом поясе локальная дата расходится с UTC-датой
    сессии на несколько часов в сутки, и тесты, строящие день календаря по
    локальной дате, ловят ложное падение около полуночи. Явный базис делает
    dev и prod одинаковыми.
    """
    return datetime.now(timezone.utc).date()


async def mark_missed_days(
    session: AsyncSession, app_user_id: int, today: date
) -> int:
    """Пометить прошедшие плановые дни как пропущенные.

    Лениво, при обращении к контексту — тем же приёмом «тихо доделать при
    обращении», которым живёт ensure_horizon. Отдельного планировщика в
    проекте нет и заводить его не нужно.

    Сегодняшний день не трогаем: он ещё не кончился. Дни отдыха и blackout
    пропустить нельзя по определению — они не в знаменателе adherence.
    """
    result = await session.execute(
        update(UserCalendarDay)
        .where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date < today,
            UserCalendarDay.status == "planned",
            UserCalendarDay.is_rest_day.is_(False),
            UserCalendarDay.is_blackout.is_(False),
        )
        .values(status="missed")
    )
    return int(result.rowcount or 0)


@dataclass(frozen=True)
class Adherence:
    planned_days: int
    completed_days: int
    missed_days: int


async def adherence_for_range(
    session: AsyncSession, app_user_id: int, start: date, end: date
) -> Adherence:
    """Исполняемость на отрезке дат по рабочим дням.

    Дни отдыха и blackout в знаменатель не входят: их невыполнение не
    является пропуском.
    """
    rows = (await session.execute(
        select(UserCalendarDay.status).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date >= start,
            UserCalendarDay.target_date <= end,
            UserCalendarDay.is_rest_day.is_(False),
            UserCalendarDay.is_blackout.is_(False),
        )
    )).scalars().all()

    return Adherence(
        planned_days=len(rows),
        completed_days=sum(1 for s in rows if s == "completed"),
        missed_days=sum(1 for s in rows if s == "missed"),
    )


@dataclass(frozen=True)
class Window:
    block_id: Optional[int]
    window_index: int
    phase_number: Optional[int]
    start_date: date
    end_date: date
    is_deload: bool


async def _window_starts(
    session: AsyncSession, app_user_id: int, block_id: Optional[int]
) -> list:
    """Дни, с которых начинаются микроциклы, по возрастанию даты — в ПРЕДЕЛАХ ОДНОГО БЛОКА.

    Границы берутся из УЖЕ материализованной правды — сброса
    UserCalendarDay.microcycle_day_number в 1, — а не считаются формулой
    от даты старта блока. День календаря единственное место, где раскладка
    микроцикла разрешена с учётом blackout и вставленных разгрузок; вторая
    формула стала бы вторым источником правды, ровно той болезнью, которую
    P0-08 лечил у номера фазы.

    Скоуп по `block_id` обязателен (ревью Задачи 8 P0-09, Critical): блоки
    закрываются в разное время и разными путями (close_stale_block, layoff,
    досрочная разгрузка — api/services/periodization/service.py), и
    непрерывность дат между концом одного блока и стартом следующего никто
    не гарантирует. Без привязки к блоку маркеры собирались бы по всей
    истории пользователя сразу, и последнее окно одного блока молча
    растягивалось бы через разрыв до дня перед стартом следующего.

    `block_id=None` — дни, сгенерированные до P0-08, у них block_id ещё не
    проставлен; фильтруем через `.is_(None)`, а не `== None`, потому что
    SQL не считает NULL равным NULL через `=`.
    """
    if block_id is None:
        block_filter = UserCalendarDay.block_id.is_(None)
    else:
        block_filter = UserCalendarDay.block_id == block_id
    rows = (await session.execute(
        select(UserCalendarDay)
        .where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.microcycle_day_number == 1,
            block_filter,
        )
        .order_by(UserCalendarDay.target_date)
    )).scalars().all()
    return list(rows)


async def _last_calendar_date(
    session: AsyncSession, app_user_id: int, block_id: Optional[int]
) -> Optional[date]:
    """Последняя дата календаря В ПРЕДЕЛАХ ОДНОГО БЛОКА.

    Тот же скоуп, что и у `_window_starts` и по той же причине: без него
    последнее окно блока растягивалось бы до конца всего календаря
    пользователя, а не до конца своего блока.
    """
    if block_id is None:
        block_filter = UserCalendarDay.block_id.is_(None)
    else:
        block_filter = UserCalendarDay.block_id == block_id
    return (await session.execute(
        select(func.max(UserCalendarDay.target_date)).where(
            UserCalendarDay.app_user_id == app_user_id,
            block_filter,
        )
    )).scalar_one_or_none()


def _window_from(starts: list, index: int, last_date: date) -> Window:
    """Собрать Window по позиции `index` в списке стартов микроциклов.

    `starts` уже скоупится одним блоком (см. `_window_starts`), поэтому
    window_index = `index + 1` — это номер окна ВНУТРИ БЛОКА, а не сквозной
    номер по всей истории пользователя (по аналогии с phase_number, P0-08).

    `window_after` больше не полагается на эту нумерацию для поиска позиции
    в заново загруженном `starts` — она ищет позицию по `start_date` (см. её
    докстринг), так что связка по индексу сюда не тянется.
    """
    head = starts[index]
    end = (
        starts[index + 1].target_date - timedelta(days=1)
        if index + 1 < len(starts)
        else last_date
    )
    return Window(
        block_id=head.block_id,
        window_index=index + 1,
        phase_number=head.mesocycle_phase_number,
        start_date=head.target_date,
        end_date=end,
        is_deload=(head.meso_tag == DELOAD_TIER),
    )


async def current_window(
    session: AsyncSession, app_user_id: int, today: date
) -> Optional[Window]:
    """Окно, в которое попадает дата. None, если календаря нет.

    Сначала находим день календаря, покрывающий `today` (последний день с
    `target_date <= today`) — он определяет БЛОК. Дальше все стартовые
    маркеры микроцикла и последняя дата ищутся только внутри этого блока
    (ревью Задачи 8 P0-09, Critical): блоки закрываются в разное время и
    разными путями, непрерывность дат между ними не гарантирована, и без
    привязки к блоку окно могло бы молча растянуться через разрыв в чужой
    блок.
    """
    covering_day = (await session.execute(
        select(UserCalendarDay)
        .where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date <= today,
        )
        .order_by(UserCalendarDay.target_date.desc())
        .limit(1)
    )).scalar_one_or_none()
    if covering_day is None:
        return None
    block_id = covering_day.block_id

    starts = await _window_starts(session, app_user_id, block_id)
    if not starts:
        return None
    last_date = await _last_calendar_date(session, app_user_id, block_id)
    if last_date is None:
        return None

    index = None
    for i, head in enumerate(starts):
        if head.target_date <= today:
            index = i
        else:
            break
    if index is None:
        return None
    return _window_from(starts, index, last_date)


async def window_after(
    session: AsyncSession, app_user_id: int, window: Window
) -> Optional[Window]:
    """Следующее окно ВНУТРИ ТОГО ЖЕ БЛОКА. Оно уже сгенерировано — календарь
    развёрнут вперёд.

    Позиция окна ищется по дате начала (`window.start_date`), а не по
    `window.window_index` (ревью Задачи 8 P0-09, Important): связка по
    индексу полагалась на то, что второй независимый вызов `_window_starts`
    вернёт те же элементы в том же порядке, и на инвариант нумерации
    `_window_from`, без всякой гарантии этого на уровне кода. Поиск по дате
    от этой связки не зависит вовсе.

    Если следующего микроцикла в этом блоке нет — блок кончился; следующий
    блок (если он есть) это не "дальше" в рамках текущего окна, поэтому None.
    """
    starts = await _window_starts(session, app_user_id, window.block_id)
    last_date = await _last_calendar_date(session, app_user_id, window.block_id)
    if not starts or last_date is None:
        return None
    index = next(
        (i for i, head in enumerate(starts) if head.target_date == window.start_date),
        None,
    )
    if index is None or index + 1 >= len(starts):
        return None
    return _window_from(starts, index + 1, last_date)


@dataclass(frozen=True)
class MuscleRow:
    target: float
    prescribed: float
    performed_direct: float
    performed_indirect: float

    @property
    def performed_effective(self) -> float:
        return MuscleContribution(
            direct=self.performed_direct, indirect=self.performed_indirect
        ).effective


async def prescribed_for(
    session: AsyncSession, app_user_id: int, window: Window
) -> dict[str, MuscleContribution]:
    """Что микроцикл предписывает, если выполнить его как есть.

    Пропущенный день из предписания НЕ вычитается: иначе разрыв исполнения
    растворился бы сам собой и стал бы невидим.
    """
    days = (await session.execute(
        select(UserCalendarDay).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date >= window.start_date,
            UserCalendarDay.target_date <= window.end_date,
            UserCalendarDay.plan_id.is_not(None),
        )
    )).scalars().all()
    if not days:
        return {}

    plan_ids = {d.plan_id for d in days}
    rows = (await session.execute(
        select(WorkoutPlanExercise, Exercise)
        .join(Exercise, WorkoutPlanExercise.exercise_id == Exercise.id)
        .where(WorkoutPlanExercise.plan_id.in_(plan_ids))
    )).all()

    by_plan: dict[int, list] = {}
    exercise_by_id: dict[int, Exercise] = {}
    for plan_exercise, exercise in rows:
        by_plan.setdefault(plan_exercise.plan_id, []).append(plan_exercise)
        exercise_by_id[exercise.id] = exercise

    total: dict[str, MuscleContribution] = {}
    for day in days:
        compiled = [
            {"exercise_id": pe.exercise_id, "target_sets": pe.target_sets}
            for pe in by_plan.get(day.plan_id, [])
        ]
        for item in apply_adjustments(compiled, day.volume_adjustments):
            exercise = exercise_by_id.get(item["exercise_id"])
            if exercise is None:
                continue
            total = accumulate(total, contribution(
                exercise.main_muscle_group,
                exercise.secondary_muscle_groups,
                item["target_sets"],
            ))
    return total


async def performed_for(
    session: AsyncSession, app_user_id: int, window: Window
) -> dict[str, MuscleContribution]:
    """Фактический объём окна.

    Отбор по ДАТЕ СТАРТА СЕССИИ, а не по WorkoutSessionSet.updated_at:
    правка старого подхода задним числом не имеет права залетать в текущее
    окно. Свободная тренировка в день отдыха тоже считается — объём сделан,
    и мышца об этом знает.
    """
    rows = (await session.execute(
        select(
            Exercise.main_muscle_group,
            Exercise.secondary_muscle_groups,
            func.count(WorkoutSessionSet.id),
        )
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .join(
            WorkoutSession,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .join(Exercise, WorkoutSessionExercise.exercise_id == Exercise.id)
        .where(
            WorkoutSession.app_user_id == app_user_id,
            func.date(WorkoutSession.started_at) >= window.start_date,
            func.date(WorkoutSession.started_at) <= window.end_date,
            WorkoutSessionSet.is_completed.is_(True),
            WorkoutSessionSet.set_type.in_(["normal", "drop"]),
            WorkoutSessionSet.is_anomalous.is_(False),
        )
        .group_by(Exercise.main_muscle_group, Exercise.secondary_muscle_groups)
    )).all()

    total: dict[str, MuscleContribution] = {}
    for main, secondary, sets_count in rows:
        total = accumulate(total, contribution(main, secondary, int(sets_count)))
    return total


def build_rows(
    target_by_muscle: dict[str, float],
    prescribed: dict[str, MuscleContribution],
    performed: dict[str, MuscleContribution],
) -> dict[str, MuscleRow]:
    """Три ряда в одну таблицу по объединению ключей.

    Мышца, у которой есть цель, но нет ни предписания, ни факта, обязана
    остаться в таблице: недобор виден только если строка существует.
    """
    keys = set(target_by_muscle) | set(prescribed) | set(performed)
    rows: dict[str, MuscleRow] = {}
    for key in keys:
        fact = performed.get(key, MuscleContribution(0.0, 0.0))
        plan = prescribed.get(key, MuscleContribution(0.0, 0.0))
        rows[key] = MuscleRow(
            target=float(target_by_muscle.get(key, 0.0)),
            prescribed=plan.effective,
            performed_direct=fact.direct,
            performed_indirect=fact.indirect,
        )
    return rows


# [КОНФИГ] Доля дней окна, у которых должно быть предписание, чтобы окно
# считалось окном. Ниже порога это обрывок после смены сплита, и его
# «недобор» — артефакт, а не сигнал.
MIN_PRESCRIBED_DAY_RATIO = 0.5


async def close_window(
    session: AsyncSession,
    app_user_id: int,
    window: Optional[Window],
    target_by_muscle: dict[str, float],
    level: Optional[str],
) -> Optional[VolumeWindow]:
    """Заморозить окно снимком. Идемпотентна.

    Возвращает None, если окна нет или оно не дотягивает до окна по числу
    дней с предписанием.
    """
    if window is None:
        return None

    existing = (await session.execute(
        select(VolumeWindow).where(
            VolumeWindow.app_user_id == app_user_id,
            VolumeWindow.block_id == window.block_id,
            VolumeWindow.window_index == window.window_index,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    # Ни день отдыха, ни blackout не несут предписания по построению —
    # SchedulingEngine намеренно оставляет на них plan_id = None (отпуск,
    # переезд и т.п.). Знаменатель «доли дней с предписанием» должен
    # исключать оба флага той же логикой, что и adherence_for_range: иначе
    # окно с несколькими blackout-днями (отпускная неделя) штрафуется за
    # чужое решение и отвергается как обрывок после смены сплита, хотя
    # ничего подобного не произошло.
    day_rows = (await session.execute(
        select(UserCalendarDay.plan_id).where(
            UserCalendarDay.app_user_id == app_user_id,
            UserCalendarDay.target_date >= window.start_date,
            UserCalendarDay.target_date <= window.end_date,
            UserCalendarDay.is_rest_day.is_(False),
            UserCalendarDay.is_blackout.is_(False),
        )
    )).all()
    working = day_rows
    if not working:
        return None
    with_plan = sum(1 for r in working if r.plan_id is not None)
    if with_plan / len(working) < MIN_PRESCRIBED_DAY_RATIO:
        return None

    prescribed = await prescribed_for(session, app_user_id, window)
    performed = await performed_for(session, app_user_id, window)
    rows = build_rows(target_by_muscle, prescribed, performed)
    adherence = await adherence_for_range(
        session, app_user_id, window.start_date, window.end_date
    )

    landmarks_snapshot: dict[str, dict] = {}
    for muscle in rows:
        lm = landmarks_for(muscle, level)
        if lm is None:
            continue
        landmarks_snapshot[muscle] = {
            "mev": lm.mev, "mav": lm.mav, "mrv": lm.mrv,
            "mev_direct": lm.mev_direct, "mrv_direct": lm.mrv_direct,
        }

    snapshot = VolumeWindow(
        app_user_id=app_user_id,
        block_id=window.block_id,
        window_index=window.window_index,
        phase_number=window.phase_number,
        start_date=window.start_date,
        end_date=window.end_date,
        muscles={
            muscle: {
                "target": row.target,
                "prescribed": row.prescribed,
                "performed_direct": row.performed_direct,
                "performed_indirect": row.performed_indirect,
            }
            for muscle, row in rows.items()
        },
        adherence={
            "planned_days": adherence.planned_days,
            "completed_days": adherence.completed_days,
            "missed_days": adherence.missed_days,
        },
        landmarks=landmarks_snapshot,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def closed_windows(
    session: AsyncSession, app_user_id: int, limit: int
) -> list[VolumeWindow]:
    """Последние закрытые окна, новые первыми."""
    return list((await session.execute(
        select(VolumeWindow)
        .where(VolumeWindow.app_user_id == app_user_id)
        .order_by(VolumeWindow.end_date.desc())
        .limit(limit)
    )).scalars().all())


async def recompute_stale_window(
    session: AsyncSession,
    app_user_id: int,
    snapshot: VolumeWindow,
) -> Optional[VolumeWindow]:
    """Пересобрать снимок, если подходы окна правились ПОСЛЕ его создания.

    Спека §7: правка подхода задним числом пересчитывает снимок, но
    принятое по нему решение не отменяется — предложение уже
    материализовано и живёт своей жизнью.

    Возвращает снимок (обновлённый или нетронутый) либо None, если
    пересчитывать нечего.
    """
    touched_at = (await session.execute(
        select(func.max(WorkoutSessionSet.updated_at))
        .select_from(WorkoutSessionSet)
        .join(
            WorkoutSessionExercise,
            WorkoutSessionSet.workout_session_exercise_id == WorkoutSessionExercise.id,
        )
        .join(
            WorkoutSession,
            WorkoutSessionExercise.workout_session_id == WorkoutSession.id,
        )
        .where(
            WorkoutSession.app_user_id == app_user_id,
            func.date(WorkoutSession.started_at) >= snapshot.start_date,
            func.date(WorkoutSession.started_at) <= snapshot.end_date,
        )
    )).scalar_one_or_none()

    if touched_at is None or touched_at <= snapshot.created_at:
        return None

    window = Window(
        block_id=snapshot.block_id,
        window_index=snapshot.window_index,
        phase_number=snapshot.phase_number,
        start_date=snapshot.start_date,
        end_date=snapshot.end_date,
        is_deload=False,
    )
    performed = await performed_for(session, app_user_id, window)

    muscles = dict(snapshot.muscles or {})
    for muscle, row in muscles.items():
        fact = performed.get(muscle)
        row["performed_direct"] = fact.direct if fact else 0.0
        row["performed_indirect"] = fact.indirect if fact else 0.0
    snapshot.muscles = muscles
    flag_modified(snapshot, "muscles")
    return snapshot
