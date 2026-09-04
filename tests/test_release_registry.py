from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, TextClause, UnaryExpression

from api.services.models import AppRelease, AppReleaseLane
from api.services.release_registry import (
    ReleaseConflictError,
    IdempotencyConflictError,
    StaleReleaseError,
    VersionConflictError,
    advance_github_mobile_push_targets,
    VersionRegressionError,
    advisory_key,
    latest_instruction,
    publish_direct_release,
    publish_eas_release,
    set_expected_commit,
    set_mandatory,
    withdraw_release,
)


class _Scalars:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def all(self) -> list[object]:
        return list(self.values)

    def first(self) -> object | None:
        return self.values[0] if self.values else None


class _Result:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def scalar_one_or_none(self) -> object | None:
        if len(self.values) > 1:
            raise AssertionError("registry query expected at most one row")
        return self.values[0] if self.values else None

    def scalars(self) -> _Scalars:
        return _Scalars(self.values)


class RegistrySession:
    """Small in-memory unit-of-work that executes the registry's SQLAlchemy statements.

    SQLite is not an adequate release-registry substitute: the production models use
    PostgreSQL JSONB and the policy must emit a PostgreSQL advisory transaction lock.
    This fixture keeps the registry's query boundary real while recording that exact
    PostgreSQL statement and using an isolated in-memory unit of work for policy tests.
    """

    def __init__(self) -> None:
        self.lanes: list[AppReleaseLane] = []
        self.releases: list[AppRelease] = []
        self._pending: list[object] = []
        self._in_transaction = False
        self.advisory_keys: list[int] = []
        self.operations: list[str] = []
        self._clock = datetime(2026, 9, 2, tzinfo=UTC)

    def in_transaction(self) -> bool:
        return self._in_transaction

    @asynccontextmanager
    async def begin(self):
        if self._in_transaction:
            raise AssertionError("nested transaction was not expected")
        self._in_transaction = True
        try:
            yield self
        finally:
            self._in_transaction = False

    @asynccontextmanager
    async def begin_nested(self):
        yield self

    def add(self, row: object) -> None:
        self._pending.append(row)

    async def flush(self) -> None:
        for row in self._pending:
            if isinstance(row, AppRelease):
                if row.id is None:
                    row.id = uuid4()
                if row.published_at is None:
                    row.published_at = self._tick()
                self.releases.append(row)
            elif isinstance(row, AppReleaseLane):
                if row.updated_at is None:
                    row.updated_at = self._tick()
                self.lanes.append(row)
            else:
                raise AssertionError(f"unexpected row type: {type(row)!r}")
        self._pending.clear()

    async def execute(self, statement, params=None):
        if isinstance(statement, TextClause):
            assert statement.text == "SELECT pg_advisory_xact_lock(:key)"
            self.advisory_keys.append(params["key"])
            self.operations.append("lock")
            return _Result([])

        entity = statement.column_descriptions[0]["entity"]
        if entity is AppReleaseLane:
            self.operations.append("lane-read")
            rows = [row for row in self.lanes if self._matches(row, statement.whereclause)]
        elif entity is AppRelease:
            rows = [row for row in self.releases if self._matches(row, statement.whereclause)]
        else:
            raise AssertionError(f"unexpected query entity: {entity!r}")
        return _Result(self._ordered(rows, statement._order_by_clauses))

    def _tick(self) -> datetime:
        self._clock += timedelta(microseconds=1)
        return self._clock

    @classmethod
    def _matches(cls, row: object, clause) -> bool:
        if clause is None:
            return True
        if isinstance(clause, BooleanClauseList):
            return all(cls._matches(row, item) for item in clause.clauses)
        if isinstance(clause, BinaryExpression):
            expected = getattr(clause.right, "value", clause.right)
            return getattr(row, clause.left.key) == expected
        raise AssertionError(f"unsupported filter: {clause!r}")

    @staticmethod
    def _ordered(rows: list[object], clauses) -> list[object]:
        ordered = list(rows)
        for clause in reversed(clauses):
            if not isinstance(clause, UnaryExpression):
                raise AssertionError(f"unsupported ordering: {clause!r}")
            ordered.sort(key=lambda row: getattr(row, clause.element.key), reverse=True)
        return ordered


class RacingRegistrySession(RegistrySession):
    """Injects a concurrently committed unique row at the persistence boundary."""

    def arm_unique_race(self, release: AppRelease) -> None:
        self._racing_release = release

    async def flush(self) -> None:
        release = getattr(self, "_racing_release", None)
        if release is not None:
            self._racing_release = None
            self._pending.clear()
            if release.id is None:
                release.id = uuid4()
            if release.published_at is None:
                release.published_at = self._tick()
            self.releases.append(release)
            raise IntegrityError("INSERT app_releases", {}, RuntimeError("unique violation"))
        await super().flush()


