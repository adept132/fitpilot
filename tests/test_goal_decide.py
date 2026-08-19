"""Классификация разрыва и лестница рычагов (P0-12, Задача 3).

ФИКС C1 (финальное ревью P0-12): decide() больше не оценивает эффект рычага
сам — он просит вызывающую сторону пересимулировать движок (simulate_with,
см. её контракт в types.SimulateWith). Здесь, в чисто модульных тестах, эта
роль отдана `_fake_simulate_with` — подмене, воспроизводящей ту же
арифметику фиксированных долей, что раньше жила ВНУТРИ decide.py
(decide._LEVER_SHARE), чтобы прежние сценарии (порядок лестницы,
минимальность, вето) проверяли то же самое поведение через новый контракт.
Отдельный тест ниже (test_effect_is_grounded_in_real_engine_and_minimal)
доказывает то же самое на НАСТОЯЩЕМ движке (simulate.apply_lever +
simulate.run), а не на подмене — то, ради чего фикс C1 и делается.
"""
from datetime import date, datetime, timedelta

from api.services.goal import decide, params, simulate
from api.services.goal.types import DecisionInput, FutureSession, Rates
from api.services.progression.types import (
    ExerciseHistory,
    Prescription,
    ProgressionState,
    SchemeContext,
    SessionFact,
    SetFact,
    SetPrescription,
)

TODAY = date(2026, 3, 2)

_SHARE = {
    params.LEVER_SCHEME: 0.15,
    params.LEVER_REP_RANGE: 0.12,
    params.LEVER_SETS: 0.10,
    params.LEVER_LIFT_FREQUENCY: 0.35,
    params.LEVER_STRUCTURAL: 0.50,
}


def _inp(**over) -> DecisionInput:
    base = dict(
        rates=Rates(required=1.2, plan=0.8, ceiling=2.0),
        deadline=TODAY + timedelta(days=90),
        eta=TODAY + timedelta(days=120),
        lift_in_plan=True,
        trend_slope=0.8,
        microcycles_left=12,
        headroom_sets=4,
        scheme="double",
        is_heavy_compound=True,
        rep_max=12,
        target_reps=3,
    )
    base.update(over)
    return DecisionInput(**base)


def _fake_simulate_with(inp: DecisionInput):
    """Каждый рычаг добавляет фиксированную долю темпа (см. докстринг
    модуля); LEVER_ENSURE_PRESENT сразу закрывает исходный разрыв целиком.
    ETA монотонно отодвигается назад на столько же дней, на сколько растёт
    темп, — этого достаточно, чтобы decide()._effect_days() не был всегда
    нулевым в этих тестах."""
    gap = max(inp.rates.required - inp.rates.plan, 0.0)

    def _share(kind: str) -> float:
        return gap if kind == params.LEVER_ENSURE_PRESENT else _SHARE[kind]

    def simulate_with(applied, kind, detail):
        total = sum(_share(l.kind) for l in applied) + _share(kind)
        new_slope = inp.rates.plan + total
        new_eta = (
            inp.eta - timedelta(days=round(total * 100))
            if inp.eta is not None
            else None
        )
        return new_slope, new_eta

    return simulate_with


def _decide(inp: DecisionInput):
    return decide.decide(inp, _fake_simulate_with(inp))


def test_on_track_is_silence():
    levers, reason = _decide(_inp(rates=Rates(required=0.5, plan=0.8, ceiling=2.0)))
    assert levers == []
    assert reason == ""


def test_missing_lift_is_the_first_lever():
    levers, reason = _decide(_inp(lift_in_plan=False))
    assert levers[0].kind == params.LEVER_ENSURE_PRESENT
    assert reason == params.REASON_LIFT_MISSING


def test_above_ceiling_gives_no_levers_but_a_reason():
    levers, reason = _decide(_inp(rates=Rates(required=3.0, plan=0.8, ceiling=2.0)))
    assert levers == []
    assert reason == params.REASON_ABOVE_CEILING


def test_falling_trend_does_not_accelerate():
    levers, reason = _decide(_inp(trend_slope=-0.4))
    assert levers == []
    assert reason == params.REASON_TREND_DOWN


def test_ladder_stops_as_soon_as_gap_is_closed():
    """Мелкий разрыв закрывается одной ступенью, а не всей лестницей."""
    small, _ = _decide(
        _inp(rates=Rates(required=0.9, plan=0.8, ceiling=2.0),
             eta=TODAY + timedelta(days=140))
    )
    big, _ = _decide(_inp(rates=Rates(required=1.9, plan=0.4, ceiling=2.0)))
    assert 0 < len(small) < len(big)
    assert params.LEVER_STRUCTURAL not in [l.kind for l in small]


def test_ladder_order_follows_params():
    levers, _ = _decide(_inp(rates=Rates(required=1.9, plan=0.4, ceiling=2.0)))
    kinds = [l.kind for l in levers]
    positions = [params.LADDER.index(k) for k in kinds]
    assert positions == sorted(positions)


