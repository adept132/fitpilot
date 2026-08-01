import asyncio
from datetime import date
from types import SimpleNamespace
import pytest
from api.services.scheduling_engine import SchedulingEngine


class FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return SimpleNamespace(all=lambda: self._rows)


class FakeSession:
    def __init__(self, days, plans):
        self._days, self._plans = days, plans
        self.committed = False
        self._calls = 0
    async def execute(self, stmt):
        # first execute -> plans, subsequent -> days (order matches implementation)
        self._calls += 1
        return FakeResult(self._plans if self._calls == 1 else self._days)
    async def commit(self): self.committed = True


def test_rebind_sets_plan_id_for_non_rest_days():
    plans = [SimpleNamespace(id=7, day_tag="push", meso_tag="adaptive", micro_tag="adaptive")]
    days = [
        SimpleNamespace(day_tag="Push", meso_tag="adaptive", micro_tag="adaptive",
                        is_rest_day=False, plan_id=None),
        SimpleNamespace(day_tag="Rest", meso_tag="adaptive", micro_tag="adaptive",
                        is_rest_day=True, plan_id=None),
    ]
    sess = FakeSession(days, plans)
    updated = asyncio.run(SchedulingEngine.rebind_plans(sess, app_user_id=1, from_date=date(2026,7,18)))
    assert days[0].plan_id == 7
    assert days[1].plan_id is None  # rest untouched
    assert updated == 1
    assert sess.committed is True