def command(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "platform": "android",
        "channel": "production-direct",
        "version_code": 2,
        "version_name": "1.0.1",
        "runtime_version": None,
        "fingerprint": "fingerprint-2",
        "release_notes": {"ru": "Исправления", "en": "Fixes"},
        "min_supported_version_code": None,
        "source_commit": "a" * 40,
        "ci_run_id": "run-2",
        "idempotency_key": "direct-2",
        "eas_build_id": None,
        "eas_update_group_id": None,
        "is_mandatory": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def stored(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "storage_key": "android/sha256/" + "1" * 64 + ".apk",
        "sha256": "1" * 64,
        "size_bytes": 123,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def latest(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "platform": "android",
        "channel": "production-direct",
        "current_version_code": 1,
        "runtime_version": "runtime-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def direct_row(value: SimpleNamespace, artifact: SimpleNamespace) -> AppRelease:
    return AppRelease(
        id=uuid4(),
        platform=value.platform,
        channel=value.channel,
        delivery_method="direct_apk",
        version_code=value.version_code,
        version_name=value.version_name,
        runtime_version=value.runtime_version,
        fingerprint=value.fingerprint,
        release_notes=value.release_notes,
        status="published",
        is_mandatory=False,
        min_supported_version_code=value.min_supported_version_code,
        artifact_storage_key=artifact.storage_key,
        artifact_sha256=artifact.sha256,
        artifact_size_bytes=artifact.size_bytes,
        source_commit=value.source_commit,
        ci_run_id=value.ci_run_id,
        idempotency_key=value.idempotency_key,
        eas_build_id=value.eas_build_id,
        eas_update_group_id=None,
        published_at=datetime(2026, 9, 2, tzinfo=UTC),
    )


def eas_row(value: SimpleNamespace) -> AppRelease:
    return AppRelease(
        id=uuid4(),
        platform=value.platform,
        channel=value.channel,
        delivery_method="eas_update",
        version_code=value.version_code,
        version_name=value.version_name,
        runtime_version=value.runtime_version,
        fingerprint=value.fingerprint,
        release_notes=value.release_notes,
        status="published",
        is_mandatory=False,
        min_supported_version_code=value.min_supported_version_code,
        artifact_storage_key=None,
        artifact_sha256=None,
        artifact_size_bytes=None,
        source_commit=value.source_commit,
        ci_run_id=value.ci_run_id,
        idempotency_key=value.idempotency_key,
        eas_build_id=value.eas_build_id,
        eas_update_group_id=value.eas_update_group_id,
        published_at=datetime(2026, 9, 2, tzinfo=UTC),
    )


async def publish_direct(session: RegistrySession, **overrides: object):
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "run-2")
    return await publish_direct_release(session, command(**overrides), stored())


async def test_stale_commit_cannot_publish() -> None:
    session = RegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "10")

    with pytest.raises(StaleReleaseError):
        await publish_direct_release(session, command(source_commit="b" * 40), stored())


async def test_direct_publish_requires_the_expected_ci_run_for_the_expected_commit() -> None:
    session = RegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "target")

    with pytest.raises(StaleReleaseError):
        await publish_direct_release(session, command(ci_run_id="other-run"), stored())

    first = await publish_direct_release(
        session,
        command(ci_run_id="target", idempotency_key="matching-direct"),
        stored(),
    )
    second = await publish_direct_release(
        session,
        command(ci_run_id="target", idempotency_key="matching-direct"),
        stored(),
    )

    assert second.release.id == first.release.id
    assert second.created is False


async def test_eas_publish_requires_the_expected_ci_run_for_the_expected_commit() -> None:
    session = RegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "target")

    with pytest.raises(StaleReleaseError):
        await publish_eas_release(
            session,
            command(
                ci_run_id="other-run",
                version_code=1,
                version_name="1.0.0",
                runtime_version="runtime-1",
                eas_update_group_id="matching-group",
                idempotency_key="matching-eas",
            ),
        )

    matching = command(
        ci_run_id="target",
        version_code=1,
        version_name="1.0.0",
        runtime_version="runtime-1",
        eas_update_group_id="matching-group",
        idempotency_key="matching-eas",
    )
    first = await publish_eas_release(session, matching)
    second = await publish_eas_release(session, matching)

    assert second.release.id == first.release.id
    assert second.created is False


