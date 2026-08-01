"""Reference-anchored tests for the fatigue-tier formula.

Each case is a real exercise (category, main muscle, secondaries, equipment taken
from the DB) with the tier it SHOULD land in. The formula is tuned until the
computed tiers match these references. Goal of the tuning: machine/cable
compounds should fall into tier 2 (not tier 1), widening the middle band, while
free-weight and bodyweight compounds stay tier 1 and isolation stays tier 3.
"""
import pytest
from api.services.fatigue_tiers import calculate_fatigue_tier

# (name, expected_tier, category, main, secondaries, equipment)
REFERENCES = [
    # --- Tier 1: heavy free-weight / bodyweight compounds ---
    ("Приседания со штангой", 1, "Базовое", "Квадрицепсы",
     ["Ягодицы", "Бицепсы ног", "Икры", "Поясница"], ["barbell"]),
    ("Жим штанги лёжа", 1, "Базовое", "Грудь",
     ["Передняя дельта", "Трицепс"], ["barbell"]),
    ("Румынская становая", 1, "Базовое", "Бицепсы ног",
     ["Ягодицы", "Икры", "Поясница"], ["barbell"]),
    ("Подтягивания", 1, "Базовое", "Широчайшие",
     ["Бицепс", "Предплечья", "Средняя часть спины"], []),
    ("Тяга штанги в наклоне", 1, "Базовое", "Средняя часть спины",
     ["Бицепс", "Широчайшие"], ["barbell"]),
    # --- Tier 2: machine / cable compounds ---
    ("Наклонный жим в тренажёре", 2, "Базовое", "Грудь",
     ["Передняя дельта", "Трицепс"], ["block_machine"]),
    ("Жим сидя в тренажёре", 2, "Базовое", "Передняя дельта",
     ["Средняя дельта", "Трицепс"], ["block_machine"]),
    # --- Tier 3: isolation ---
    ("Подъём штанги на бицепс", 3, "Изолирующее", "Бицепс",
     ["Предплечья"], ["barbell"]),
    ("Сгибание ног в тренажёре", 3, "Изолирующее", "Бицепсы ног",
     [], ["block_machine"]),
]


@pytest.mark.parametrize("name,expected,cat,main,sec,equip", REFERENCES)
def test_fatigue_tier_matches_reference(name, expected, cat, main, sec, equip):
    got = calculate_fatigue_tier(cat, main, sec, equip)
    assert got == expected, f"{name}: expected tier {expected}, got {got}"
