"""Оценка результата и восстановление состояния прогрессии.

evaluate() сравнивает факт с предписанием. Ключевая асимметрия (спека §8.1):
rep_max — планка повышения веса, rep_min — планка провала. Недобор до
растянутой цели при взятом rep_min провалом не считается.
"""

from __future__ import annotations

from typing import Optional, Sequence

from api.services.progression import params
from api.services.progression.metrics import e1rm
from api.services.progression.types import (
    ExerciseHistory,
    Outcome,
    Prescription,
    ProgressionState,
    SessionFact,
    SetFact,
    SetPrescription,
)


def working_sets(facts: Sequence[SetFact], require_weight: bool = True) -> list[SetFact]:
    """Подходы, пригодные для оценки: без разминки, дропов и пустых значений.

    Аномальные оставляем — их отсеивает evaluate(), которому нужно отличить
    «все подходы аномальны» от «подходов не было».

    require_weight=False — для упражнений со своим весом (предписание не
    задаёт weight_kg): отсутствие веса в факте там легитимно, а не пропуск
    в логе. Дефолт True сохраняет прежнее поведение для внешних вызовов.
    """
    result = []
    for s in facts:
        if (s.set_type or "normal").lower() in params.IGNORED_SET_TYPES:
            continue
        if require_weight and s.weight_kg is None:
            continue
        if s.reps is None:
            continue
        if s.reps <= 0:
            continue
        result.append(s)
    return result


def _requires_weight(prescription: Optional[Prescription]) -> bool:
    """Нужен ли вес в факте, чтобы подход считался валидным.

    Решение по предписанию, а не по факту: предписание само знает, ожидался
    ли вес. Если оно задаёт вес — факт без веса это забытый лог (require
    True). Если предписание осознанно без веса (упражнение со своим весом,
    weight_kg=None) — отсутствие веса в факте легитимно (require False).
    Предписания нет вовсе — безопасный дефолт True, как было раньше.

    P0-06 C3: смотрим на ВСЕ подходы предписания, а не только на первый
    (sets[0]). working_sets() принимает один общий флаг require_weight на
    весь список фактов сессии — он не умеет требовать вес выборочно, по
    set_number. Раньше при смешанном предписании (первый подход без веса,
    остальные с весом) sets[0].weight_kg is None давал require_weight=False,
    working_sets() пропускал факты без веса дальше в evaluate(), и там
    строка `abs(float(s.weight_kg) - sp.weight_kg)` падала TypeError — для
    подхода с фактическим весом=None, но sp (предписание ЭТОГО set_number)
    ожидающим вес. Требуем вес, если он нужен ХОТЯ БЫ для одного подхода:
    это самый безопасный вариант при одном общем флаге на всю сессию — факты
    без веса в такой сессии просто не попадут в оценку (see working_sets()),
    вместо падения. Для честно безвесового упражнения (все sets без веса)
    поведение не меняется: any(...) даёт False, как и раньше sets[0].
    """
    if prescription is None or not prescription.sets:
        return True
    return any(sp.weight_kg is not None for sp in prescription.sets)


def _prescription_for(prescription: Prescription, set_number: int) -> SetPrescription:
    """Предписание на подход. Лишние подходы берут цель последнего известного.

    Вызывающая сторона (evaluate) гарантирует непустой prescription.sets —
    иначе она возвращает no_basis раньше, до вызова этой функции.
    """
    for sp in prescription.sets:
        if sp.set_number == set_number:
            return sp
    return prescription.sets[-1]


