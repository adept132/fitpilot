import pytest

from tests.integration.cleanup_test_guard import require_disposable_cleanup_database


def test_cleanup_integration_guard_rejects_an_ordinary_database_url():
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL"):
        require_disposable_cleanup_database({"DATABASE_URL": "postgresql+asyncpg://localhost/fitpilot"})


def test_cleanup_integration_guard_accepts_only_task_specific_local_database():
    require_disposable_cleanup_database(
        {"TEST_DATABASE_URL": "postgresql+asyncpg://localhost/fitpilot_task7_unique"}
    )
