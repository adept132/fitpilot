import asyncio
from datetime import date
from types import SimpleNamespace
from api.schemas.plan import ConfirmPlanRequest, GeneratedDayOut, GeneratedExerciseOut
import api.routers.plans as plans_mod


class FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalar_one_or_none(self): return self._rows[0] if self._rows else None
    def scalars(self): return SimpleNamespace(
        all=lambda: self._rows,
        first=lambda: self._rows[0] if self._rows else None,
    )


class FakeSession:
    def __init__(self, profile):
        self._profile = profile
        self.added = []
        self.committed = False
        self._exec = 0
        self._next_id = 100
    async def execute(self, stmt):
        self._exec += 1
        rows = [self._profile] if self._exec == 1 else []  # 2nd exec: no active UserSplit
        return FakeResult(rows)
    def add(self, obj):
        self.added.append(obj)
    async def flush(self):
        for o in self.added:
            if type(o).__name__ == "WorkoutPlan" and getattr(o, "id", None) is None:
                o.id = self._next_id
                self._next_id += 1
    async def commit(self):
        self.committed = True


def _day():
    return GeneratedDayOut(day_tag="push", day_name="Push", coverage={}, warnings=[],
        exercises=[GeneratedExerciseOut(exercise_id=1, name="Жим", target_sets=3,
            order_index=0, superset_group_id=None, fatigue_tier=1,
            primary_muscle="Грудь", secondary_muscle="Трицепс")])


def test_confirm_creates_plans_and_rebinds():
    profile = SimpleNamespace(experience_level="beginner")
    sess = FakeSession(profile)
    req = ConfirmPlanRequest(days=[_day()])
    current_user = SimpleNamespace(id=1)
    resp = asyncio.run(plans_mod.confirm_generated_plan(req, db=sess, current_user=current_user))
    assert resp.status == "success"
    assert resp.applied_from is not None
    assert len(resp.created_plan_ids) == 1
    plans_added = [o for o in sess.added if type(o).__name__ == "WorkoutPlan"]
    assert plans_added and plans_added[0].meso_tag == "adaptive"
    assert sess.committed is True


def test_confirm_preserves_prescription_and_never_rewrites_the_past():
    profile = SimpleNamespace(experience_level="beginner")
    sess = FakeSession(profile)
    day = _day()
    day.exercises[0].override_reps = "6-8"
    day.exercises[0].override_rir = 2
    req = ConfirmPlanRequest(days=[day], target_date=date(2000, 1, 1))

    resp = asyncio.run(plans_mod.confirm_generated_plan(
        req, db=sess, current_user=SimpleNamespace(id=1)
    ))

    exercise = next(
        obj for obj in sess.added if type(obj).__name__ == "WorkoutPlanExercise"
    )
    assert exercise.override_reps == "6-8"
    assert exercise.override_rir == 2
    assert resp.applied_from == date.today()


def test_generated_plans_bind_every_matching_split_day():
    upper = SimpleNamespace(id=101, day_tag="upper", meso_tag="adaptive", micro_tag="adaptive")
    lower = SimpleNamespace(id=102, day_tag="lower", meso_tag="adaptive", micro_tag="adaptive")
    split = SimpleNamespace(
        selected_plans={"1": 7, "3": 999},
        blueprint=SimpleNamespace(slots=[
            SimpleNamespace(day_order=1, day=SimpleNamespace(name="Upper")),
            SimpleNamespace(day_order=2, day=SimpleNamespace(name="Lower")),
            SimpleNamespace(day_order=3, day=SimpleNamespace(name="Rest")),
        ]),
    )

    updated = plans_mod._bind_generated_plans_to_split(split, [upper, lower])

    assert updated == 2
    assert split.selected_plans == {"1": 101, "2": 102, "3": 999}


def test_partial_bind_keeps_unrelated_split_plans():
    upper = SimpleNamespace(id=101, day_tag="upper", meso_tag="adaptive", micro_tag="adaptive")
    split = SimpleNamespace(
        selected_plans={"1": 7, "2": 8},
        blueprint=SimpleNamespace(slots=[
            SimpleNamespace(day_order=1, day=SimpleNamespace(name="Upper")),
            SimpleNamespace(day_order=2, day=SimpleNamespace(name="Lower")),
        ]),
    )

    plans_mod._bind_generated_plans_to_split(split, [upper])

    assert split.selected_plans == {"1": 101, "2": 8}


def test_confirm_single_day_requires_exactly_one_day():
    profile = SimpleNamespace(experience_level="beginner")
    sess = FakeSession(profile)
    req = ConfirmPlanRequest(
        days=[_day(), _day()],
        mode="single_day",
        target_date=date(2026, 8, 20),
    )

    try:
        asyncio.run(plans_mod.confirm_generated_plan(
            req, db=sess, current_user=SimpleNamespace(id=1)
        ))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("single_day accepted more than one generated day")
