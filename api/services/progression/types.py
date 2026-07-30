"""Словарь понятий движка прогрессии.

Все структуры неизменяемы: ядро — чистые функции, и мутабельное состояние
в них только источник ошибок.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

ENGINE_VERSION = 2


@dataclass(frozen=True)
class SetFact:
    """Факт одного выполненного подхода."""

    set_number: int
    weight_kg: Optional[float]
    reps: int
    rir: int
    set_type: str = "normal"
    is_anomalous: bool = False


@dataclass(frozen=True)
class SetPrescription:
    """Предписание на один подход. rep_max=None — открытый верх (AMRAP)."""

    set_number: int
    weight_kg: Optional[float]
    rep_min: int
    rep_max: Optional[int]
    rir: int
    kind: str = "normal"  # normal | amrap | backoff

    def to_dict(self) -> dict[str, Any]:
        return {
            "set_number": self.set_number,
            "weight_kg": self.weight_kg,
            "rep_min": self.rep_min,
            "rep_max": self.rep_max,
            "rir": self.rir,
            "kind": self.kind,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "SetPrescription":
        return SetPrescription(
            set_number=int(raw["set_number"]),
            weight_kg=None if raw.get("weight_kg") is None else float(raw["weight_kg"]),
            rep_min=int(raw["rep_min"]),
            rep_max=None if raw.get("rep_max") is None else int(raw["rep_max"]),
            rir=int(raw["rir"]),
            kind=str(raw.get("kind", "normal")),
        )


@dataclass(frozen=True)
class Prescription:
    """Что движок предписал на упражнение. Сериализуется в JSONB."""

    scheme: str
    sets: tuple[SetPrescription, ...]
    reason_code: str
    reason_text: str
    basis: dict[str, Any] = field(default_factory=dict)
    engine_version: int = ENGINE_VERSION
    provisional: bool = False
    # --- P0-07: подрезка объёма ---
    # Отдельно от reason_code намеренно: иначе причина объёма затирала бы
    # причину веса, и пользователь с крепатурой перестал бы видеть, что у
    # него вдобавок плато.
    volume_delta: int = 0
    volume_reason_code: Optional[str] = None
    volume_reason_text: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "sets": [s.to_dict() for s in self.sets],
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "basis": dict(self.basis),
            "engine_version": self.engine_version,
            "provisional": self.provisional,
            "volume_delta": self.volume_delta,
            "volume_reason_code": self.volume_reason_code,
            "volume_reason_text": self.volume_reason_text,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Prescription":
        return Prescription(
            scheme=str(raw["scheme"]),
            sets=tuple(SetPrescription.from_dict(s) for s in raw.get("sets", [])),
            reason_code=str(raw["reason_code"]),
            reason_text=str(raw.get("reason_text", "")),
            basis=dict(raw.get("basis") or {}),
            engine_version=int(raw.get("engine_version", ENGINE_VERSION)),
            provisional=bool(raw.get("provisional", False)),
            volume_delta=int(raw.get("volume_delta", 0) or 0),
            volume_reason_code=raw.get("volume_reason_code"),
            volume_reason_text=raw.get("volume_reason_text"),
        )

    @property
    def top_weight(self) -> Optional[float]:
        """Максимальный предписанный вес — якорь для схем и снижения."""
        weights = [s.weight_kg for s in self.sets if s.weight_kg is not None]
        return max(weights) if weights else None


@dataclass(frozen=True)
class SessionFact:
    """Одна сессия с упражнением: что предписали и что сделали."""

    session_id: int
    finished_at: Optional[datetime]
    prescription: Optional[Prescription]
    sets: tuple[SetFact, ...]
    is_deload: bool = False


@dataclass(frozen=True)
class ExerciseHistory:
    """Последние N сессий с упражнением, от новой к старой."""

    exercise_id: int
    sessions: tuple[SessionFact, ...] = ()


@dataclass(frozen=True)
class Outcome:
    """Вердикт по прошлой сессии.

    status: hit | miss | deviated | strained | overshoot | skipped | no_basis
    """

    status: str
    hit_sets: int = 0
    miss_sets: int = 0
    total_sets: int = 0
    achieved_e1rm: Optional[float] = None
    # P0-07: сколько рабочих подходов сделано в упор — вес и повторы взяты,
    # но ценой отказа там, где запас был предписан.
    strained_sets: int = 0


@dataclass(frozen=True)
class ProgressionState:
    """Производное состояние. Целиком восстанавливается rebuild_state()."""

    working_e1rm: Optional[float] = None
    training_max: Optional[float] = None
    best_e1rm_ever: Optional[float] = None
    consecutive_misses: int = 0
    sessions_since_gain: int = 0
    last_top_weight: Optional[float] = None
    last_scheme: Optional[str] = None
    stalled: bool = False
    completed_sessions: int = 0


@dataclass(frozen=True)
class SchemeContext:
    """Всё, что нужно схеме. Ни одного объекта SQLAlchemy."""

    history: ExerciseHistory
    state: ProgressionState
    last_outcome: Optional[Outcome]
    target_sets: int
    rep_min: int
    rep_max: int
    rep_range_source: str
    target_rir: int
    equipment: tuple[str, ...] = ()
    unit: str = "kg"
    weight_steps: dict[str, Any] = field(default_factory=dict)
    experience_level: str = "beginner"
    fatigue_tier: int = 2
    main_muscle_group: Optional[str] = None
    phase_effort_tier: str = "medium"
    days_since_last_session: Optional[int] = None
    settings: dict[str, Any] = field(default_factory=dict)
    # --- P0-07 ---
    # Уровень готовности, УЖЕ разрешённый для этого упражнения
    # (readiness.verdict.level_for_exercise). Ядро движка не знает ни про
    # JOINT_IMPACT, ни про мышцы: сюда приходит готовый скаляр.
    readiness_level: str = "ok"
    readiness_source: Optional[str] = None  # pain | soreness | global
    # Самая свежая сессия с упражнением была без залогированных подходов.
    # Отдельно от last_outcome: _latest_outcome намеренно пролистывает
    # пропуски в поисках последней результативной сессии, и менять его
    # семантику ради одного правила нельзя.
    last_session_skipped: bool = False
