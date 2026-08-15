from datetime import date

from api.services.reports.metrics import (
    AdherenceMetric,
    EffortMetric,
    IntensityMetric,
    MuscleVolume,
    RecordItem,
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
    ведёт в уже готовое решение, а не предлагает тот же выбор вторым путём.
    Проверяем весь список действий, а не только первый элемент: иначе
    объёмный совет мог бы молча проехать вторым/третьим пунктом."""
    muscles = {
        "chest": MuscleVolume(direct=2.0, indirect=0.0, mev=8, mav=16, mrv=22),
        "back": MuscleVolume(direct=30.0, indirect=0.0, mev=10, mav=18, mrv=25),
    }
    context = RuleContext(pending_proposal_id=42, pending_proposal_kind="volume_review",
                          target_rir=2)

    actions = build_actions(_metrics(muscles=muscles), context)
    ids = [action.id for action in actions]

    assert actions[0].id == "pending_proposal"
    assert actions[0].route == "/periodization/window-summary"
    assert "volume_over_mrv" not in ids
    assert "volume_below_mev" not in ids


def test_pending_proposal_of_other_kind_does_not_suppress_volume_advice():
    """Подавление узкое: только volume_review — про объём. Предложение
    другого рода (например, block_review) не владеет объёмным решением и не
    должен глушить объёмный совет."""
    muscles = {
        "chest": MuscleVolume(direct=2.0, indirect=0.0, mev=8, mav=16, mrv=22),
        "back": MuscleVolume(direct=30.0, indirect=0.0, mev=10, mav=18, mrv=25),
    }
    context = RuleContext(pending_proposal_id=42, pending_proposal_kind="block_review",
                          target_rir=2)

    actions = build_actions(_metrics(muscles=muscles), context)
    ids = [action.id for action in actions]

    assert "volume_over_mrv" in ids
    assert "volume_below_mev" in ids


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


def test_effort_labelling_advice_fills_leftover_slot():
    """Совет про метод — низший приоритет, а не строгий last resort: он
    занимает свободное место, если под тремя действиями остался зазор, и
    закономерно вытесняется, когда три содержательных правила уже сработали."""
    # Три содержательных правила срабатывают: adherence_low (0.33 <
    # ADHERENCE_FLOOR), volume_over_mrv (спина: 30 подходов при потолке 25),
    # volume_below_mev (грудь: 2 подхода при минимуме 8). Без ожидающего
    # предложения — иначе volume_review заглушил бы оба объёмных правила и
    # тест перестал бы проверять то, что заявлен проверять. Список заполнен
    # до крышки ещё до того, как очередь дойдёт до effort_unlabelled.
    both_muscles = {
        "chest": MuscleVolume(direct=2.0, indirect=0.0, mev=8, mav=16, mrv=22),
        "back": MuscleVolume(direct=30.0, indirect=0.0, mev=10, mav=18, mrv=25),
    }
    crowded = build_actions(
        _metrics(adherence=0.33, muscles=both_muscles, labeled=0.1), EMPTY_CONTEXT
    )
    assert [action.id for action in crowded] == [
        "adherence_low", "volume_over_mrv", "volume_below_mev",
    ]
    assert len(crowded) == MAX_ACTIONS
    assert "effort_unlabelled" not in [action.id for action in crowded]

    # Только два содержательных правила срабатывают (adherence_low,
    # volume_below_mev) — есть одно свободное место, и метод-совет его
    # занимает, оставаясь последним по приоритету.
    below_mev_muscle = {"chest": MuscleVolume(direct=2.0, indirect=0.0, mev=8, mav=16, mrv=22)}
    two_fired = build_actions(
        _metrics(adherence=0.33, muscles=below_mev_muscle, labeled=0.1), EMPTY_CONTEXT
    )
    assert [action.id for action in two_fired] == [
        "adherence_low", "volume_below_mev", "effort_unlabelled",
    ]

    # Ничего содержательного не сработало — метод-совет остаётся один.
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


def test_no_records_fires_when_enough_sessions_without_records():
    actions = build_actions(_metrics(sessions=4, records=[]), EMPTY_CONTEXT)

    assert any(action.id == "no_records" for action in actions)
    fired = next(action for action in actions if action.id == "no_records")
    assert fired.route == "/progress"
    assert "4" in fired.reason


def test_no_records_does_not_fire_with_a_record_present():
    """Хоть один рекорд в периоде — прогресс не встал, правило не должно
    срабатывать, даже если тренировок достаточно."""
    record = RecordItem(
        exercise_id=1, exercise_name="Жим лёжа", record_type="max_weight",
        value=100.0, achieved_on=date(2026, 8, 12),
    )
    actions = build_actions(_metrics(sessions=4, records=[record]), EMPTY_CONTEXT)

    assert all(action.id != "no_records" for action in actions)


def test_no_records_does_not_fire_with_too_few_sessions():
    """Меньше 4 тренировок — отсутствие рекордов ожидаемо, не сигнал."""
    actions = build_actions(_metrics(sessions=3, records=[]), EMPTY_CONTEXT)

    assert all(action.id != "no_records" for action in actions)


def test_action_is_hashable_dataclass():
    action = Action(id="x", title="t", reason="r", route="/home")
    assert action.route == "/home"