def test_no_sets_lever_without_volume_headroom():
    levers, _ = _decide(_inp(headroom_sets=0, rates=Rates(required=1.9, plan=0.4, ceiling=2.0)))
    assert all(l.kind != params.LEVER_SETS for l in levers)


def test_structural_levers_are_blocked_on_short_horizon():
    levers, _ = _decide(
        _inp(microcycles_left=1, rates=Rates(required=1.9, plan=0.4, ceiling=2.0))
    )
    assert all(l.kind not in params.STRUCTURAL_LEVERS for l in levers)


def test_both_thresholds_must_fire():
    """Отставание по темпу есть, а по ETA — меньше недели: молчим."""
    levers, reason = _decide(
        _inp(rates=Rates(required=0.9, plan=0.8, ceiling=2.0),
             eta=TODAY + timedelta(days=93))
    )
    assert levers == []
    assert reason == ""


def test_levers_are_indexed_from_zero():
    levers, _ = _decide(_inp(rates=Rates(required=1.9, plan=0.4, ceiling=2.0)))
    assert [l.index for l in levers] == list(range(len(levers)))


def test_ladder_exhausted_but_short_is_partial_catchup():
    """Разрыв огромный (gap=1.5), сумма долей применимых ступеней ~1.22:
    лестница отрабатывает целиком и всё равно не закрывает разрыв — это
    не то же самое, что "рычаги закрывают гап" (REASON_PACE_BEHIND)."""
    levers, reason = _decide(
        _inp(rates=Rates(required=1.9, plan=0.4, ceiling=2.0))
    )
    assert levers != []
    assert reason == params.REASON_PARTIAL_CATCHUP


def test_no_applicable_lever_reports_no_lever_left():
    """Объёма нет, схема уже оптимальная (не тяжёлый компаунд), диапазон
    повторов уже максимален, горизонт короткий для структурных ступеней —
    ни одна ступень не применима, лифт в плане уже есть."""
    levers, reason = _decide(
        _inp(
            rates=Rates(required=1.9, plan=0.4, ceiling=2.0),
            headroom_sets=0,
            is_heavy_compound=False,
            scheme="fixed_increment",
            target_reps=12,
            rep_max=12,
            microcycles_left=1,
        )
    )
    assert levers == []
    assert reason == params.REASON_NO_LEVER_LEFT


def test_lift_returning_to_plan_unlocks_the_rest_of_the_ladder_same_pass():
    """ФИКС C1, следствие: lift_present внутри decide() обновляется сразу
    после того, как LEVER_ENSURE_PRESENT принят, а не только на следующем
    вызове. Если возврат лифта в план (реалистично — раз в микроцикл) один
    не закрывает весь разрыв, решатель обязан в ТОМ ЖЕ проходе рассмотреть
    остальные ступени (scheme/rep_range/sets/frequency/structural), а не
    остановиться после первой из-за того, что _applicable всё ещё видит
    "лифта в плане нет" по СТАРОМУ, не обновлённому inp.lift_in_plan."""
    inp = _inp(lift_in_plan=False, rates=Rates(required=1.9, plan=0.4, ceiling=2.0))
    # ensure_present здесь НЕ закрывает разрыв целиком (в отличие от
    # _fake_simulate_with) — синтетическая частота "раз в микроцикл" даёт
    # умеренный прирост, как и в реальном simulate.apply_lever.
    contribution = {
        params.LEVER_ENSURE_PRESENT: 0.5,
        params.LEVER_SCHEME: 0.15,
        params.LEVER_REP_RANGE: 0.12,
        params.LEVER_SETS: 0.10,
        params.LEVER_LIFT_FREQUENCY: 0.35,
        params.LEVER_STRUCTURAL: 0.50,
    }

    def simulate_with(applied, kind, detail):
        total = sum(contribution[l.kind] for l in applied) + contribution[kind]
        return inp.rates.plan + total, inp.eta

    levers, reason = decide.decide(inp, simulate_with)
    kinds = [l.kind for l in levers]
    assert kinds[0] == params.LEVER_ENSURE_PRESENT
    assert len(kinds) > 1, "разрыв не закрылся одним ensure_present — лестница обязана продолжить"
    assert params.LEVER_SCHEME in kinds


def test_zero_effect_lever_is_not_proposed():
    """Пересимуляция может честно сказать "этот рычаг ничего не даёт"
    (потолок уже исчерпан предыдущими ступенями) — decide() обязан
    промолчать про него, а не предложить рычаг с нулевым/отрицательным
    эффектом."""
    inp = _inp(rates=Rates(required=0.95, plan=0.8, ceiling=2.0))

    def simulate_with(applied, kind, detail):
        # Ни один кандидат в этом сценарии ничего не меняет — пересимуляция
        # честно возвращает тот же темп, что и до рычага, для любой ступени.
        return inp.rates.plan, inp.eta

    levers, reason = decide.decide(inp, simulate_with)
    assert levers == []
    assert reason == params.REASON_NO_LEVER_LEFT


