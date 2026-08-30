"""Детерминированные правила действий отчёта.

Каждое правило — чистая функция от метрик и контекста. Никакой модели,
никакой генерации текста: отчёт обязан объяснять себя одинаково сегодня и
через полгода, а старые снапшоты — оставаться воспроизводимыми.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from api.i18n import SupportedLanguage, tr
from api.services.reports.metrics import ReportMetrics

# Версия набора. Пишется в снапшот; задним числом ничего не переписывает.
RULES_VERSION = 2

# Больше трёх советов человек не выполняет — он их пролистывает.
MAX_ACTIONS = 3

# [КОНФИГ] Ниже этой доли выполненных дней план считается не влезающим в
# неделю. 70 % — два пропуска из трёх дней уже срабатывают, один нет.
ADHERENCE_FLOOR = 0.7

# [КОНФИГ] Насколько средний RIR должен превысить цель, чтобы это считалось
# систематическим недогрузом, а не разбросом.
RIR_SLACK = 1.0

# [КОНФИГ] Ниже этой доли размеченных подходов средний RIR перестаёт быть
# фактом и превращается в шум.
LABELLED_FLOOR = 0.5


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    reason: str
    route: str
    title_key: str = ""
    body_key: str = ""
    params: dict[str, str | int | float | bool] = field(default_factory=dict)
    # Системный ключ мышцы (то же значение, что и в metrics.volume.by_muscle).
    # Отображаемое имя строит клиент — см. api/services/volume_calculator.py
    # про то, почему сервер не переводит его сам.
    muscle: str | None = None


@dataclass(frozen=True)
class RuleContext:
    pending_proposal_id: int | None
    pending_proposal_kind: str | None
    target_rir: int


def _action(
    action_id: str,
    route: str,
    language: SupportedLanguage,
    *,
    params: dict[str, str | int | float | bool] | None = None,
    muscle: str | None = None,
) -> Action:
    title_key = f"report.action.{action_id}.title"
    body_key = f"report.action.{action_id}.body"
    values = params or {}
    return Action(
        id=action_id,
        title=tr(language, title_key, **values),
        reason=tr(language, body_key, **values),
        route=route,
        title_key=title_key,
        body_key=body_key,
        params=values,
        muscle=muscle,
    )


def _adherence_low(
    metrics: ReportMetrics, _: RuleContext, language: SupportedLanguage
) -> Action | None:
    adherence = metrics.adherence
    if adherence.planned_days == 0 or adherence.rate >= ADHERENCE_FLOOR:
        return None
    return _action(
        "adherence_low", "/settings/training", language,
        params={
            "completed_days": adherence.completed_days,
            "planned_days": adherence.planned_days,
        },
    )


def _pending_proposal(
    _: ReportMetrics, context: RuleContext, language: SupportedLanguage
) -> Action | None:
    if context.pending_proposal_id is None:
        return None
    route = (
        "/periodization/window-summary"
        if context.pending_proposal_kind == "volume_review"
        else "/periodization"
    )
    return _action("pending_proposal", route, language)


def _volume_over_mrv(
    metrics: ReportMetrics, context: RuleContext, language: SupportedLanguage
) -> Action | None:
    # Предложение по итогам микроцикла уже владеет этим решением — отчёт не
    # должен предлагать свой вариант того же вопроса вторым путём.
    if context.pending_proposal_kind == "volume_review":
        return None
    over = [
        (muscle, row) for muscle, row in metrics.volume.by_muscle.items()
        if row.direct + row.indirect > row.mrv
    ]
    if not over:
        return None
    muscle, row = max(over, key=lambda item: item[1].direct + item[1].indirect)
    return _action(
        "volume_over_mrv", "/progress", language,
        params={"sets": f"{row.direct + row.indirect:.0f}", "limit": row.mrv},
        muscle=muscle,
    )


def _volume_below_mev(
    metrics: ReportMetrics, context: RuleContext, language: SupportedLanguage
) -> Action | None:
    # Предложение по итогам микроцикла уже владеет этим решением — отчёт не
    # должен предлагать свой вариант того же вопроса вторым путём.
    if context.pending_proposal_kind == "volume_review":
        return None
    below = [
        (muscle, row) for muscle, row in metrics.volume.by_muscle.items()
        if row.direct + row.indirect < row.mev
    ]
    if not below:
        return None
    muscle, row = min(below, key=lambda item: item[1].direct + item[1].indirect)
    return _action(
        "volume_below_mev", "/progress", language,
        params={"sets": f"{row.direct + row.indirect:.0f}", "minimum": row.mev},
        muscle=muscle,
    )


def _rir_too_easy(
    metrics: ReportMetrics, context: RuleContext, language: SupportedLanguage
) -> Action | None:
    effort = metrics.effort
    if effort.avg_rir is None or effort.labeled_share < LABELLED_FLOOR:
        return None
    if effort.avg_rir < context.target_rir + RIR_SLACK:
        return None
    return _action(
        "rir_too_easy", "/progress", language,
        params={"average_rir": f"{effort.avg_rir:.1f}", "target_rir": context.target_rir},
    )


def _no_records(
    metrics: ReportMetrics, _: RuleContext, language: SupportedLanguage
) -> Action | None:
    if metrics.records or metrics.time.sessions < 4:
        return None
    return _action(
        "no_records", "/progress", language,
        params={"sessions": metrics.time.sessions},
    )


def _effort_unlabelled(
    metrics: ReportMetrics, _: RuleContext, language: SupportedLanguage
) -> Action | None:
    if metrics.time.sessions == 0 or metrics.effort.labeled_share >= LABELLED_FLOOR:
        return None
    return _action(
        "effort_unlabelled", "/workout", language,
        params={"labeled_percent": f"{metrics.effort.labeled_share * 100:.0f}"},
    )


# Порядок = приоритет. Совет про метод (_effort_unlabelled) стоит последним
# намеренно: это способ, а не результат, и он не должен вытеснять
# содержательные действия при переполнении.
RULES = (
    _pending_proposal,
    _adherence_low,
    _volume_over_mrv,
    _volume_below_mev,
    _rir_too_easy,
    _no_records,
    _effort_unlabelled,
)


def build_actions(
    metrics: ReportMetrics,
    context: RuleContext,
    language: SupportedLanguage = "ru",
) -> list[Action]:
    """Не больше MAX_ACTIONS действий. Пустой список — валидный отчёт."""
    actions: list[Action] = []
    for rule in RULES:
        action = rule(metrics, context, language)
        if action is not None:
            actions.append(action)
        if len(actions) == MAX_ACTIONS:
            break
    return actions
