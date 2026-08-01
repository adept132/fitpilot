from api.services.exercise_selection_engine import order_by_systemic_cost


def test_orders_tier1_first_then_by_muscle_score():
    items = [
        {"id": "iso_bic", "fatigue_tier": 3, "primary_muscle": "Бицепс"},
        {"id": "squat",   "fatigue_tier": 1, "primary_muscle": "Квадрицепсы"},
        {"id": "machine", "fatigue_tier": 2, "primary_muscle": "Грудь"},
    ]
    out = [x["id"] for x in order_by_systemic_cost(items)]
    assert out == ["squat", "machine", "iso_bic"]


def test_tie_break_prefers_larger_muscle():
    items = [
        {"id": "tri", "fatigue_tier": 2, "primary_muscle": "Трицепс"},
        {"id": "chest", "fatigue_tier": 2, "primary_muscle": "Грудь"},
    ]
    out = [x["id"] for x in order_by_systemic_cost(items)]
    assert out == ["chest", "tri"]
