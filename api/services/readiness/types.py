"""Словарь понятий готовности.

Все структуры неизменяемы: ядро — чистые функции, и мутабельное
состояние в них только источник ошибок.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Optional

from api.services.readiness import params


@dataclass(frozen=True)
class CheckinSignals:
    """Сырые ответы чек-ина. Любое поле может отсутствовать."""

    sleep: Optional[int] = None                       # 1..5, выше = лучше
    stress: Optional[int] = None                      # 1..5, выше = хуже
    soreness: Mapping[str, int] = field(default_factory=dict)  # muscle -> 0..3
    pain: Mapping[str, int] = field(default_factory=dict)      # place -> 0..3
    observed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        # frozen=True блокирует только переассоединение атрибутов, но не мутацию
        # содержимого. MappingProxyType делает словари неизменяемыми.
        object.__setattr__(self, "soreness", MappingProxyType(self.soreness))
        object.__setattr__(self, "pain", MappingProxyType(self.pain))


@dataclass(frozen=True)
class MuscleFlag:
    """Крепатура по конкретной мышце."""

    muscle: str
    level: str
    reason_code: str  # soreness_caution | soreness_limit


@dataclass(frozen=True)
class ExerciseTarget:
    """Что вердикту нужно знать об упражнении. Ни одного объекта SQLAlchemy.

    main_muscle и secondary_muscles — нормализованные system keys
    (to_system_key), action — значение ExerciseAction.
    """

    exercise_id: int
    main_muscle: Optional[str] = None
    secondary_muscles: tuple[str, ...] = ()
    action: str = "unknown"


@dataclass(frozen=True)
class ExerciseReadiness:
    """Уровень готовности, разрешённый для одного упражнения."""

    level: str = params.LEVEL_OK
    source: Optional[str] = None  # pain | soreness | global


@dataclass(frozen=True)
class ReadinessVerdict:
    """Свёртка чек-ина. Два потребителя: потолок веса и подрезка объёма."""

    level: str
    reason_code: str
    reason_text: str
    muscle_flags: tuple[MuscleFlag, ...] = ()
    pain_places: tuple[str, ...] = ()
    completeness: str = params.COMPLETENESS_PARTIAL
    observed_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reason_code": self.reason_code,
            "reason_text": self.reason_text,
            "muscle_flags": [
                {"muscle": f.muscle, "level": f.level,
                 "reason_code": f.reason_code}
                for f in self.muscle_flags
            ],
            "pain_places": list(self.pain_places),
            "completeness": self.completeness,
            "observed_at": (
                None if self.observed_at is None else self.observed_at.isoformat()
            ),
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "ReadinessVerdict":
        observed = raw.get("observed_at")
        return ReadinessVerdict(
            level=str(raw["level"]),
            reason_code=str(raw["reason_code"]),
            reason_text=str(raw.get("reason_text", "")),
            muscle_flags=tuple(
                MuscleFlag(
                    muscle=str(f["muscle"]),
                    level=str(f["level"]),
                    reason_code=str(f["reason_code"]),
                )
                for f in raw.get("muscle_flags", [])
            ),
            pain_places=tuple(str(p) for p in raw.get("pain_places", [])),
            completeness=str(
                raw.get("completeness", params.COMPLETENESS_PARTIAL)
            ),
            observed_at=(
                None if observed is None else datetime.fromisoformat(observed)
            ),
        )
