from copy import deepcopy
from types import SimpleNamespace

import pytest

from api.services.reports.service import enrich_report_record_localizations


class _ExerciseResult:
    def __init__(self, exercises):
        self._exercises = exercises

    def scalars(self):
        return self

    def all(self):
        return self._exercises


class _CountingSession:
    def __init__(self, exercises=()):
        self._exercises = list(exercises)
        self.execute_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        if self.execute_count > 1:
            raise AssertionError("report localization used more than one query")
        return _ExerciseResult(self._exercises)


class _NoQuerySession:
    async def execute(self, _statement):
        raise AssertionError(
            "report localization queried without a usable exercise ID"
        )


def _exercise(
    exercise_id: int,
    *,
    name: str,
    description: str,
    source: str = "default",
    name_en: str | None = None,
    description_en: str | None = None,
):
    return SimpleNamespace(
        id=exercise_id,
        name=name,
        description=description,
        source=source,
        name_en=name_en,
        description_en=description_en,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exercise_id", "localized_names", "localized_descriptions"),
    [
        (
            None,
            {"en": "Stored name", "de": "Gespeicherter Name"},
            {"en": "Stored description", "de": "Gespeicherte Beschreibung"},
        ),
        (
            "not-an-id",
            {"ru": "   ", "en": "Stored name"},
            {"ru": "\t", "en": "Stored description"},
        ),
        (
            False,
            {"ru": 17, "en": "Stored name"},
            {"ru": ["invalid"], "en": "Stored description"},
        ),
    ],
    ids=["english-only", "whitespace-ru", "wrong-type-ru"],
)
async def test_partial_legacy_maps_merge_durable_ru_without_query(
    exercise_id,
    localized_names,
    localized_descriptions,
):
    stored_metrics = {
        "records": [
            {
                "exercise_id": exercise_id,
                "exercise_name": "Durable name",
                "exercise_description": "Durable description",
                "localized_names": localized_names,
                "localized_descriptions": localized_descriptions,
            }
        ]
    }
    original_metrics = deepcopy(stored_metrics)

    enriched = await enrich_report_record_localizations(
        _NoQuerySession(), stored_metrics
    )

    record = enriched["records"][0]
    assert record["localized_names"]["ru"] == "Durable name"
    assert record["localized_names"]["en"] == "Stored name"
    assert record["localized_descriptions"]["ru"] == "Durable description"
    assert record["localized_descriptions"]["en"] == "Stored description"
    if "de" in localized_names:
        assert record["localized_names"]["de"] == "Gespeicherter Name"
        assert (
            record["localized_descriptions"]["de"]
            == "Gespeicherte Beschreibung"
        )
    assert stored_metrics == original_metrics
    assert enriched is not stored_metrics


@pytest.mark.asyncio
async def test_partial_system_maps_merge_canonical_ru_and_preserve_stored_values():
    system = _exercise(
        76,
        name="Жим лёжа",
        name_en="Bench Press",
        description="Жим штанги лёжа.",
        description_en="Barbell bench press.",
    )
    stored_metrics = {
        "records": [
            {
                "exercise_id": 76,
                "exercise_name": "Stale durable name",
                "exercise_description": "Stale durable description",
                "localized_names": {"en": "Stored English name"},
                "localized_descriptions": {
                    "ru": "   ",
                    "en": "Stored English description",
                },
            }
        ]
    }
    original_metrics = deepcopy(stored_metrics)
    session = _CountingSession([system])

    enriched = await enrich_report_record_localizations(session, stored_metrics)

    assert enriched["records"][0]["localized_names"] == {
        "ru": "Жим лёжа",
        "en": "Stored English name",
    }
    assert enriched["records"][0]["localized_descriptions"] == {
        "ru": "Жим штанги лёжа.",
        "en": "Stored English description",
    }
    assert stored_metrics == original_metrics
    assert session.execute_count == 1


@pytest.mark.asyncio
async def test_partial_custom_maps_use_canonical_ru_without_shadow_english():
    custom = _exercise(
        501,
        name="Мой жим",
        name_en="SHADOW NAME",
        description="Моя техника",
        description_en="SHADOW DESCRIPTION",
        source="custom",
    )
    stored_metrics = {
        "records": [
            {
                "exercise_id": 501,
                "exercise_name": "Stale custom name",
                "exercise_description": "Stale custom description",
                "localized_names": {"en": "Stored historical translation"},
                "localized_descriptions": {"ru": None},
            }
        ]
    }
    original_metrics = deepcopy(stored_metrics)
    session = _CountingSession([custom])

    enriched = await enrich_report_record_localizations(session, stored_metrics)

    assert enriched["records"][0]["localized_names"] == {
        "ru": "Мой жим",
        "en": "Stored historical translation",
    }
    assert enriched["records"][0]["localized_descriptions"] == {
        "ru": "Моя техника"
    }
    assert stored_metrics == original_metrics
    assert session.execute_count == 1


@pytest.mark.asyncio
async def test_wrong_type_maps_are_rebuilt_from_canonical_localizations():
    system = _exercise(
        76,
        name="Жим лёжа",
        name_en="Bench Press",
        description="Жим штанги лёжа.",
        description_en="Barbell bench press.",
    )
    stored_metrics = {
        "records": [
            {
                "exercise_id": 76,
                "localized_names": ["invalid"],
                "localized_descriptions": "invalid",
            }
        ]
    }
    original_metrics = deepcopy(stored_metrics)
    session = _CountingSession([system])

    enriched = await enrich_report_record_localizations(session, stored_metrics)

    assert enriched["records"][0]["localized_names"] == {
        "ru": "Жим лёжа",
        "en": "Bench Press",
    }
    assert enriched["records"][0]["localized_descriptions"] == {
        "ru": "Жим штанги лёжа.",
        "en": "Barbell bench press.",
    }
    assert stored_metrics == original_metrics
    assert session.execute_count == 1


@pytest.mark.asyncio
async def test_nonblank_ru_maps_remain_byte_for_byte_without_query():
    stored_metrics = {
        "records": [
            {
                "exercise_id": 76,
                "exercise_name": "Canonical lookup must not happen",
                "localized_names": {
                    "ru": "  Stored Russian name  ",
                    "en": "Stored English name",
                },
                "localized_descriptions": {
                    "ru": "\tStored Russian description\n",
                    "en": "Stored English description",
                },
            }
        ]
    }
    original_metrics = deepcopy(stored_metrics)

    enriched = await enrich_report_record_localizations(
        _NoQuerySession(), stored_metrics
    )

    assert enriched == original_metrics
    assert enriched is not stored_metrics
    assert enriched["records"][0] is not stored_metrics["records"][0]
    assert stored_metrics == original_metrics