async def test_idempotent_retry_returns_existing_release() -> None:
    session = RegistrySession()
    first = await publish_direct(session, idempotency_key="same")
    second = await publish_direct(session, idempotency_key="same")

    assert second.release.id == first.release.id
    assert second.created is False
    assert len(session.releases) == 1


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("version_name", "2.0.0"),
        ("runtime_version", "runtime-other"),
        ("fingerprint", "fingerprint-other"),
        ("release_notes", {"ru": "Другое", "en": "Different"}),
        ("min_supported_version_code", 2),
        ("eas_build_id", "other-build"),
    ],
)
async def test_direct_idempotency_key_rejects_every_changed_immutable_command_field(
    field: str, changed: object
) -> None:
    session = RegistrySession()
    await publish_direct(session, idempotency_key="same")

    with pytest.raises(IdempotencyConflictError):
        await publish_direct_release(session, command(idempotency_key="same", **{field: changed}), stored())


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("storage_key", "android/sha256/other.apk"),
        ("sha256", "2" * 64),
        ("size_bytes", 999),
    ],
)
async def test_direct_idempotency_key_rejects_every_changed_artifact_field(
    field: str, changed: object
) -> None:
    session = RegistrySession()
    await publish_direct(session, idempotency_key="same")

    with pytest.raises(IdempotencyConflictError):
        await publish_direct_release(session, command(idempotency_key="same"), stored(**{field: changed}))


async def test_eas_idempotency_key_rejects_a_different_runtime() -> None:
    session = RegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "run-2")
    original = command(
        version_code=1,
        version_name="1.0.0",
        runtime_version="runtime-1",
        eas_update_group_id="group-1",
        idempotency_key="ota-same",
    )
    await publish_eas_release(session, original)

    with pytest.raises(IdempotencyConflictError):
        await publish_eas_release(session, command(
            version_code=1,
            version_name="1.0.0",
            runtime_version="runtime-other",
            eas_update_group_id="group-1",
            idempotency_key="ota-same",
        ))


async def test_unique_idempotency_race_returns_the_complete_matching_release() -> None:
    session = RacingRegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "run-2")
    value = command(idempotency_key="racing")
    artifact = stored()
    remote = direct_row(value, artifact)
    session.arm_unique_race(remote)

    result = await publish_direct_release(session, value, artifact)

    assert result.release.id == remote.id
    assert result.created is False


async def test_cross_lane_idempotency_unique_race_is_a_registry_conflict() -> None:
    session = RacingRegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "run-2")
    await set_expected_commit(session, "android", "production-play", "a" * 40, "run-2")
    remote = direct_row(command(idempotency_key="shared"), stored())
    session.arm_unique_race(remote)

    with pytest.raises(IdempotencyConflictError):
        await publish_direct_release(
            session,
            command(channel="production-play", idempotency_key="shared"),
            stored(),
        )


async def test_cross_lane_eas_group_unique_race_is_a_registry_conflict() -> None:
    session = RacingRegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "run-2")
    await set_expected_commit(session, "android", "production-play", "a" * 40, "run-2")
    remote = eas_row(command(
        version_code=1,
        version_name="1.0.0",
        runtime_version="runtime-1",
        eas_update_group_id="shared-group",
        idempotency_key="direct-group",
    ))
    session.arm_unique_race(remote)

    with pytest.raises(ReleaseConflictError):
        await publish_eas_release(session, command(
            channel="production-play",
            version_code=1,
            version_name="1.0.0",
            runtime_version="runtime-1",
            eas_update_group_id="shared-group",
            idempotency_key="play-group",
        ))


async def test_lane_lock_is_stable_signed_int64_and_isolated_by_channel() -> None:
    session = RegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "run-2")
    await set_expected_commit(session, "android", "production-play", "b" * 40, "run-2")

    assert session.advisory_keys[-2:] == [
        advisory_key("android", "production-direct"),
        advisory_key("android", "production-play"),
    ]
    assert advisory_key("android", "production-direct") == int.from_bytes(
        sha256(b"android:production-direct").digest()[:8], "big", signed=True
    )
    assert -(2**63) <= session.advisory_keys[-1] < 2**63


async def test_github_target_advance_locks_both_lanes_before_reading_either() -> None:
    session = RegistrySession()

    duplicate = await advance_github_mobile_push_targets(
        session,
        before="0" * 40,
        source_commit="a" * 40,
    )

    assert duplicate is False
    assert session.advisory_keys[-2:] == [
        advisory_key("android", "production-direct"),
        advisory_key("android", "production-play"),
    ]
    assert session.operations == ["lock", "lock", "lane-read", "lane-read"]
    assert [(lane.channel, lane.expected_source_commit, lane.expected_ci_run_id) for lane in session.lanes] == [
        ("production-direct", "a" * 40, None),
        ("production-play", "a" * 40, None),
    ]


async def test_lane_isolation_rejects_a_commit_expected_only_in_another_channel() -> None:
    session = RegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "run-2")
    await set_expected_commit(session, "android", "production-play", "b" * 40, "run-2")

    with pytest.raises(StaleReleaseError):
        await publish_direct_release(session, command(source_commit="b" * 40), stored())


