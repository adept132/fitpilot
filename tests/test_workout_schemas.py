"""P0-09: ExerciseShortResponse отдаёт нормализованные системные ключи мышц.

Раньше клиент нормализовал русские названия сам, тремя расходящимися копиями
RU_TO_EN_MAP. Нормализация переехала на бэкенд, в единственный to_system_key —
эта схема обязана заполнять muscle_key/secondary_muscle_keys независимо от
того, как именно main_muscle_group/secondary_muscle_groups хранятся в БД.
"""

from api.schemas.workouts import ExerciseShortResponse


def test_muscle_key_normalized_from_russian_main_group():
    resp = ExerciseShortResponse(
        id=1, name="Жим лёжа", main_muscle_group="Грудь",
    )
    assert resp.muscle_key == "chest"


def test_secondary_muscle_keys_normalized_from_list():
    resp = ExerciseShortResponse(
        id=1, name="Жим лёжа", main_muscle_group="Грудь",
        secondary_muscle_groups=["Трицепс", "Передняя дельта"],
    )
    assert resp.secondary_muscle_keys == ["triceps", "front_delts"]


def test_secondary_muscle_keys_survive_string_shape():
    # secondary_muscle_groups иногда приходит строкой, а не списком.
    resp = ExerciseShortResponse(
        id=1, name="Жим лёжа", main_muscle_group="Грудь",
        secondary_muscle_groups="Трицепс, Передняя дельта",
    )
    assert resp.secondary_muscle_keys == ["triceps", "front_delts"]


def test_unknown_muscle_names_drop_to_none_and_are_filtered():
    resp = ExerciseShortResponse(
        id=1, name="Загадочное упражнение", main_muscle_group="Неведома зверушка",
        secondary_muscle_groups=["Тоже неизвестно", "Трицепс"],
    )
    assert resp.muscle_key is None
    assert resp.secondary_muscle_keys == ["triceps"]


def test_missing_main_muscle_group_yields_none_key_and_empty_secondary():
    resp = ExerciseShortResponse(id=1, name="Без мышцы")
    assert resp.muscle_key is None
    assert resp.secondary_muscle_keys == []


def test_already_system_key_passes_through():
    # main_muscle_group у кастомных упражнений иногда уже системный ключ.
    resp = ExerciseShortResponse(
        id=1, name="Кастом", main_muscle_group="chest",
        secondary_muscle_groups=["triceps"],
    )
    assert resp.muscle_key == "chest"
    assert resp.secondary_muscle_keys == ["triceps"]