# --- Доказательство фикса C1 на настоящем движке, а не на подмене ---
#
# Спека §5.4 требует, чтобы эффект рычага был результатом пересимуляции.
# Тест ниже подключает decide() к НАСТОЯЩЕЙ паре simulate.apply_lever +
# simulate.run (та же связка, что использует service.evaluate в проде) и
# проверяет два конкретных требования брифа сразу:
#   - минимальность против РЕАЛЬНЫХ чисел: без последнего принятого рычага
#     симулированный темп ещё не дотягивает до required;
#   - effect_days последнего рычага — это ИМЕННО разница симулированных ETA
#     до и после него, а не производная величина.

def _seed_ctx():
    working_e1rm = 100.0
    seed_prescription = Prescription(
        scheme="double",
        sets=tuple(SetPrescription(n, working_e1rm, 5, 8, 2, "normal") for n in range(1, 4)),
        reason_code="progressed",
        reason_text="",
    )
    seed_facts = tuple(
        SetFact(set_number=n, weight_kg=working_e1rm, reps=8, rir=2) for n in range(1, 4)
    )
    history = ExerciseHistory(
        exercise_id=51,
        sessions=(
            SessionFact(
                session_id=-1, finished_at=datetime(2026, 2, 27),
                prescription=seed_prescription, sets=seed_facts,
            ),
        ),
    )
    return SchemeContext(
        history=history,
        state=ProgressionState(working_e1rm=working_e1rm, last_scheme="double"),
        last_outcome=None,
        target_sets=3,
        rep_min=5,
        rep_max=8,
        rep_range_source="fallback",
        target_rir=2,
        equipment=("barbell",),
        experience_level="intermediate",
        fatigue_tier=1,
        main_muscle_group="chest",
    )


def _seed_sessions(n: int, start: date, every_days: int = 7):
    return [
        FutureSession(date=start + timedelta(days=i * every_days),
                      phase_effort_tier="medium", prescription_sets=3)
        for i in range(n)
    ]


def test_effect_is_grounded_in_real_engine_and_minimal():
    ctx = _seed_ctx()
    start = date(2026, 3, 2)
    until = start + timedelta(days=400)
    sessions = _seed_sessions(50, start=start, every_days=7)
    target_e1rm = 140.0
    cap_pct = 1.0  # щедрый потолок — частота реально должна быть видна, не потолок

    baseline = simulate.run(ctx, target_e1rm=target_e1rm, sessions=sessions,
                             cap_pct=cap_pct, factor=None)
    assert baseline.plan_slope > 0, "сценарий подобран так, что план и так растёт"

    def simulate_with(applied, kind, detail):
        cur_sessions, cur_ctx = sessions, ctx
        for lever in applied:
            cur_sessions, cur_ctx = simulate.apply_lever(
                lever.kind, lever.detail, cur_sessions, cur_ctx,
                exercise_id=51, microcycle_length=7, start=start, until=until,
            )
        cur_sessions, cur_ctx = simulate.apply_lever(
            kind, detail, cur_sessions, cur_ctx,
            exercise_id=51, microcycle_length=7, start=start, until=until,
        )
        probe = simulate.run(cur_ctx, target_e1rm=target_e1rm, sessions=cur_sessions,
                              cap_pct=cap_pct, factor=None)
        return probe.plan_slope, probe.nominal_date

    inp = DecisionInput(
        rates=Rates(required=baseline.plan_slope * 1.5, plan=baseline.plan_slope,
                    ceiling=baseline.plan_slope * 6),
        deadline=start,  # уже "просрочен" -> заведомо большой ETA-разрыв
        eta=baseline.nominal_date,
        lift_in_plan=True,
        trend_slope=0.1,
        microcycles_left=12,
        headroom_sets=4,
        scheme="double",
        is_heavy_compound=True,
        rep_max=8,
        target_reps=3,
    )
    levers, reason = decide.decide(inp, simulate_with)
    assert levers, "лестница обязана найти хотя бы один настоящий рычаг для этого разрыва"

    def _replay(n: int):
        cur_sessions, cur_ctx = sessions, ctx
        for lever in levers[:n]:
            cur_sessions, cur_ctx = simulate.apply_lever(
                lever.kind, lever.detail, cur_sessions, cur_ctx,
                exercise_id=51, microcycle_length=7, start=start, until=until,
            )
        return simulate.run(cur_ctx, target_e1rm=target_e1rm, sessions=cur_sessions,
                             cap_pct=cap_pct, factor=None)

    after_all = _replay(len(levers))
    assert after_all.plan_slope >= inp.rates.required - 1e-9  # достаточность

    before_last = _replay(len(levers) - 1)
    assert before_last.plan_slope < inp.rates.required, (
        "без последнего рычага симулированный темп уже НЕ должен закрывать "
        "разрыв — иначе decide() добавил лишнюю, не минимальную ступень"
    )

    last = levers[-1]
    if before_last.nominal_date is not None and after_all.nominal_date is not None:
        expected_days = max(0, (before_last.nominal_date - after_all.nominal_date).days)
    else:
        expected_days = 0
    assert last.effect_days == expected_days
