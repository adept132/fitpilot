"""Граница блока: итоги плюс структурные предложения по вставшим упражнениям."""
from api.services.periodization import params
from api.services.periodization.decide import decide
from api.services.periodization.types import (
    BlockPosition,
    DecisionInput,
    FatigueSignal,
    PlateauSignal,
)


def _complete_pos() -> BlockPosition:
    return BlockPosition(
        phase_number=4,
        effort_tier="deload",
        phase_ordinal=4,
        phases_total=4,
        day_in_block=29,
        days_to_deload=0,
        is_last_phase=True,
        is_complete=True,
    )


def test_completed_block_proposes_boundary():
    result = decide(DecisionInput(position=_complete_pos()))
    assert [p.kind for p in result] == [params.KIND_BLOCK_BOUNDARY]
    assert result[0].reason_code == params.REASON_BLOCK_COMPLETED


def test_structural_proposal_per_stalled_exercise():
    inp = DecisionInput(
        position=_complete_pos(),
        plateau=PlateauSignal(
            exercises_with_history=5, stalled=2, stalled_after_deload=(11, 42)
        ),
    )
    result = decide(inp)
    assert [p.kind for p in result] == [
        params.KIND_BLOCK_BOUNDARY,
        params.KIND_STRUCTURAL,
        params.KIND_STRUCTURAL,
    ]
    assert [p.payload["exercise_id"] for p in result[1:]] == [11, 42]
    assert result[1].reason_code == params.REASON_STALLED_AFTER_DELOAD


def test_structural_proposal_offers_three_ways_out():
    inp = DecisionInput(
        position=_complete_pos(),
        plateau=PlateauSignal(exercises_with_history=4, stalled=1, stalled_after_deload=(7,)),
    )
    structural = decide(inp)[1]
    assert structural.payload["options"] == ["shift_reps", "replace", "keep"]
    assert structural.payload["default_option"] == "shift_reps"


def test_no_early_deload_at_the_boundary():
    """Блок кончился — предлагать в нём разгрузку поздно."""
    inp = DecisionInput(
        position=_complete_pos(),
        fatigue=FatigueSignal(fatigued_days=9, band_known=True),
    )
    assert params.KIND_EARLY_DELOAD not in [p.kind for p in decide(inp)]


def test_stalled_without_deload_history_yields_no_structural():
    """Критерий — «пережило разгрузку». Пустой список адресов значит, что
    проверку усталостью упражнения ещё не проходили."""
    inp = DecisionInput(
        position=_complete_pos(),
        plateau=PlateauSignal(exercises_with_history=6, stalled=4, stalled_after_deload=()),
    )
    assert [p.kind for p in decide(inp)] == [params.KIND_BLOCK_BOUNDARY]