async def test_direct_version_regression_is_rejected() -> None:
    session = RegistrySession()
    await publish_direct(session, version_code=3, version_name="1.0.2", idempotency_key="v3")

    with pytest.raises(VersionRegressionError):
        await publish_direct_release(session, command(version_code=2, idempotency_key="v2"), stored())


async def test_same_direct_version_with_different_sha_is_a_conflict() -> None:
    session = RegistrySession()
    await publish_direct(session, version_code=3, version_name="1.0.2", idempotency_key="first")

    with pytest.raises(VersionConflictError):
        await publish_direct_release(
            session,
            command(version_code=3, version_name="1.0.2", idempotency_key="second"),
            stored(sha256="2" * 64, storage_key="android/sha256/" + "2" * 64 + ".apk"),
        )


async def test_withdrawn_release_is_excluded_from_latest() -> None:
    session = RegistrySession()
    published = await publish_direct(session)
    await withdraw_release(session, published.release.id, "broken startup")

    result = await latest_instruction(session, latest())

    assert result.release is None
    assert result.update_available is False


async def test_repeated_withdrawal_preserves_the_original_reason_and_time() -> None:
    session = RegistrySession()
    published = await publish_direct(session)
    first = await withdraw_release(session, published.release.id, "broken startup")
    first_reason = first.withdrawal_reason
    first_time = first.withdrawn_at

    second = await withdraw_release(session, published.release.id, "different later reason")

    assert second.withdrawal_reason == first_reason
    assert second.withdrawn_at == first_time


async def test_newer_direct_binary_has_priority_over_runtime_compatible_ota() -> None:
    session = RegistrySession()
    direct = await publish_direct(session, version_code=3, version_name="1.0.2")
    ota = await publish_eas_release(
        session,
        command(
            delivery_method="eas_update",
            version_code=1,
            version_name="1.0.0",
            runtime_version="runtime-1",
            eas_update_group_id="group-1",
            idempotency_key="ota-1",
        ),
    )

    result = await latest_instruction(session, latest(current_version_code=1))

    assert result.release.id == direct.release.id
    assert result.release.id != ota.release.id


async def test_ota_requires_matching_runtime_and_current_native_version() -> None:
    session = RegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "run-2")
    ota = await publish_eas_release(
        session,
        command(
            delivery_method="eas_update",
            version_code=2,
            runtime_version="runtime-2",
            eas_update_group_id="group-2",
            idempotency_key="ota-2",
        ),
    )

    assert (await latest_instruction(session, latest(current_version_code=2, runtime_version="runtime-1"))).release is None
    assert (await latest_instruction(session, latest(current_version_code=1, runtime_version="runtime-2"))).release is None
    assert (await latest_instruction(session, latest(current_version_code=2, runtime_version="runtime-2"))).release.id == ota.release.id


@pytest.mark.parametrize("current_version_code", [2, 3])
async def test_direct_binary_is_not_offered_when_current_version_is_equal_or_newer(
    current_version_code: int,
) -> None:
    session = RegistrySession()
    await publish_direct(session, version_code=2, idempotency_key="v2")

    result = await latest_instruction(session, latest(current_version_code=current_version_code))

    assert result.release is None
    assert result.update_available is False


async def test_automatic_publication_never_sets_mandatory() -> None:
    session = RegistrySession()
    published = await publish_direct(session)

    assert published.release.is_mandatory is False


async def test_automatic_eas_publication_never_sets_mandatory() -> None:
    session = RegistrySession()
    await set_expected_commit(session, "android", "production-direct", "a" * 40, "run-2")

    published = await publish_eas_release(session, command(
        version_code=1,
        version_name="1.0.0",
        runtime_version="runtime-1",
        eas_update_group_id="group-1",
        idempotency_key="ota-1",
        is_mandatory=True,
    ))

    assert published.release.is_mandatory is False


async def test_only_current_latest_published_release_can_be_marked_mandatory() -> None:
    session = RegistrySession()
    older = await publish_direct(session, version_code=2, idempotency_key="v2")
    newer = await publish_direct(session, version_code=3, version_name="1.0.2", idempotency_key="v3")

    with pytest.raises(ReleaseConflictError):
        await set_mandatory(session, older.release.id, True)
    changed = await set_mandatory(session, newer.release.id, True)

    assert changed.is_mandatory is True


async def test_withdrawn_installed_direct_forces_newer_direct_mandatory() -> None:
    session = RegistrySession()
    installed = await publish_direct(session, version_code=2, idempotency_key="v2")
    newer = await publish_direct(session, version_code=3, version_name="1.0.2", idempotency_key="v3")
    await withdraw_release(session, installed.release.id, "security issue")

    result = await latest_instruction(session, latest(current_version_code=2))

    assert result.release.id == newer.release.id
    assert result.current_release_withdrawn is True
    assert result.mandatory is True
    assert newer.release.is_mandatory is False
