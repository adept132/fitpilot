"""GET /splits/suggest: кандидаты по частоте и требованию (P1-03 ч.1, §6.1)."""
import json

import pytest

from api.seed_splits import ensure_system_splits
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _seeded():
    async with SessionLocal() as db:
        await ensure_system_splits(db)
        await db.commit()


async def test_suggest_returns_at_most_three_candidates_for_the_frequency(
    client, auth_headers,
):
    r = await client.get("/splits/suggest?training_frequency=4", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert 1 <= len(body) <= 3
    for item in body:
        assert abs(item["sessions_per_week"] - 4) <= 0.5
        assert item["reason"]


async def test_two_days_a_week_returns_fewer_than_three(client, auth_headers):
    r = await client.get("/splits/suggest?training_frequency=2", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 2


async def test_requirement_narrows_the_result(client, auth_headers):
    without_requirement = await client.get(
        "/splits/suggest?training_frequency=4", headers=auth_headers,
    )
    assert without_requirement.status_code == 200, without_requirement.text
    baseline = without_requirement.json()

    # Требование, отсекающее хотя бы одного кандидата на той же частоте:
    # min=99 не наберёт ни один сплит на четырёх днях, значит фильтр по
    # requirement реально что-то делает, а не является no-op.
    requirement = json.dumps({"any_of": ["legs", "lower", "full_body"], "min": 99})
    r = await client.get(
        f"/splits/suggest?training_frequency=4&requirement={requirement}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    narrowed = r.json()

    assert len(narrowed) < len(baseline), (
        "requirement должен отсекать хотя бы одного кандидата на той же частоте",
        baseline, narrowed,
    )


async def test_seven_days_a_week_falls_back_to_six(client, auth_headers):
    # Сплита на семь тренировок нет намеренно (§5.1). Спека §7 требует
    # показать шестидневных кандидатов, а не пустой список.
    r = await client.get("/splits/suggest?training_frequency=7", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body
    assert all(item["sessions_per_week"] == 6 for item in body)


async def test_malformed_requirement_is_rejected(client, auth_headers):
    r = await client.get(
        "/splits/suggest?training_frequency=4&requirement=not-json",
        headers=auth_headers,
    )
    assert r.status_code == 400
