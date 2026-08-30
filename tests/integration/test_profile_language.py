import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AppUser, AppUserProfile


@pytest_asyncio.fixture
async def with_profile(db: AsyncSession, test_user: AppUser):
    profile = AppUserProfile(app_user_id=test_user.id)
    db.add(profile)
    await db.commit()
    yield profile


@pytest.mark.asyncio
async def test_language_is_persisted_without_erasing_settings(
    client, auth_headers, with_profile
):
    initial_response = await client.patch(
        "/profile/settings", headers=auth_headers, json={"weight_unit": "kg"}
    )
    assert initial_response.status_code == 200, initial_response.text

    response = await client.patch(
        "/profile/settings", headers=auth_headers, json={"language": "en"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["settings"] == {"weight_unit": "kg", "language": "en"}


@pytest.mark.asyncio
async def test_unknown_language_is_rejected(client, auth_headers, with_profile):
    response = await client.patch(
        "/profile/settings", headers=auth_headers, json={"language": "de"}
    )

    assert response.status_code == 422
