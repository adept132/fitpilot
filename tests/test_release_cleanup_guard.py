import pytest

from tests.integration.cleanup_test_guard import require_disposable_cleanup_database


def test_cleanup_integration_guard_rejects_an_ordinary_database_url():
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL"):
        require_disposable_cleanup_database({"DATABASE_URL": "postgresql+asyncpg://localhost/fitpilot"})


@pytest.mark.parametrize("task_number", [7, 8])
def test_cleanup_integration_guard_accepts_release_verification_databases(
    task_number: int,
):
    require_disposable_cleanup_database(
        {
            "TEST_DATABASE_URL": (
                f"postgresql+asyncpg://localhost/fitpilot_task{task_number}_unique"
            )
        }
    )
