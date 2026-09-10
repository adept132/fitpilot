from types import SimpleNamespace

import pytest

from api.errors import LocalizedHTTPException, localized_http_exception_handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "ru", "en"),
    [
        ("workout.not_active", "Тренировка не активна", "Workout is not active"),
        ("workout.set_not_found", "Подход тренировки не найден", "Workout set not found"),
        (
            "workout.parent_set_mismatch",
            "Родительский подход не относится к этому упражнению",
            "Parent set does not belong to this exercise",
        ),
        (
            "workout.target_session_exercise_not_found",
            "Целевое упражнение сессии не найдено",
            "Target workout session exercise not found",
        ),
        ("auth.invalid_token", "Недействительный токен авторизации", "Invalid authorization token"),
        ("system.database_unavailable", "База данных недоступна", "Database is unavailable"),
    ],
)
async def test_workout_router_errors_render_as_structured_ru_en(code, ru, en):
    error = LocalizedHTTPException(404, code)

    ru_response = await localized_http_exception_handler(
        SimpleNamespace(state=SimpleNamespace(language="ru")), error
    )
    en_response = await localized_http_exception_handler(
        SimpleNamespace(state=SimpleNamespace(language="en")), error
    )

    assert ru_response.body.decode() == (
        '{"detail":"' + ru + '","error":{"code":"' + code + '","params":{}}}'
    )
    assert en_response.body.decode() == (
        '{"detail":"' + en + '","error":{"code":"' + code + '","params":{}}}'
    )
