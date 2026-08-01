# tests/test_allocate_ratio.py
from api.services.exercise_selection_engine import _allocate


def test_default_fraction_unchanged():
    comp, iso = _allocate(9)  # 2:1 default
    assert sum(comp) == 6 and sum(iso) == 3


def test_more_base_fraction():
    comp, iso = _allocate(8, compound_fraction=3/4)  # ratio 3:1
    assert sum(comp) >= sum(iso)  # base-heavy
    assert sum(comp) + sum(iso) == 8
