"""Transactional release-lane publication and update-selection policy."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy import and_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.models import AppRelease, AppReleaseLane


class ReleaseRegistryError(RuntimeError):
    """Base error for a rejected registry policy operation."""


class StaleReleaseError(ReleaseRegistryError):
    """The lane target moved on after this release candidate was built."""


class ReleaseConflictError(ReleaseRegistryError):
    """The requested change conflicts with an existing release state."""


class IdempotencyConflictError(ReleaseConflictError):
    """An idempotency key was reused for a different release."""


class VersionRegressionError(ReleaseConflictError):
    """A direct APK does not advance the lane's native version code."""


class VersionConflictError(ReleaseConflictError):
    """A direct APK tries to reuse a version code with another artifact."""


class ReleaseNotFoundError(ReleaseRegistryError):
    """A requested release id does not exist."""


@dataclass(frozen=True)
class PublishResult:
    release: AppRelease
    created: bool


@dataclass(frozen=True)
class LatestResult:
    current_version_code: int
    update_available: bool
    mandatory: bool
    current_release_withdrawn: bool
    release: AppRelease | None


def advisory_key(platform: str, channel: str) -> int:
    """Return PostgreSQL's signed bigint advisory key for a release lane."""

    digest = sha256(f"{platform}:{channel}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@asynccontextmanager
async def _transaction(session: AsyncSession) -> AsyncIterator[None]:
    """Join a caller-owned transaction or commit one registry operation atomically."""

    if session.in_transaction():
        yield
        return
    async with session.begin():
        yield


async def _lock_lane(session: AsyncSession, platform: str, channel: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": advisory_key(platform, channel)},
    )


async def _lane(
    session: AsyncSession, platform: str, channel: str
) -> AppReleaseLane | None:
    return (
        await session.execute(
            select(AppReleaseLane).where(
                and_(
                    AppReleaseLane.platform == platform,
                    AppReleaseLane.channel == channel,
                )
            )
        )
    ).scalar_one_or_none()


async def _release_by_idempotency(
    session: AsyncSession, idempotency_key: str
) -> AppRelease | None:
    return (
        await session.execute(
            select(AppRelease).where(AppRelease.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()


async def _release_by_id(session: AsyncSession, release_id: UUID) -> AppRelease | None:
    return (
        await session.execute(select(AppRelease).where(AppRelease.id == release_id))
    ).scalar_one_or_none()


async def _direct_releases(
    session: AsyncSession, platform: str, channel: str
) -> list[AppRelease]:
    return (
        await session.execute(
            select(AppRelease).where(
                and_(
                    AppRelease.platform == platform,
                    AppRelease.channel == channel,
                    AppRelease.delivery_method == "direct_apk",
                )
            )
        )
    ).scalars().all()


def _command_value(command: Any, name: str, default: Any = None) -> Any:
    if isinstance(command, dict):
        return command.get(name, default)
    return getattr(command, name, default)


def _stored_value(stored: Any, name: str) -> Any:
    if isinstance(stored, dict):
        return stored[name]
    return getattr(stored, name)


def _matches_existing_direct(release: AppRelease, command: Any, stored: Any) -> bool:
    return (
        release.platform == _command_value(command, "platform")
        and release.channel == _command_value(command, "channel")
        and release.delivery_method == "direct_apk"
        and release.version_code == _command_value(command, "version_code")
        and release.version_name == _command_value(command, "version_name")
        and release.runtime_version == _command_value(command, "runtime_version")
        and release.fingerprint == _command_value(command, "fingerprint")
        and release.release_notes == _command_value(command, "release_notes")
        and release.min_supported_version_code
        == _command_value(command, "min_supported_version_code")
        and release.source_commit == _command_value(command, "source_commit")
        and release.ci_run_id == _command_value(command, "ci_run_id")
        and release.eas_build_id == _command_value(command, "eas_build_id")
        and release.eas_update_group_id is None
        and release.artifact_storage_key == _stored_value(stored, "storage_key")
        and release.artifact_sha256 == _stored_value(stored, "sha256")
        and release.artifact_size_bytes == _stored_value(stored, "size_bytes")
    )


def _matches_existing_eas(release: AppRelease, command: Any) -> bool:
    return (
        release.platform == _command_value(command, "platform")
        and release.channel == _command_value(command, "channel")
        and release.delivery_method == "eas_update"
        and release.version_code == _command_value(command, "version_code")
        and release.version_name == _command_value(command, "version_name")
        and release.runtime_version == _command_value(command, "runtime_version")
        and release.fingerprint == _command_value(command, "fingerprint")
        and release.release_notes == _command_value(command, "release_notes")
        and release.min_supported_version_code
        == _command_value(command, "min_supported_version_code")
        and release.source_commit == _command_value(command, "source_commit")
        and release.ci_run_id == _command_value(command, "ci_run_id")
        and release.eas_build_id == _command_value(command, "eas_build_id")
        and release.eas_update_group_id == _command_value(command, "eas_update_group_id")
    )


async def _persist_new_release(
    session: AsyncSession,
    release: AppRelease,
    command: Any,
    matches_existing: Any,
    *,
    eas_update_group_id: str | None = None,
) -> PublishResult:
    """Flush under a savepoint and turn cross-lane unique races into policy results."""

    try:
        async with session.begin_nested():
            session.add(release)
            await session.flush()
    except IntegrityError as error:
        existing = await _release_by_idempotency(
            session, _command_value(command, "idempotency_key")
        )
        if existing is not None:
            if matches_existing(existing, command):
                return PublishResult(existing, created=False)
            raise IdempotencyConflictError(
                "idempotency key belongs to a different release"
            ) from error
        if eas_update_group_id is not None:
            matching_group = (
                await session.execute(
                    select(AppRelease).where(
                        AppRelease.eas_update_group_id == eas_update_group_id
                    )
                )
            ).scalar_one_or_none()
            if matching_group is not None:
                raise ReleaseConflictError("EAS update group is already published") from error
        raise ReleaseConflictError("release conflicts with an existing registry record") from error
    return PublishResult(release, created=True)


async def set_expected_commit(
    session: AsyncSession,
    platform: str,
    channel: str,
    source_commit: str,
    ci_run_id: str | None,
) -> AppReleaseLane:
    """Set the one source commit allowed to publish for a lane."""

    async with _transaction(session):
        await _lock_lane(session, platform, channel)
        lane = await _lane(session, platform, channel)
        if lane is None:
            lane = AppReleaseLane(
                platform=platform,
                channel=channel,
                expected_source_commit=source_commit,
                expected_ci_run_id=ci_run_id,
            )
            session.add(lane)
        else:
            lane.expected_source_commit = source_commit
            lane.expected_ci_run_id = ci_run_id
        await session.flush()
        return lane


async def bind_expected_ci_run(
    session: AsyncSession,
    platform: str,
    channel: str,
    source_commit: str,
    ci_run_id: str,
) -> AppReleaseLane:
    """Bind one concrete CI run only to the currently webhooked target SHA."""

    async with _transaction(session):
        await _lock_lane(session, platform, channel)
        lane = await _lane(session, platform, channel)
        if lane is None or lane.expected_source_commit != source_commit:
            raise StaleReleaseError("release source commit is no longer expected for this lane")
        if lane.expected_ci_run_id is None:
            lane.expected_ci_run_id = ci_run_id
            await session.flush()
        elif lane.expected_ci_run_id != ci_run_id:
            raise ReleaseConflictError("a different CI run is already bound to this lane")
        return lane


async def publish_direct_release(
    session: AsyncSession, command: Any, stored: Any
) -> PublishResult:
    """Publish a new direct APK after serializing all lane policy checks."""

    platform = _command_value(command, "platform")
    channel = _command_value(command, "channel")
    source_commit = _command_value(command, "source_commit")
    ci_run_id = _command_value(command, "ci_run_id")
    idempotency_key = _command_value(command, "idempotency_key")

    async with _transaction(session):
        await _lock_lane(session, platform, channel)
        lane = await _lane(session, platform, channel)
        if (
            lane is None
            or lane.expected_source_commit != source_commit
            or lane.expected_ci_run_id != ci_run_id
        ):
            raise StaleReleaseError("release source commit is no longer expected for this lane")

        existing = await _release_by_idempotency(session, idempotency_key)
        if existing is not None:
            if _matches_existing_direct(existing, command, stored):
                return PublishResult(existing, created=False)
            raise IdempotencyConflictError("idempotency key belongs to a different release")

        direct_releases = await _direct_releases(session, platform, channel)
        requested_version = _command_value(command, "version_code")
        for release in direct_releases:
            if release.version_code == requested_version:
                if release.artifact_sha256 != _stored_value(stored, "sha256"):
                    raise VersionConflictError(
                        "direct version code is already bound to a different artifact"
                    )
                raise VersionRegressionError("direct version code is already published")
        if direct_releases and requested_version <= max(
            release.version_code for release in direct_releases
        ):
            raise VersionRegressionError("direct version code must advance the lane")

        release = AppRelease(
            platform=platform,
            channel=channel,
            delivery_method="direct_apk",
            version_code=requested_version,
            version_name=_command_value(command, "version_name"),
            runtime_version=_command_value(command, "runtime_version"),
            fingerprint=_command_value(command, "fingerprint"),
            release_notes=_command_value(command, "release_notes"),
            status="published",
            is_mandatory=False,
            min_supported_version_code=_command_value(command, "min_supported_version_code"),
            artifact_storage_key=_stored_value(stored, "storage_key"),
            artifact_sha256=_stored_value(stored, "sha256"),
            artifact_size_bytes=_stored_value(stored, "size_bytes"),
            source_commit=source_commit,
            ci_run_id=ci_run_id,
            idempotency_key=idempotency_key,
            eas_build_id=_command_value(command, "eas_build_id"),
            eas_update_group_id=None,
        )
        return await _persist_new_release(
            session,
            release,
            command,
            lambda existing, candidate: _matches_existing_direct(existing, candidate, stored),
        )


async def publish_eas_release(session: AsyncSession, command: Any) -> PublishResult:
    """Publish EAS metadata; OTA records remain scoped to their native runtime."""

    platform = _command_value(command, "platform")
    channel = _command_value(command, "channel")
    source_commit = _command_value(command, "source_commit")
    ci_run_id = _command_value(command, "ci_run_id")
    idempotency_key = _command_value(command, "idempotency_key")
    runtime_version = _command_value(command, "runtime_version")
    update_group = _command_value(command, "eas_update_group_id")
    if not runtime_version or not update_group:
        raise ReleaseConflictError("EAS releases require runtime_version and eas_update_group_id")

    async with _transaction(session):
        await _lock_lane(session, platform, channel)
        lane = await _lane(session, platform, channel)
        if (
            lane is None
            or lane.expected_source_commit != source_commit
            or lane.expected_ci_run_id != ci_run_id
        ):
            raise StaleReleaseError("release source commit is no longer expected for this lane")

        existing = await _release_by_idempotency(session, idempotency_key)
        if existing is not None:
            if _matches_existing_eas(existing, command):
                return PublishResult(existing, created=False)
            raise IdempotencyConflictError("idempotency key belongs to a different release")

        matching_group = (
            await session.execute(
                select(AppRelease).where(AppRelease.eas_update_group_id == update_group)
            )
        ).scalar_one_or_none()
        if matching_group is not None:
            raise ReleaseConflictError("EAS update group is already published")

        release = AppRelease(
            platform=platform,
            channel=channel,
            delivery_method="eas_update",
            version_code=_command_value(command, "version_code"),
            version_name=_command_value(command, "version_name"),
            runtime_version=runtime_version,
            fingerprint=_command_value(command, "fingerprint"),
            release_notes=_command_value(command, "release_notes"),
            status="published",
            is_mandatory=False,
            min_supported_version_code=_command_value(command, "min_supported_version_code"),
            artifact_storage_key=None,
            artifact_sha256=None,
            artifact_size_bytes=None,
            source_commit=source_commit,
            ci_run_id=ci_run_id,
            idempotency_key=idempotency_key,
            eas_build_id=_command_value(command, "eas_build_id"),
            eas_update_group_id=update_group,
        )
        return await _persist_new_release(
            session,
            release,
            command,
            _matches_existing_eas,
            eas_update_group_id=update_group,
        )


async def _published_direct(
    session: AsyncSession, platform: str, channel: str
) -> AppRelease | None:
    return (
        await session.execute(
            select(AppRelease)
            .where(
                and_(
                    AppRelease.platform == platform,
                    AppRelease.channel == channel,
                    AppRelease.delivery_method == "direct_apk",
                    AppRelease.status == "published",
                )
            )
            .order_by(AppRelease.version_code.desc(), AppRelease.published_at.desc())
        )
    ).scalars().first()


async def _installed_direct_is_withdrawn(
    session: AsyncSession, platform: str, channel: str, version_code: int
) -> bool:
    return (
        await session.execute(
            select(AppRelease).where(
                and_(
                    AppRelease.platform == platform,
                    AppRelease.channel == channel,
                    AppRelease.delivery_method == "direct_apk",
                    AppRelease.version_code == version_code,
                    AppRelease.status == "withdrawn",
                )
            )
        )
    ).scalars().first() is not None


async def _compatible_ota(
    session: AsyncSession,
    platform: str,
    channel: str,
    version_code: int,
    runtime_version: str | None,
) -> AppRelease | None:
    if runtime_version is None:
        return None
    return (
        await session.execute(
            select(AppRelease)
            .where(
                and_(
                    AppRelease.platform == platform,
                    AppRelease.channel == channel,
                    AppRelease.delivery_method == "eas_update",
                    AppRelease.version_code == version_code,
                    AppRelease.runtime_version == runtime_version,
                    AppRelease.status == "published",
                )
            )
            .order_by(AppRelease.published_at.desc())
        )
    ).scalars().first()


async def latest_instruction(session: AsyncSession, query: Any) -> LatestResult:
    """Choose a direct binary first, then an exactly compatible EAS update."""

    platform = _command_value(query, "platform")
    channel = _command_value(query, "channel")
    current_version_code = _command_value(query, "current_version_code")
    runtime_version = _command_value(query, "runtime_version")

    direct = await _published_direct(session, platform, channel)
    current_is_withdrawn = await _installed_direct_is_withdrawn(
        session, platform, channel, current_version_code
    )
    if direct is not None and direct.version_code > current_version_code:
        mandatory = bool(direct.is_mandatory) or current_is_withdrawn
        return LatestResult(
            current_version_code=current_version_code,
            update_available=True,
            mandatory=mandatory,
            current_release_withdrawn=current_is_withdrawn,
            release=direct,
        )

    ota = await _compatible_ota(
        session, platform, channel, current_version_code, runtime_version
    )
    if ota is not None:
        return LatestResult(
            current_version_code=current_version_code,
            update_available=True,
            mandatory=bool(ota.is_mandatory),
            current_release_withdrawn=current_is_withdrawn,
            release=ota,
        )
    return LatestResult(
        current_version_code=current_version_code,
        update_available=False,
        mandatory=False,
        current_release_withdrawn=current_is_withdrawn,
        release=None,
    )


async def _latest_published_release(
    session: AsyncSession, platform: str, channel: str
) -> AppRelease | None:
    return (
        await session.execute(
            select(AppRelease)
            .where(
                and_(
                    AppRelease.platform == platform,
                    AppRelease.channel == channel,
                    AppRelease.status == "published",
                )
            )
            .order_by(AppRelease.version_code.desc(), AppRelease.published_at.desc())
        )
    ).scalars().first()


async def withdraw_release(
    session: AsyncSession, release_id: UUID, reason: str
) -> AppRelease:
    """Withdraw a release once; repeated withdrawal preserves its original audit data."""

    if not reason or not reason.strip():
        raise ReleaseConflictError("withdrawal reason is required")
    async with _transaction(session):
        release = await _release_by_id(session, release_id)
        if release is None:
            raise ReleaseNotFoundError("release was not found")
        await _lock_lane(session, release.platform, release.channel)
        release = await _release_by_id(session, release_id)
        if release is None:
            raise ReleaseNotFoundError("release was not found")
        if release.status == "withdrawn":
            return release
        release.status = "withdrawn"
        release.withdrawn_at = datetime.now(UTC)
        release.withdrawal_reason = reason.strip()
        await session.flush()
        return release


async def set_mandatory(
    session: AsyncSession, release_id: UUID, mandatory: bool
) -> AppRelease:
    """Change mandatory only for the lane's current published release."""

    async with _transaction(session):
        release = await _release_by_id(session, release_id)
        if release is None:
            raise ReleaseNotFoundError("release was not found")
        await _lock_lane(session, release.platform, release.channel)
        release = await _release_by_id(session, release_id)
        if release is None:
            raise ReleaseNotFoundError("release was not found")
        latest = await _latest_published_release(session, release.platform, release.channel)
        if latest is None or latest.id != release.id:
            raise ReleaseConflictError(
                "mandatory can be changed only for the latest published lane release"
            )
        if release.is_mandatory != mandatory:
            release.is_mandatory = mandatory
            release.mandatory_changed_at = datetime.now(UTC)
            await session.flush()
        return release
