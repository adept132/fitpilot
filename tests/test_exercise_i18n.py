from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from api.routers.exercises import create_custom_exercise
from api.schemas.exercises import (
    CustomExerciseCreate,
    ExerciseDetailResponse,
    ExerciseListItemResponse,
    ExerciseSearchItem,
)
from api.schemas.goals import GoalResponse, GoalStatus
from api.schemas.plan import GeneratedExerciseOut
from api.schemas.progress import ProgressAchievement
from api.schemas.supersets import WorkoutStructureExerciseItem
from api.schemas.workouts import ExerciseShortResponse
from api.services.exercise_matcher import ExerciseMatcher
from api.services.exercise_search_service import ExerciseSearchService
from api.services.models import Exercise
from api.services.exercise_selection_engine import SelectedExercise
from api.services.plan_duration import DurationConfig, fit_to_duration


ROOT = Path(__file__).resolve().parents[1]


def _localization_module():
    try:
        return importlib.import_module("api.services.exercise_localization")
    except ModuleNotFoundError:
        pytest.fail("exercise localization helpers are missing")


def _backfill_module():
    try:
        return importlib.import_module("scripts.backfill_exercise_localizations")
    except ModuleNotFoundError:
        pytest.fail("exercise localization backfill is missing")


def _load_migration():
    path = ROOT / "migrations" / "versions" / "20260830_02_exercise_localizations.py"
    if not path.exists():
        pytest.fail("exercise localization migration is missing")
    spec = importlib.util.spec_from_file_location("exercise_i18n_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _exercise(**updates):
    values = {
        "id": 76,
        "name": "Жим лёжа",
        "name_en": "Bench Press",
        "description": "Русское описание",
        "description_en": "Press the bar from the chest with control.",
        "source": "default",
        "category": "base",
        "main_muscle_group": "Грудь",
        "secondary_muscle_groups": [],
        "equipment_needed": ["barbell"],
        "difficulty": "beginner",
        "fatigue_tier": 1,
        "image_urls": [],
        "image_approx": False,
    }
    values.update(updates)
    return SimpleNamespace(**values)


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SequenceSession:
    def __init__(self, *row_sets):
        self._row_sets = list(row_sets)

    async def execute(self, _statement):
        return _RowsResult(self._row_sets.pop(0))


class _BackfillSession(_SequenceSession):
    def __init__(self, rows, *, fail_commit=False):
        super().__init__(rows)
        self.rows = list(rows)
        self.fail_commit = fail_commit
        self.commit_calls = 0
        self.rollback_calls = 0
        self._before = {
            row.id: (row.name_en, row.description_en) for row in self.rows
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def commit(self):
        self.commit_calls += 1
        if self.fail_commit:
            raise RuntimeError("commit failed")

    async def rollback(self):
        self.rollback_calls += 1
        for row in self.rows:
            row.name_en, row.description_en = self._before[row.id]


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _CustomExerciseSession:
    def __init__(self):
        self.execute_calls = 0
        self.added = None

    async def execute(self, _statement):
        self.execute_calls += 1
        if self.execute_calls <= 2:
            return _ScalarResult(None)
        return _ScalarResult(self.added)

    def add(self, exercise):
        self.added = exercise

    async def commit(self):
        return None

    async def refresh(self, exercise):
        exercise.id = 901
        exercise.image_approx = False


def test_exercise_model_declares_nullable_english_columns():
    assert Exercise.__table__.c.name_en.type.length == 200
    assert Exercise.__table__.c.name_en.nullable is True
    assert Exercise.__table__.c.description_en.nullable is True


def test_exercise_schemas_preserve_legacy_fields_and_add_localized_maps():
    item = ExerciseListItemResponse(
        id=76,
        name="Жим лёжа",
        localized_names={"ru": "Жим лёжа", "en": "Bench Press"},
        category="base",
        main_muscle_group="Грудь",
        difficulty="beginner",
        equipment_needed=["barbell"],
        fatigue_tier=1,
        source="default",
    )
    detail = ExerciseDetailResponse(
        id=76,
        name="Жим лёжа",
        description="Русское описание",
        localized_names={"ru": "Жим лёжа", "en": "Bench Press"},
        localized_descriptions={
            "ru": "Русское описание",
            "en": "Press the bar from the chest with control.",
        },
        category="base",
        main_muscle_group="Грудь",
        secondary_muscle_groups=[],
        equipment_needed=["barbell"],
        difficulty="beginner",
    )

    assert item.model_dump()["name"] == "Жим лёжа"
    assert item.model_dump()["localized_names"]["en"] == "Bench Press"
    assert detail.model_dump()["description"] == "Русское описание"
    assert detail.model_dump()["localized_descriptions"]["en"].startswith("Press")


def test_embedded_exercise_schema_builds_maps_from_orm_without_replacing_name():
    system = _exercise(
        category="base",
        main_muscle_group="Грудь",
        secondary_muscle_groups=[],
        equipment_needed=["barbell"],
        fatigue_tier=1,
        image_urls=[],
        image_approx=False,
    )

    response = ExerciseShortResponse.model_validate(system, from_attributes=True)

    assert response.name == "Жим лёжа"
    assert response.localized_names == {"ru": "Жим лёжа", "en": "Bench Press"}
    assert response.localized_descriptions["en"].startswith("Press")


def test_named_consumer_schemas_carry_localized_maps_additively():
    names = {"ru": "Жим лёжа", "en": "Bench Press"}
    goal = GoalResponse(
        id=1,
        goal_type="strength",
        target_value=100,
        exercise_id=76,
        exercise_name="Жим лёжа",
        localized_names=names,
        is_completed=False,
        status=GoalStatus(),
    )
    generated = GeneratedExerciseOut(
        exercise_id=76,
        name="Жим лёжа",
        localized_names=names,
        target_sets=3,
        order_index=0,
        fatigue_tier=1,
        primary_muscle="Грудь",
    )
    achievement = ProgressAchievement(
        id="1:76:e1rm",
        exercise_id=76,
        exercise_name="Жим лёжа",
        localized_names=names,
        e1rm=100,
        weight=80,
        reps=8,
        achieved_at=__import__("datetime").datetime(2026, 8, 30),
        workout_id=1,
    )
    structure = WorkoutStructureExerciseItem(
        session_exercise_id=1,
        order_index=0,
        exercise_id=76,
        exercise_name="Жим лёжа",
        localized_names=names,
        sets_count=0,
        volume_total=0,
    )

    assert goal.exercise_name == generated.name == achievement.exercise_name == "Жим лёжа"
    assert goal.localized_names == generated.localized_names == names
    assert achievement.localized_names == structure.localized_names == names


def test_duration_refit_rehydrates_display_name_from_canonical_exercise_id():
    selected = SelectedExercise(
        exercise_id=76,
        name="Client supplied name",
        sets=3,
        order_index=0,
        superset_group_id=None,
        fatigue_tier=1,
        primary_muscle="Грудь",
        secondary_muscle=None,
    )
    canonical = _exercise(
        equipment_needed=["barbell"],
        action="horizontal_press",
    )

    result = fit_to_duration(
        [selected], {76: canonical}, None, DurationConfig()
    )

    assert result.exercises[0].exercise_id == 76
    assert result.exercises[0].name == "Жим лёжа"


def test_system_exercise_exposes_both_locales_without_replacing_legacy_text():
    localization = _localization_module()
    exercise = _exercise()

    assert localization.localized_names(exercise) == {
        "ru": "Жим лёжа",
        "en": "Bench Press",
    }
    assert localization.localized_descriptions(exercise) == {
        "ru": "Русское описание",
        "en": "Press the bar from the chest with control.",
    }
    assert exercise.name == "Жим лёжа"
    assert exercise.description == "Русское описание"


def test_custom_exercise_never_exposes_or_selects_english_shadow_fields():
    localization = _localization_module()
    custom = _exercise(
        id=900,
        name="Мой жим",
        name_en="Must Not Leak",
        description="Моя техника",
        description_en="Must Not Leak",
        source="custom",
    )

    assert localization.localized_names(custom) == {"ru": "Мой жим"}
    assert localization.localized_descriptions(custom) == {"ru": "Моя техника"}
    assert localization.display_name(custom, "en") == "Мой жим"
    assert localization.display_description(custom, "en") == "Моя техника"


def test_english_presentation_has_deterministic_russian_fallback():
    localization = _localization_module()
    system = _exercise(name_en=None, description_en=None)

    assert localization.display_name(system, "en") == "Жим лёжа"
    assert localization.display_description(system, "en") == "Русское описание"


def test_matcher_compares_both_system_names_but_only_custom_original_name():
    system = _exercise()
    custom = _exercise(
        id=901,
        name="Мой жим",
        name_en="Bench Press",
        source="custom",
    )

    assert ExerciseMatcher._name_similarity("bench", system) > 0.5
    assert ExerciseMatcher._name_similarity("bench", custom) < 0.5


def test_matcher_exact_clause_queries_russian_and_system_english_names():
    sql = str(
        ExerciseMatcher._exact_name_clause("bench").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "exercises.name ILIKE '%%bench%%'" in sql
    assert "exercises.name_en ILIKE '%%bench%%'" in sql
    assert "exercises.source = 'default'" in sql


def test_localized_sort_uses_display_name_then_canonical_id():
    localization = _localization_module()
    rows = [
        _exercise(id=4, name="Бета", name_en="Alpha"),
        _exercise(id=2, name="Альфа", name_en=None),
        _exercise(id=3, name="Гамма", name_en="Alpha"),
        _exercise(id=1, name="Custom Z", name_en="A", source="custom"),
    ]

    assert [row.id for row in localization.sort_exercises(rows, "en")] == [
        3,
        4,
        1,
        2,
    ]
    assert [row.id for row in localization.sort_exercises(rows, "ru")] == [
        1,
        2,
        4,
        3,
    ]


def test_migration_is_additive_and_reversible_after_task14_head(monkeypatch):
    migration = _load_migration()
    calls = []

    class Recorder:
        def add_column(self, table, column):
            calls.append(("add_column", table, column.name, column.nullable))

        def create_index(self, name, table, columns, unique=False):
            calls.append(("create_index", name, table, tuple(columns), unique))

        def drop_index(self, name, table_name=None):
            calls.append(("drop_index", name, table_name))

        def drop_column(self, table, column):
            calls.append(("drop_column", table, column))

    monkeypatch.setattr(migration, "op", Recorder())
    migration.upgrade()

    assert migration.down_revision == "20260830_01"
    assert calls == [
        ("add_column", "exercises", "name_en", True),
        ("add_column", "exercises", "description_en", True),
        (
            "create_index",
            "ix_exercises_name_en",
            "exercises",
            ("name_en",),
            False,
        ),
    ]

    calls.clear()
    migration.downgrade()
    assert calls == [
        ("drop_index", "ix_exercises_name_en", "exercises"),
        ("drop_column", "exercises", "description_en"),
        ("drop_column", "exercises", "name_en"),
    ]


def test_reviewed_translation_catalog_covers_every_known_system_id():
    backfill = _backfill_module()
    catalog = backfill.load_translations(
        ROOT / "api" / "data" / "exercise_localizations_en.json"
    )

    assert set(catalog) == set(range(76, 270))
    assert len(catalog) == 194
    assert all(item.name.strip() and "_" not in item.name for item in catalog.values())
    assert all(item.description.strip() for item in catalog.values())


def test_backfill_plan_is_idempotent_and_never_writes_custom_exercises():
    backfill = _backfill_module()
    translations = {
        76: backfill.Translation("Cable Shrug", "Shrug the shoulders under control."),
        77: backfill.Translation("Barbell Shrug", "Shrug the shoulders under control."),
    }
    rows = [
        _exercise(id=76, name_en=None, description_en=None),
        _exercise(
            id=77,
            name_en="Barbell Shrug",
            description_en="Shrug the shoulders under control.",
        ),
        _exercise(id=901, source="custom", name_en=None, description_en=None),
        _exercise(id=999, source="default", name_en=None, description_en=None),
    ]

    plan = backfill.plan_backfill(rows, translations)

    assert [(item.exercise_id, item.name_en) for item in plan.updates] == [
        (76, "Cable Shrug")
    ]
    assert plan.missing_translation_ids == (999,)
    assert plan.custom_ids_skipped == (901,)


def test_translation_loader_rejects_noncanonical_normalized_ids(tmp_path):
    """Catches an alternate key spelling targeting a canonical exercise ID."""
    backfill = _backfill_module()
    translations_path = tmp_path / "translations.json"
    translations_path.write_text(
        json.dumps({
            "76": {"name": "Cable Shrug", "description": "First."},
            "076": {"name": "Other Shrug", "description": "Second."},
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="noncanonical exercise id: '076'"):
        backfill.load_translations(translations_path)


@pytest.mark.asyncio
async def test_literal_duplicate_catalog_key_fails_before_session_open(
    tmp_path, monkeypatch
):
    """Catches the JSON decoder collapsing an exact duplicate ID key."""
    backfill = _backfill_module()
    translations_path = tmp_path / "translations.json"
    translations_path.write_text(
        """{
          "76": {"name": "First", "description": "One."},
          "76": {"name": "Second", "description": "Two."}
        }""",
        encoding="utf-8",
    )
    opened = 0

    def session_factory():
        nonlocal opened
        opened += 1
        return _BackfillSession([])

    monkeypatch.setattr(backfill, "SessionLocal", session_factory)

    with pytest.raises(ValueError, match="duplicate exercise id key: '76'"):
        await backfill._run(True, translations_path)

    assert opened == 0


@pytest.mark.parametrize("raw_id", ["076", "0", "-1", "+76", " 76"])
@pytest.mark.asyncio
async def test_noncanonical_catalog_id_fails_before_session_open(
    raw_id, tmp_path, monkeypatch
):
    """Catches permissive integer conversion accepting unstable catalog keys."""
    backfill = _backfill_module()
    translations_path = tmp_path / "translations.json"
    translations_path.write_text(
        json.dumps({
            raw_id: {"name": "Cable Shrug", "description": "Controlled shrug."}
        }),
        encoding="utf-8",
    )
    opened = 0

    def session_factory():
        nonlocal opened
        opened += 1
        return _BackfillSession([])

    monkeypatch.setattr(backfill, "SessionLocal", session_factory)

    with pytest.raises(ValueError, match="noncanonical exercise id"):
        await backfill._run(True, translations_path)

    assert opened == 0


@pytest.mark.asyncio
async def test_overlength_english_name_fails_before_session_open(
    tmp_path, monkeypatch
):
    """Catches values longer than Exercise.name_en reaching ORM mutation."""
    backfill = _backfill_module()
    translations_path = tmp_path / "translations.json"
    translations_path.write_text(
        json.dumps({
            "76": {"name": "N" * 201, "description": "Controlled shrug."}
        }),
        encoding="utf-8",
    )
    opened = 0

    def session_factory():
        nonlocal opened
        opened += 1
        return _BackfillSession([])

    monkeypatch.setattr(backfill, "SessionLocal", session_factory)

    with pytest.raises(ValueError, match="English name exceeds 200 characters"):
        await backfill._run(True, translations_path)

    assert opened == 0


@pytest.mark.asyncio
async def test_apply_with_missing_system_translation_changes_nothing(
    tmp_path, monkeypatch
):
    """Catches partial writes before full system-catalog validation."""
    backfill = _backfill_module()
    translations_path = tmp_path / "translations.json"
    translations_path.write_text(
        json.dumps({
            "76": {
                "name": "Cable Shrug",
                "description": "Shrug the shoulders under control.",
            }
        }),
        encoding="utf-8",
    )
    known = _exercise(id=76, name_en=None, description_en=None)
    newly_added = _exercise(id=999, name_en=None, description_en=None)
    session = _BackfillSession([known, newly_added])
    monkeypatch.setattr(backfill, "SessionLocal", lambda: session)

    result = await backfill._run(True, translations_path)

    assert result == 1
    assert (known.name_en, known.description_en) == (None, None)
    assert (newly_added.name_en, newly_added.description_en) == (None, None)
    assert session.commit_calls == 0
    assert session.rollback_calls == 1


@pytest.mark.asyncio
async def test_apply_rolls_back_all_fields_when_commit_fails(tmp_path, monkeypatch):
    """Catches an unexpected persistence error leaving dirty ORM state behind."""
    backfill = _backfill_module()
    translations_path = tmp_path / "translations.json"
    translations_path.write_text(
        json.dumps({
            "76": {
                "name": "Cable Shrug",
                "description": "Shrug the shoulders under control.",
            }
        }),
        encoding="utf-8",
    )
    exercise = _exercise(id=76, name_en=None, description_en=None)
    session = _BackfillSession([exercise], fail_commit=True)
    monkeypatch.setattr(backfill, "SessionLocal", lambda: session)

    with pytest.raises(RuntimeError, match="commit failed"):
        await backfill._run(True, translations_path)

    assert (exercise.name_en, exercise.description_en) == (None, None)
    assert session.commit_calls == 1
    assert session.rollback_calls == 1


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.asyncio
async def test_nonquery_order_normalizes_locale_name_then_uses_id(language):
    """Catches whitespace/case variants bypassing the canonical ID tie-breaker."""
    rows = [
        _exercise(id=2, name="  Альфа  ", name_en="  Alpha  "),
        _exercise(id=1, name="альфа", name_en="alpha"),
    ]
    session = _SequenceSession(rows, [])

    result = await ExerciseSearchService.search_exercises(
        session, user_id=7, language=language
    )

    assert [row.id for row in result] == [1, 2]


def test_preference_rank_leads_then_locale_name_and_id_are_deterministic():
    """Catches preference partitioning that preserves arbitrary DB order."""
    rows = [
        _exercise(id=4, name="Гамма", name_en="Zulu"),
        _exercise(id=3, name="Бета", name_en="Beta"),
        _exercise(id=2, name="Альфа  ", name_en="Alpha  "),
        _exercise(id=1, name="  альфа", name_en="  alpha"),
        _exercise(id=6, name="Дельта", name_en="Delta"),
        _exercise(id=5, name="Вега", name_en="Beta"),
    ]
    preferences = {
        1: "favorite",
        2: "favorite",
        5: "disliked",
        6: "disliked",
    }

    result = ExerciseSearchService.sort_and_mark_preferences(
        rows, preferences, language="en"
    )

    assert [row.id for row in result] == [1, 2, 3, 4, 5, 6]
    assert [getattr(row, "_user_preference") for row in result] == [
        "favorite",
        "favorite",
        None,
        None,
        "disliked",
        "disliked",
    ]


@pytest.mark.asyncio
async def test_exact_match_equal_scores_use_english_name_then_id():
    """Catches exact SQL result order leaking into equal-score search results."""
    zulu = _exercise(id=1, name="Первое", name_en="Zulu Press")
    beta_later = _exercise(id=3, name="Второе", name_en="Beta Press")
    beta_first = _exercise(id=2, name="Третье", name_en="Beta Press")
    session = _SequenceSession([zulu, beta_later, beta_first], [])

    best, matches = await ExerciseMatcher.find_or_create_exercise(
        session, 7, "press", language="en"
    )

    assert best["id"] == 2
    assert [item["id"] for item in matches] == [2, 3, 1]


@pytest.mark.asyncio
async def test_fuzzy_match_equal_scores_use_english_name_then_id():
    """Catches fuzzy candidate order leaking into equal-score search results."""
    zulu = _exercise(id=1, name="Первое", name_en="Zulu Press")
    beta_later = _exercise(id=3, name="Второе", name_en="Beta Press")
    beta_first = _exercise(id=2, name="Третье", name_en="Beta Press")
    session = _SequenceSession([], [zulu, beta_later, beta_first])

    best, matches = await ExerciseMatcher.find_or_create_exercise(
        session, 7, "press", language="en"
    )

    assert best["id"] == 2
    assert [item["id"] for item in matches] == [2, 3, 1]


def _six_ranked_search_candidates(*, leading_source="default"):
    rows = [
        _exercise(
            id=index,
            name=f"Press {letter}" if leading_source == "custom" else f"Пресс {letter}",
            name_en=f"Press {letter}",
            source=leading_source,
        )
        for index, letter in enumerate("ABCDE", start=1)
    ]
    rows.append(_exercise(
        id=6,
        name="Жим со штангой",
        name_en="Barbell Press",
        source="default",
    ))
    return rows


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("match_branch", ["exact", "fuzzy"])
@pytest.mark.asyncio
async def test_query_filters_all_candidates_before_final_limit(
    language, match_branch
):
    """Catches a valid sixth candidate disappearing before source filtering."""
    candidates = _six_ranked_search_candidates(leading_source="custom")
    exact_rows = candidates if match_branch == "exact" else []
    fuzzy_rows = candidates if match_branch == "fuzzy" else []
    session = _SequenceSession(exact_rows, fuzzy_rows, [])

    result = await ExerciseSearchService.search_exercises(
        session,
        user_id=7,
        q="press",
        source="default",
        language=language,
    )

    assert [item["id"] for item in result] == [6]


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("match_branch", ["exact", "fuzzy"])
@pytest.mark.asyncio
async def test_query_preference_ranking_precedes_final_limit(language, match_branch):
    """Catches a sixth-ranked favorite being pruned behind five disliked rows."""
    candidates = _six_ranked_search_candidates()
    exact_rows = candidates if match_branch == "exact" else []
    fuzzy_rows = candidates if match_branch == "fuzzy" else []
    preferences = [
        *((exercise_id, "disliked") for exercise_id in range(1, 6)),
        (6, "favorite"),
    ]
    session = _SequenceSession(exact_rows, fuzzy_rows, preferences)

    result = await ExerciseSearchService.search_exercises(
        session, user_id=7, q="press", language=language
    )

    assert len(result) == 5
    assert result[0]["id"] == 6
    assert result[0]["preference"] == "favorite"


@pytest.mark.asyncio
async def test_custom_create_retry_preserves_russian_localization_maps():
    """Catches idempotent replay returning defaults instead of custom text maps."""
    session = _CustomExerciseSession()
    user = SimpleNamespace(
        id=7,
        experience_level="beginner",
        _request_language="en",
    )
    payload = CustomExerciseCreate(
        name="Мой жим",
        main_muscle_group="Грудь",
        secondary_muscle_groups=[],
        equipment_needed=[],
        description="Моя техника",
        client_uuid="exercise-retry-1",
    )

    created = await create_custom_exercise(payload, session, user)
    replayed = await create_custom_exercise(payload, session, user)
    created_wire = ExerciseSearchItem.model_validate(
        created, from_attributes=True
    ).model_dump()
    replayed_wire = ExerciseSearchItem.model_validate(
        replayed, from_attributes=True
    ).model_dump()

    assert created_wire["name"] == replayed_wire["name"] == "Мой жим"
    assert created_wire["localized_names"] == {"ru": "Мой жим"}
    assert replayed_wire["localized_names"] == {"ru": "Мой жим"}
    assert created_wire["description"] == replayed_wire["description"] == "Моя техника"
    assert created_wire["localized_descriptions"] == {"ru": "Моя техника"}
    assert replayed_wire["localized_descriptions"] == {"ru": "Моя техника"}
