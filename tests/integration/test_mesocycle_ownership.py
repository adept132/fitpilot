"""Мезоциклы не текут между пользователями (финальное ревью P1-03, правка 2)."""
import uuid
from datetime import date, datetime, timedelta, timezone
from importlib import import_module

import pytest
from sqlalchemy import delete, select, text, update

from api.services.models import (
    AppUser,
    AppUserMesocycle,
    Mesocycle,
    MesocyclePhase,
    TrainingBlock,
    WorkoutSession,
)
from api.services.periodization.repository import create_block
from api.services.periodization.types import PhaseSnapshot
from api.services.structure.bootstrap import ensure_structure
from app.database import SessionLocal

pytestmark = pytest.mark.asyncio


async def _make_other_user() -> int:
    marker = uuid.uuid4().hex[:12]
    async with SessionLocal() as db:
        user = AppUser(
            firebase_uid=f"test-{marker}",
            email=f"test-{marker}@example.com",
            display_name="Other Test User",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id


async def _make_mesocycle(
    author_id: int | None, name: str, *, phase_name: str = "Тест",
) -> str:
    async with SessionLocal() as db:
        meso = Mesocycle(
            author_id=author_id,
            name=name,
            code=f"test-{uuid.uuid4().hex[:8]}",
            phases_in_cycle=1,
        )
        db.add(meso)
        await db.flush()
        db.add(MesocyclePhase(
            mesocycle_id=meso.id,
            phase_number=1,
            name=phase_name,
            effort_tier="medium",
        ))
        await db.commit()
        return str(meso.id)


async def _make_relation_and_block(
    app_user_id: int,
    mesocycle_id: str,
    phase_name: str,
    *,
    block_index: int = 1,
    trusted: bool = False,
) -> tuple[int, int]:
    """Persist the exact legacy shape that existed before ownership checks."""
    async with SessionLocal() as db:
        relation = AppUserMesocycle(
            app_user_id=app_user_id,
            mesocycle_id=uuid.UUID(mesocycle_id),
            is_active=True,
            microcycle_length=7,
            current_phase=1,
        )
        db.add(relation)
        await db.flush()
        block = TrainingBlock(
            app_user_id=app_user_id,
            block_index=block_index,
            user_mesocycle_id=relation.id,
            mesocycle_id=uuid.UUID(mesocycle_id),
            phases=[{
                "phase_number": 1,
                "name": phase_name,
                "effort_tier": "medium",
                "length_days": 7,
            }],
            phase_snapshot_trusted=trusted,
            microcycle_length=7,
            start_date=date.today(),
            planned_end_date=date.today() + timedelta(days=6),
            status="active",
        )
        db.add(block)
        await db.commit()
        return relation.id, block.id


async def _delete_user(user_id: int) -> None:
    async with SessionLocal() as db:
        # ondelete="CASCADE" на mesocycles.author_id уносит и мезоцикл.
        await db.execute(delete(AppUser).where(AppUser.id == user_id))
        await db.commit()


async def test_foreign_mesocycle_not_visible_in_workout_center_context(
    client, auth_headers, test_user,
):
    other_id = await _make_other_user()
    try:
        foreign_name = f"Чужой {uuid.uuid4().hex[:8]}"
        foreign_id = await _make_mesocycle(other_id, foreign_name)

        r = await client.get("/workout-center/context", headers=auth_headers)
        assert r.status_code == 200, r.text
        ids = [m["id"] for m in r.json()["available_mesocycles"]]
        assert foreign_id not in ids
    finally:
        await _delete_user(other_id)


async def test_historical_foreign_active_relation_is_not_serialized(
    client, auth_headers, test_user,
):
    """A legacy bad join row must not expose another user's periodization."""
    # First create the user's normal structure.  That makes the later foreign
    # relation the only active one while ensuring the context bootstrap takes
    # its idempotent path and does not repair the fixture for us.
    async with SessionLocal() as db:
        await ensure_structure(db, test_user.id)
        await db.commit()

    other_id = await _make_other_user()
    try:
        foreign_name = f"TOP SECRET {uuid.uuid4().hex[:8]}"
        foreign_id = await _make_mesocycle(other_id, foreign_name)
        async with SessionLocal() as db:
            await db.execute(
                update(AppUserMesocycle)
                .where(AppUserMesocycle.app_user_id == test_user.id)
                .values(is_active=False)
            )
            db.add(AppUserMesocycle(
                app_user_id=test_user.id,
                mesocycle_id=uuid.UUID(foreign_id),
                is_active=True,
                microcycle_length=7,
                current_phase=1,
            ))
            await db.commit()

        response = await client.get("/workout-center/context", headers=auth_headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["selected_periodization"] is None
        assert foreign_name not in response.text
    finally:
        await _delete_user(other_id)


async def test_foreign_block_snapshot_is_absent_from_all_context_responses(
    client, auth_headers, test_user,
):
    """A pre-fix active block must not bypass relation ownership filtering."""
    other_id = await _make_other_user()
    try:
        secret = f"FOREIGN PHASE SECRET {uuid.uuid4().hex}"
        foreign_id = await _make_mesocycle(
            other_id,
            f"Foreign {uuid.uuid4().hex[:8]}",
            phase_name=secret,
        )
        await _make_relation_and_block(
            test_user.id, foreign_id, secret, trusted=True,
        )

        workout = await client.get("/workout-center/context", headers=auth_headers)
        periodization = await client.get(
            "/periodization/context",
            params={"local_date": date.today().isoformat()},
            headers=auth_headers,
        )

        assert workout.status_code == 200, workout.text
        assert periodization.status_code == 200, periodization.text
        assert secret not in workout.text
        assert secret not in periodization.text
    finally:
        await _delete_user(other_id)


async def test_cascade_null_foreign_snapshot_is_cleaned_and_never_serialized(
    client, auth_headers, test_user,
):
    """Deleting the source must not turn a foreign snapshot into a generic one."""
    other_id = await _make_other_user()
    secret = f"CASCADE NULL SECRET {uuid.uuid4().hex}"
    foreign_id = await _make_mesocycle(
        other_id,
        f"Cascade foreign {uuid.uuid4().hex[:8]}",
        phase_name=secret,
    )
    _, block_id = await _make_relation_and_block(test_user.id, foreign_id, secret)
    async with SessionLocal() as db:
        workout = WorkoutSession(
            app_user_id=test_user.id,
            source="free",
            status="finished",
            training_block_id=block_id,
            notes="cascade result survives",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        db.add(workout)
        await db.commit()
        workout_id = workout.id

    # author -> mesocycle CASCADE -> relation CASCADE -> both block FKs SET NULL
    await _delete_user(other_id)
    async with SessionLocal() as db:
        orphaned = await db.get(TrainingBlock, block_id)
        assert orphaned is not None
        assert orphaned.user_mesocycle_id is None
        assert orphaned.mesocycle_id is None

        relation_cleanup = import_module(
            "migrations.versions.20260909_01_deactivate_cross_user_mesocycles"
        )
        provenance_cleanup = import_module(
            "migrations.versions.20260909_02_trust_training_block_snapshots"
        )
        await db.run_sync(relation_cleanup.deactivate_cross_user_relations)
        await db.run_sync(
            provenance_cleanup.backfill_and_cleanup_block_provenance
        )
        await db.commit()

    workout_response = await client.get(
        "/workout-center/context", headers=auth_headers,
    )
    periodization_response = await client.get(
        "/periodization/context",
        params={"local_date": date.today().isoformat()},
        headers=auth_headers,
    )
    leaks = [
        label
        for label, response in (
            ("workout-center", workout_response),
            ("periodization", periodization_response),
        )
        if secret in response.text
    ]
    assert workout_response.status_code == 200, workout_response.text
    assert periodization_response.status_code == 200, periodization_response.text
    assert leaks == []

    async with SessionLocal() as db:
        cleaned = await db.get(TrainingBlock, block_id)
        preserved_workout = await db.get(WorkoutSession, workout_id)
        assert cleaned is not None
        assert cleaned.status == "closed"
        assert secret not in str(cleaned.phases)
        assert preserved_workout is not None
        assert preserved_workout.training_block_id == block_id
        assert preserved_workout.notes == "cascade result survives"


async def test_dangling_relation_source_is_not_treated_as_system_owned(
    client, auth_headers, test_user,
):
    """An absent outer-joined Mesocycle has NULL fields but is not system-owned."""
    secret = f"DANGLING RELATION SECRET {uuid.uuid4().hex}"
    missing_mesocycle_id = uuid.uuid4()
    constraint = "app_user_mesocycles_mesocycle_id_fkey"
    relation_id = None
    block_id = None
    async with SessionLocal() as db:
        await db.execute(text(
            f"ALTER TABLE app_user_mesocycles DROP CONSTRAINT {constraint}"
        ))
        await db.commit()
    try:
        async with SessionLocal() as db:
            relation = AppUserMesocycle(
                app_user_id=test_user.id,
                mesocycle_id=missing_mesocycle_id,
                is_active=True,
                microcycle_length=7,
                current_phase=1,
            )
            db.add(relation)
            await db.flush()
            block = TrainingBlock(
                app_user_id=test_user.id,
                block_index=1,
                user_mesocycle_id=relation.id,
                mesocycle_id=None,
                phases=[{
                    "phase_number": 1,
                    "name": secret,
                    "effort_tier": "medium",
                    "length_days": 7,
                }],
                phase_snapshot_trusted=True,
                microcycle_length=7,
                start_date=date.today(),
                planned_end_date=date.today() + timedelta(days=6),
                status="active",
            )
            db.add(block)
            await db.commit()
            relation_id = relation.id
            block_id = block.id

        workout_response = await client.get(
            "/workout-center/context", headers=auth_headers,
        )
        periodization_response = await client.get(
            "/periodization/context",
            params={"local_date": date.today().isoformat()},
            headers=auth_headers,
        )
        leaks = [
            label
            for label, response in (
                ("workout-center", workout_response),
                ("periodization", periodization_response),
            )
            if secret in response.text
        ]
        assert workout_response.status_code == 200, workout_response.text
        assert periodization_response.status_code == 200, periodization_response.text
        assert leaks == []

        provenance_cleanup = import_module(
            "migrations.versions.20260909_02_trust_training_block_snapshots"
        )
        async with SessionLocal() as db:
            await db.run_sync(
                provenance_cleanup.backfill_and_cleanup_block_provenance
            )
            await db.commit()
            cleaned = await db.get(TrainingBlock, block_id)
            assert cleaned is not None
            assert cleaned.phase_snapshot_trusted is False
            assert cleaned.status == "closed"
            assert secret not in str(cleaned.phases)
    finally:
        async with SessionLocal() as db:
            if block_id is not None:
                await db.execute(
                    delete(TrainingBlock).where(TrainingBlock.id == block_id)
                )
            if relation_id is not None:
                await db.execute(
                    delete(AppUserMesocycle).where(
                        AppUserMesocycle.id == relation_id
                    )
                )
            await db.commit()
            await db.execute(text(
                f"ALTER TABLE app_user_mesocycles ADD CONSTRAINT {constraint} "
                "FOREIGN KEY (mesocycle_id) REFERENCES mesocycles(id) "
                "ON DELETE CASCADE"
            ))
            await db.commit()


async def test_owned_block_snapshot_remains_visible(
    client, auth_headers, test_user,
):
    """The runtime guard must not hide a legitimate user-owned snapshot."""
    phase_name = f"OWN PHASE {uuid.uuid4().hex}"
    own_id = await _make_mesocycle(
        test_user.id,
        f"Own {uuid.uuid4().hex[:8]}",
        phase_name=phase_name,
    )
    await _make_relation_and_block(
        test_user.id, own_id, phase_name, trusted=True,
    )

    workout = await client.get("/workout-center/context", headers=auth_headers)

    assert workout.status_code == 200, workout.text
    assert workout.json()["active_block"]["phase_name"] == phase_name


async def test_cleanup_migration_deactivates_only_cross_user_relations(test_user):
    """The repair keeps own/system selections and neutralizes legacy leaks."""
    other_id = await _make_other_user()
    system_id = None
    try:
        own_id = await _make_mesocycle(test_user.id, f"Own {uuid.uuid4().hex[:8]}")
        system_id = await _make_mesocycle(None, f"System {uuid.uuid4().hex[:8]}")
        secret = f"MIGRATION SECRET {uuid.uuid4().hex}"
        foreign_id = await _make_mesocycle(
            other_id,
            f"Foreign {uuid.uuid4().hex[:8]}",
            phase_name=secret,
        )
        async with SessionLocal() as db:
            relations = [
                AppUserMesocycle(
                    app_user_id=test_user.id,
                    mesocycle_id=uuid.UUID(mesocycle_id),
                    is_active=True,
                    microcycle_length=7,
                    current_phase=1,
                )
                for mesocycle_id in (own_id, system_id, foreign_id)
            ]
            db.add_all(relations)
            await db.commit()
            relation_ids = [relation.id for relation in relations]

            blocks = []
            for index, (relation, phase_name) in enumerate(
                zip(relations, ("OWN VALID", "SYSTEM VALID", secret)), start=1
            ):
                block = TrainingBlock(
                    app_user_id=test_user.id,
                    block_index=index,
                    user_mesocycle_id=relation.id,
                    mesocycle_id=relation.mesocycle_id,
                    phases=[{
                        "phase_number": 1,
                        "name": phase_name,
                        "effort_tier": "medium",
                        "length_days": 7,
                    }],
                    microcycle_length=7,
                    start_date=date.today(),
                    planned_end_date=date.today() + timedelta(days=6),
                    status="active",
                )
                db.add(block)
                blocks.append(block)
            await db.flush()
            workout = WorkoutSession(
                app_user_id=test_user.id,
                source="free",
                status="finished",
                training_block_id=blocks[-1].id,
                notes="legitimate result data",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            )
            db.add(workout)
            await db.commit()
            block_ids = [block.id for block in blocks]
            workout_id = workout.id

        relation_cleanup = import_module(
            "migrations.versions.20260909_01_deactivate_cross_user_mesocycles"
        )
        provenance_cleanup = import_module(
            "migrations.versions.20260909_02_trust_training_block_snapshots"
        )
        async with SessionLocal() as db:
            await db.run_sync(relation_cleanup.deactivate_cross_user_relations)
            await db.run_sync(
                provenance_cleanup.backfill_and_cleanup_block_provenance
            )
            await db.commit()
            rows = (await db.execute(
                select(AppUserMesocycle).where(
                    AppUserMesocycle.id.in_(relation_ids)
                )
            )).scalars().all()
            migrated_blocks = (await db.execute(
                select(TrainingBlock)
                .where(TrainingBlock.id.in_(block_ids))
                .order_by(TrainingBlock.block_index)
            )).scalars().all()
            preserved_workout = await db.get(WorkoutSession, workout_id)

        active_by_meso = {str(row.mesocycle_id): row.is_active for row in rows}
        assert active_by_meso[own_id] is True
        assert active_by_meso[system_id] is True
        assert active_by_meso[foreign_id] is False
        assert migrated_blocks[0].status == "active"
        assert migrated_blocks[0].phase_snapshot_trusted is True
        assert migrated_blocks[0].phases[0]["name"] == "OWN VALID"
        assert migrated_blocks[1].status == "active"
        assert migrated_blocks[1].phase_snapshot_trusted is True
        assert migrated_blocks[1].phases[0]["name"] == "SYSTEM VALID"
        assert migrated_blocks[2].status == "closed"
        assert migrated_blocks[2].phase_snapshot_trusted is False
        assert migrated_blocks[2].close_reason == "ownership_invalid"
        assert migrated_blocks[2].user_mesocycle_id is None
        assert migrated_blocks[2].mesocycle_id is None
        assert secret not in str(migrated_blocks[2].phases)
        assert migrated_blocks[2].phases[0]["name"] == "medium"
        assert preserved_workout is not None
        assert preserved_workout.training_block_id == migrated_blocks[2].id
        assert preserved_workout.notes == "legitimate result data"
    finally:
        if system_id is not None:
            async with SessionLocal() as db:
                await db.execute(
                    delete(Mesocycle).where(Mesocycle.id == uuid.UUID(system_id))
                )
                await db.commit()
        await _delete_user(other_id)


async def test_system_owned_block_snapshot_remains_trusted(
    client, auth_headers, test_user,
):
    system_id = await _make_mesocycle(
        None,
        f"System block {uuid.uuid4().hex[:8]}",
        phase_name=f"SYSTEM PHASE {uuid.uuid4().hex}",
    )
    try:
        _, block_id = await _make_relation_and_block(
            test_user.id,
            system_id,
            "SYSTEM PHASE",
            trusted=True,
        )
        response = await client.get(
            "/workout-center/context", headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["active_block"]["block_id"] == block_id
    finally:
        async with SessionLocal() as db:
            await db.execute(
                delete(Mesocycle).where(Mesocycle.id == uuid.UUID(system_id))
            )
            await db.commit()


async def test_new_generic_block_is_explicitly_trusted_and_serialized(
    client, auth_headers, test_user,
):
    phase_name = f"GENERIC PHASE {uuid.uuid4().hex}"
    async with SessionLocal() as db:
        block = await create_block(
            db,
            test_user.id,
            date.today(),
            block_index=1,
            phases=(PhaseSnapshot(
                phase_number=1,
                name=phase_name,
                effort_tier="medium",
                length_days=7,
            ),),
            user_meso=None,
            user_micro=None,
            phase_snapshot_trusted=True,
        )
        await db.commit()
        block_id = block.id
        assert block.phase_snapshot_trusted is True

    response = await client.get(
        "/workout-center/context", headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["active_block"]["block_id"] == block_id
    assert response.json()["active_block"]["phase_name"] == phase_name


async def test_provenance_migration_quarantines_only_untrusted_generic_rows(
    test_user,
):
    """Authorized tradeoff: old generic is closed; explicit new origin survives."""
    legacy_secret = f"LEGACY GENERIC SECRET {uuid.uuid4().hex}"
    current_name = f"CURRENT GENERIC {uuid.uuid4().hex}"
    async with SessionLocal() as db:
        legacy = TrainingBlock(
            app_user_id=test_user.id,
            block_index=1,
            phases=[{
                "phase_number": 1,
                "name": legacy_secret,
                "effort_tier": "medium",
                "length_days": 7,
            }],
            phase_snapshot_trusted=False,
            microcycle_length=7,
            start_date=date.today(),
            planned_end_date=date.today() + timedelta(days=6),
            status="active",
        )
        current = TrainingBlock(
            app_user_id=test_user.id,
            block_index=2,
            phases=[{
                "phase_number": 1,
                "name": current_name,
                "effort_tier": "medium",
                "length_days": 7,
            }],
            phase_snapshot_trusted=True,
            microcycle_length=7,
            start_date=date.today(),
            planned_end_date=date.today() + timedelta(days=6),
            status="active",
        )
        db.add_all([legacy, current])
        await db.commit()
        legacy_id, current_id = legacy.id, current.id

        migration = import_module(
            "migrations.versions.20260909_02_trust_training_block_snapshots"
        )
        await db.run_sync(migration.backfill_and_cleanup_block_provenance)
        await db.commit()
        db.expire_all()

        migrated_legacy = await db.get(TrainingBlock, legacy_id)
        migrated_current = await db.get(TrainingBlock, current_id)
        assert migrated_legacy is not None
        assert migrated_legacy.status == "closed"
        assert migrated_legacy.phase_snapshot_trusted is False
        assert legacy_secret not in str(migrated_legacy.phases)
        assert migrated_current is not None
        assert migrated_current.status == "active"
        assert migrated_current.phase_snapshot_trusted is True
        assert migrated_current.phases[0]["name"] == current_name


async def test_system_mesocycle_visible_in_workout_center_context(
    client, auth_headers, test_user,
):
    system_name = f"Системный {uuid.uuid4().hex[:8]}"
    system_id = await _make_mesocycle(None, system_name)
    try:
        r = await client.get("/workout-center/context", headers=auth_headers)
        assert r.status_code == 200, r.text
        ids = [m["id"] for m in r.json()["available_mesocycles"]]
        assert system_id in ids
    finally:
        async with SessionLocal() as db:
            await db.execute(delete(Mesocycle).where(Mesocycle.id == system_id))
            await db.commit()


async def test_get_mesocycles_list_filters_by_owner(client, auth_headers, test_user):
    other_id = await _make_other_user()
    try:
        foreign_name = f"Чужой {uuid.uuid4().hex[:8]}"
        foreign_id = await _make_mesocycle(other_id, foreign_name)
        own_id = await _make_mesocycle(test_user.id, f"Своё {uuid.uuid4().hex[:8]}")

        r = await client.get("/mesocycles/", headers=auth_headers)
        assert r.status_code == 200, r.text
        ids = [m["id"] for m in r.json()]
        assert own_id in ids
        assert foreign_id not in ids
    finally:
        await _delete_user(other_id)


async def test_activating_foreign_mesocycle_is_404(client, auth_headers, test_user):
    other_id = await _make_other_user()
    try:
        foreign_id = await _make_mesocycle(other_id, f"Чужой {uuid.uuid4().hex[:8]}")

        r = await client.patch(
            "/workout-center/context/mesocycle",
            json={"mesocycle_id": foreign_id, "microcycle_length": 7},
            headers=auth_headers,
        )
        assert r.status_code == 404, r.text
    finally:
        await _delete_user(other_id)
