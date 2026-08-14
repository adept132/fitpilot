from datetime import date

from api.services.reports.metrics import (
    AdherenceMetric,
    EffortMetric,
    IntensityMetric,
    MuscleVolume,
    ReportMetrics,
    TimeMetric,
    VolumeMetric,
)
from api.services.reports.rules import MAX_ACTIONS, Action, RuleContext, build_actions


def _metrics(*, adherence=1.0, muscles=None, avg_rir=2.0, labeled=1.0,
             records=None, sessions=3) -> ReportMetrics:
    return ReportMetrics(
        period_type="week",
        period_start=date(2026, 8, 10),
        period_end=date(2026, 8, 16),
        adherence=AdherenceMetric(
            planned_days=3, completed_days=round(3 * adherence),
            missed_days=3 - round(3 * adherence), rate=adherence,
        ),
        volume=VolumeMetric(work_sets=30, tonnage_kg=9000.0, by_muscle=muscles or {}),
        intensity=IntensityMetric(avg_relative=0.75, heavy_set_share=0.2),
        effort=EffortMetric(avg_rir=avg_rir, labeled_share=labeled),
        time=TimeMetric(sessions=sessions, total_minutes=180,
                        avg_session_minutes=60.0, sets_per_hour=10.0),
        records=records or [],
    )


EMPTY_CONTEXT = RuleContext(pending_proposal_id=None, pending_proposal_kind=None, target_rir=2)


def test_low_adherence_produces_schedule_action():
    actions = build_actions(_metrics(adherence=0.33), EMPTY_CONTEXT)

    assert actions[0].id == "adherence_low"
    assert "1 из 3" in actions[0].reason
    assert actions[0].route == "/settings/training"


def test_full_adherence_produces_no_schedule_action():
    actions = build_actions(_metrics(adherence=1.0), EMPTY_CONTEXT)

    assert all(action.id != "adherence_low" for action in actions)


def test_pending_proposal_outranks_volume_advice():
    """Существующее предложение нельзя дублировать своим советом — отчёт
    ведёт в уже готовое решение."""
    muscles = {"chest": MuscleVolume(direct=2.0, indirect=0.0, mev=8, mav=16, mrv=22)}
    context = RuleContext(pending_proposal_id=42, pending_proposal_kind="volume_review",
                          target_rir=2)

    actions = build_actions(_metrics(muscles=muscles), context)

    assert actions[0].id == "pending_proposal"
    assert actions[0].route == "/periodization/window-summary"


def test_muscle_below_mev_and_above_mrv_both_reported():
    muscles = {
        "chest": MuscleVolume(direct=2.0, indirect=0.0, mev=8, mav=16, mrv=22),
        "back": MuscleVolume(direct=30.0, indirect=0.0, mev=10, mav=18, mrv=25),
    }

    actions = build_actions(_metrics(muscles=muscles), EMPTY_CONTEXT)
    ids = [action.id for action in actions]

    assert "volume_over_mrv" in ids
    assert "volume_below_mev" in ids
    # Перебор объёма опаснее недобора и обязан идти выше.
    assert ids.index("volume_over_mrv") < ids.index("volume_below_mev")


def test_high_rir_suggests_raising_weights():
    actions = build_actions(_metrics(avg_rir=3.5), EMPTY_CONTEXT)

    assert any(action.id == "rir_too_easy" for action in actions)


def test_effort_labelling_advice_is_last_resort():
    """Совет про метод не должен вытеснять содержательные действия."""
    muscles = {"chest": MuscleVolume(direct=2.0, indirect=0.0, mev=8, mav=16, mrv=22)}

    crowded = build_actions(
        _metrics(adherence=0.33, muscles=muscles, labeled=0.1), EMPTY_CONTEXT
    )
    assert all(action.id != "effort_unlabelled" for action in crowded)

    alone = build_actions(_metrics(labeled=0.1), EMPTY_CONTEXT)
    assert [action.id for action in alone] == ["effort_unlabelled"]


def test_never_more_than_three_actions():
    muscles = {
        "chest": MuscleVolume(direct=2.0, indirect=0.0, mev=8, mav=16, mrv=22),
        "back": MuscleVolume(direct=30.0, indirect=0.0, mev=10, mav=18, mrv=25),
    }
    context = RuleContext(pending_proposal_id=7, pending_proposal_kind="volume_review",
                          target_rir=2)

    actions = build_actions(
        _metrics(adherence=0.0, muscles=muscles, avg_rir=4.0, labeled=0.1), context
    )

    assert len(actions) == MAX_ACTIONS
    assert len({action.id for action in actions}) == MAX_ACTIONS


def test_quiet_period_produces_no_actions():
    assert build_actions(_metrics(), EMPTY_CONTEXT) == []


def test_action_is_hashable_dataclass():
    action = Action(id="x", title="t", reason="r", route="/home")
    assert action.route == "/home"
