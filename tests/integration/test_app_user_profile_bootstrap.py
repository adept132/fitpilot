"""The authenticated-user boundary keeps the domain profile invariant."""

import importlib
import uuid

import pytest
from sqlalchemy import delete, select

from api.services.app_user_service import get_or_create_app_user
from api.services.models import AppUser, AppUserProfile


@pytest.mark.asyncio
async def test_backfill_adds_only_missing_domain_profiles(db, test_user):
    """Deployment repair makes old auth-only accounts usable."""
    migration = importlib.import_module(
        "migrations.versions.20260908_01_backfill_app_user_profiles"
    )

    await db.run_sync(migration.backfill_missing_profiles)
    await db.commit()

    profiles = (
        await db.execute(
            select(AppUserProfile).where(
                AppUserProfile.app_user_id == test_user.id
            )
        )
    ).scalars().all()

    assert len(profiles) == 1

    # Re-running is safe and does not replace or duplicate the existing row.
    original_id = profiles[0].id
    await db.run_sync(migration.backfill_missing_profiles)
    await db.commit()
    profiles = (
        await db.execute(
            select(AppUserProfile).where(
                AppUserProfile.app_user_id == test_user.id
            )
        )
    ).scalars().all()
    assert [profile.id for profile in profiles] == [original_id]


@pytest.mark.asyncio
async def test_first_google_sign_in_creates_domain_profile(db):
    """A brand-new Firebase identity is immediately usable by profile APIs."""
    marker = uuid.uuid4().hex[:12]
    uid = f"google-{marker}"

    try:
        user = await get_or_create_app_user(
            db,
            {
                "uid": uid,
                "email": f"google-{marker}@example.com",
                "email_verified": True,
                "name": "Google User",
            },
        )

        profile = (
            await db.execute(
                select(AppUserProfile).where(AppUserProfile.app_user_id == user.id)
            )
        ).scalar_one_or_none()

        assert profile is not None
        assert profile.app_user_id == user.id
        assert profile.experience_level == "beginner"
        assert profile.training_frequency == 3
    finally:
        await db.rollback()
        await db.execute(delete(AppUser).where(AppUser.firebase_uid == uid))
        await db.commit()


@pytest.mark.asyncio
async def test_existing_auth_user_without_profile_is_repaired(db, test_user):
    """An app_users row alone must not leave a signed-in user locked out."""
    result = await get_or_create_app_user(
        db,
        {
            "uid": test_user.firebase_uid,
            "email": test_user.email,
            "email_verified": True,
            "name": test_user.display_name,
        },
    )

    profile = (
        await db.execute(
            select(AppUserProfile).where(AppUserProfile.app_user_id == result.id)
        )
    ).scalar_one_or_none()

    assert profile is not None
    assert profile.app_user_id == result.id
    assert profile.experience_level == "beginner"
    assert profile.training_frequency == 3
