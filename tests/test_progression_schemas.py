"""Совместимость расширенных схем ответа."""

from api.schemas.workouts import AutoprogressionResponse


def test_legacy_shape_still_validates():
    # Старые сборки приложения шлют и ждут ровно эти поля.
    resp = AutoprogressionResponse(
        has_basis=True, metric="e1rm", target_weight=42.5, target_reps=8,
        modified_target=56.1,
    )
    assert resp.target_weight == 42.5
    assert resp.prescription is None
    assert resp.reason_code is None


def test_new_fields_are_accepted():
    resp = AutoprogressionResponse(
        has_basis=True,
        metric="e1rm",
        target_weight=42.5,
        target_reps=8,
        modified_target=None,
        prescription={"scheme": "double", "sets": []},
        scheme="double",
        reason_code="progressed",
        reason_text="Прошлая цель выполнена — двигаемся вперёд.",
    )
    assert resp.scheme == "double"
    assert resp.reason_text.startswith("Прошлая цель")


def test_no_basis_response_is_valid():
    resp = AutoprogressionResponse(
        has_basis=False, metric=None, target_weight=None, target_reps=None,
        modified_target=None, reason_code="no_basis",
    )
    assert resp.has_basis is False
