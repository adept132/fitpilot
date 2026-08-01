from __future__ import annotations
import enum
from dataclasses import dataclass, field


class CommandType(str, enum.Enum):
    EXCLUDE_EXERCISE = "EXCLUDE_EXERCISE"
    REPLACE_EXERCISE = "REPLACE_EXERCISE"
    ADJUST_MUSCLE_SETS = "ADJUST_MUSCLE_SETS"
    ACCENT_MUSCLE = "ACCENT_MUSCLE"
    ADD_MUSCLE = "ADD_MUSCLE"
    SET_ALL_SETS = "SET_ALL_SETS"
    SCALE_VOLUME = "SCALE_VOLUME"
    EQUIPMENT_CONSTRAINT = "EQUIPMENT_CONSTRAINT"
    ADD_INJURY = "ADD_INJURY"
    SET_BASE_ISO_RATIO = "SET_BASE_ISO_RATIO"
    CLARIFY = "CLARIFY"


@dataclass
class Command:
    type: CommandType
    params: dict = field(default_factory=dict)
    seed: int = 0
    confidence: float = 1.0
    summary: str = ""

    def to_dict(self) -> dict:
        return {"type": self.type.value, "params": self.params, "seed": self.seed,
                "confidence": self.confidence, "summary": self.summary}

    @classmethod
    def from_dict(cls, d: dict) -> "Command":
        return cls(type=CommandType(d["type"]), params=d.get("params", {}),
                   seed=d.get("seed", 0), confidence=d.get("confidence", 1.0),
                   summary=d.get("summary", ""))
