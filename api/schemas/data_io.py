"""Схемы импорта/экспорта данных."""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ImportPreviewRequest(BaseModel):
    # Содержимое CSV передаётся в JSON-теле, а не multipart-файлом: RN не умеет
    # надёжно стримить выбранный content:// файл в multipart ("Network request
    # failed"). Клиент читает файл в строку и присылает её сюда.
    csv: str
    unit: Optional[str] = None


class ImportCommitRequest(ImportPreviewRequest):
    # {"Bench Press (Barbell)": 12} — ручное сопоставление имени с id упражнения.
    mappings: Optional[Dict[str, int]] = None


class ExerciseSuggestion(BaseModel):
    id: int
    name: str
    main_muscle_group: Optional[str] = None
    similarity: float


class UnmatchedExercise(BaseModel):
    """Упражнение из файла, которое не удалось сопоставить автоматически."""
    name: str
    equipment: List[str] = Field(default_factory=list)
    sets_count: int
    suggestions: List[ExerciseSuggestion] = Field(default_factory=list)


class SkippedRowOut(BaseModel):
    line: int
    reason: str


class ImportPreviewResponse(BaseModel):
    # Единица из колонки Weight Unit. Если None — спрашиваем пользователя.
    detected_unit: Optional[str] = None
    needs_unit_choice: bool = False

    workouts_total: int = 0
    workouts_new: int = 0
    workouts_duplicate: int = 0
    sets_total: int = 0

    matched_exercises: int = 0
    unmatched: List[UnmatchedExercise] = Field(default_factory=list)
    skipped_rows: List[SkippedRowOut] = Field(default_factory=list)
    skipped_rows_total: int = 0


class ImportCommitResponse(BaseModel):
    added_workouts: int = 0
    skipped_duplicates: int = 0
    skipped_exercises: int = 0
    saved_aliases: int = 0
    unit_used: str = "kg"