def evaluate(
    prescription: Optional[Prescription],
    facts: Sequence[SetFact],
    step_kg: float,
) -> Outcome:
    """Вердикт по сессии. Приоритет: no_basis, deviated, miss, strained, overshoot, hit."""
    if prescription is None or not prescription.sets:
        return Outcome(status="no_basis")

    candidates = working_sets(facts, _requires_weight(prescription))
    if not candidates:
        return Outcome(status="skipped")

    usable = [s for s in candidates if not s.is_anomalous]
    if not usable:
        return Outcome(status="no_basis")

    hit = miss = deviated = overshoot = strained = 0
    achieved: Optional[float] = None

    for s in usable:
        sp = _prescription_for(prescription, s.set_number)
        if sp is None:
            continue

        if s.weight_kg is not None:
            value = e1rm(float(s.weight_kg), int(s.reps), int(s.rir))
            achieved = value if achieved is None else max(achieved, value)

        # P0-06 C3: доп. защита от TypeError (float() argument ... NoneType) —
        # _requires_weight() выше уже не должна пропускать сюда факт без веса,
        # когда предписание ЭТОГО set_number его ожидает (см. её докстринг),
        # но working_sets() фильтрует по ОДНОМУ флагу на всю сессию, а не по
        # set_number. s.weight_kg is not None — вторая, независимая гарантия
        # на случай прескрипшена, не покрытого этим инвариантом.
        if sp.weight_kg is not None and s.weight_kg is not None and abs(float(s.weight_kg) - sp.weight_kg) > step_kg:
            deviated += 1
            continue
        if s.reps < sp.rep_min:
            miss += 1
            continue

        # P0-07 §8.1: цель взята — но какой ценой. Условие узкое: работа на
        # нуле там, где запас предписан. Предписанный RIR 1 или AMRAP с
        # RIR 0 сигналом не считаются, это штатный режим схемы.
        if int(sp.rir) >= params.STRAIN_MIN_PRESCRIBED_RIR and int(s.rir) <= 0:
            strained += 1

        if sp.rep_max is not None and s.reps > sp.rep_max:
            overshoot += 1
            hit += 1
            continue
        hit += 1

    total = hit + miss + deviated

    if deviated:
        status = "deviated"
    elif miss:
        status = "miss"
    elif strained and strained >= total * params.STRAIN_SET_RATIO:
        # Последний подход до отказа — обычная практика; сигналом считаем
        # только когда так прошла половина рабочих подходов и больше.
        status = "strained"
    elif overshoot and overshoot == hit:
        status = "overshoot"
    else:
        status = "hit"

    return Outcome(
        status=status,
        hit_sets=hit,
        miss_sets=miss,
        total_sets=total,
        achieved_e1rm=achieved,
        strained_sets=strained,
    )


def _measured_e1rm(usable: Sequence[SetFact]) -> Optional[float]:
    """Максимальный e1RM по подходам, где вес фактически известен.

    Для упражнения со своим весом usable может быть непустым (сессия
    выполнена), но без единого веса e1RM посчитать нечем — тогда None:
    это не пропуск сессии, а честное «неизмеримо».
    """
    measured = [s for s in usable if s.weight_kg is not None]
    if not measured:
        return None
    return max(e1rm(float(s.weight_kg), int(s.reps), int(s.rir)) for s in measured)


def rebuild_state(history: ExerciseHistory, step_kg: float) -> ProgressionState:
    """Полное восстановление состояния из истории.

    Единственный источник истины: кэш-таблица только материализует результат
    этой функции, поэтому любое расхождение чинится пересчётом.
    """
    # Внутри удобнее хронологический порядок; снаружи история от новой к старой.
    chronological = list(reversed(history.sessions))

    best_ever: Optional[float] = None
    completed = 0
    consecutive_misses = 0
    sessions_since_gain = 0
    working: Optional[float] = None
    last_top: Optional[float] = None
    last_scheme: Optional[str] = None

    for session in chronological:
        require_weight = _requires_weight(session.prescription)
        usable = [s for s in working_sets(session.sets, require_weight) if not s.is_anomalous]
        if not usable:
            # Пропущенная сессия: нет ни одного пригодного подхода —
            # ни счётчики, ни метрики не двигаются.
            continue

        completed += 1
        if session.prescription is not None:
            # `or` здесь неверен: top_weight == 0.0 — валидное предписание
            # (например, безопасное упражнение с нулевым весом), а `or`
            # считает 0.0 ложным и молча оставляет last_top от старой сессии.
            top = session.prescription.top_weight
            if top is not None:
                last_top = top
            last_scheme = session.prescription.scheme

        outcome = evaluate(session.prescription, session.sets, step_kg)
        if outcome.status == "miss":
            consecutive_misses += 1
        elif outcome.status in ("hit", "overshoot"):
            consecutive_misses = 0

        value = _measured_e1rm(usable)
        if value is None:
            # Сессия выполнена (подходы были), но e1RM неизмерим — свой вес
            # без отягощения. Прирост силы без массы тела не вывести, а
            # значит нельзя ни двигать working_e1rm/best_ever/training_max,
            # ни считать это застоем или прогрессом.
            continue

        working = value

        if session.is_deload:
            # Разгрузка не обязана расти — в счёт застоя не идёт.
            continue

        if best_ever is None:
            best_ever = value
        elif value > best_ever * params.PLATEAU_GAIN_TOLERANCE:
            best_ever = value
            sessions_since_gain = 0
        else:
            best_ever = max(best_ever, value)
            sessions_since_gain += 1

    stalled = (
        completed >= params.PLATEAU_MIN_SESSIONS
        and sessions_since_gain >= params.PLATEAU_STALL_SESSIONS
    )

    return ProgressionState(
        working_e1rm=working,
        training_max=None if working is None else working * params.TRAINING_MAX_RATIO,
        best_e1rm_ever=best_ever,
        consecutive_misses=consecutive_misses,
        sessions_since_gain=sessions_since_gain,
        last_top_weight=last_top,
        last_scheme=last_scheme,
        stalled=stalled,
        completed_sessions=completed,
    )
