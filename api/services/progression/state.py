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
    """
    if prescription is None or not prescription.sets:
        return True
    return prescription.sets[0].weight_kg is not None


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
    """Вердикт по сессии. Приоритет: no_basis, deviated, miss, overshoot, hit."""
    if prescription is None or not prescription.sets:
        return Outcome(status="no_basis")

    candidates = working_sets(facts, _requires_weight(prescription))
    if not candidates:
        return Outcome(status="skipped")

    usable = [s for s in candidates if not s.is_anomalous]
    if not usable:
        return Outcome(status="no_basis")

    hit = miss = deviated = overshoot = 0
    achieved: Optional[float] = None

    for s in usable:
        sp = _prescription_for(prescription, s.set_number)

        if s.weight_kg is not None:
            value = e1rm(float(s.weight_kg), int(s.reps), int(s.rir))
            achieved = value if achieved is None else max(achieved, value)

        if sp.weight_kg is not None and abs(float(s.weight_kg) - sp.weight_kg) > step_kg:
            deviated += 1
            continue
        if s.reps < sp.rep_min:
            miss += 1
            continue
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
    elif overshoot and overshoot == hit:
        # overshoot выставляется, только если ВСЕ засчитанные подходы ушли
        # выше rep_max. Смешанный случай (часть выше потолка, часть в
        # диапазоне) — это "hit": частичный перебор не даёт достаточных
        # оснований для ускоренного роста веса на всех подходах.
        status = "overshoot"
    else:
        status = "hit"

    return Outcome(
        status=status,
        hit_sets=hit,
        miss_sets=miss,
        total_sets=total,
        achieved_e1rm=achieved,
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
