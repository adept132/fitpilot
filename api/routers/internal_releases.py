"""Authenticated publication endpoints for the Android update-center ledger."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.releases import ReleaseNotes, ReleaseRecord
from api.security.github_webhook import verify_github_signature
from api.security.release_publisher import require_release_operator, require_release_publisher
from api.services.models import AppRelease, AppReleaseLane
from api.services.release_registry import (
    IdempotencyConflictError,
    ReleaseConflictError,
    ReleaseNotFoundError,
    ReleaseRegistryError,
    StaleReleaseError,
    advance_github_mobile_push_targets,
    publish_direct_release,
    publish_eas_release,
    bind_expected_ci_run,
    set_mandatory,
    withdraw_release,
)
from api.services.release_storage import (
    ArtifactValidationError,
    ReleaseStorage,
    StagedArtifact,
    StoredArtifact,
)
from api.services.release_artifact_lock import hold_release_artifact_lock
from api.routers.releases import release_storage_root


webhook_router = APIRouter()
router = APIRouter(prefix="/internal/app-releases")

_GITHUB_REPOSITORY = "adept132/eurith-mobile"
_GITHUB_REF = "refs/heads/main"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class _ReleaseCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["android"] = "android"
    channel: Literal["production-direct", "production-play"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    ci_run_id: str = Field(min_length=1, max_length=128)
    version_code: int = Field(gt=0)
    version_name: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    runtime_version: str | None = Field(default=None, max_length=255)
    fingerprint: str | None = Field(default=None, max_length=128)
    release_notes: ReleaseNotes
    min_supported_version_code: int | None = Field(default=None, gt=0)
    eas_build_id: str | None = Field(default=None, max_length=128)
    # The header is injected after JSON body validation on the EAS endpoint.
    idempotency_key: str = Field(default="", max_length=128)


class EASReleaseCommand(_ReleaseCommand):
    runtime_version: str = Field(min_length=1, max_length=255)
    fingerprint: str = Field(min_length=1, max_length=128)
    eas_update_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    eas_update_group_id: str = Field(min_length=1, max_length=128)


class _WithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class _MandatoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandatory: bool


class _CiRunBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    ci_run_id: str = Field(min_length=1, max_length=128)


class _LedgerRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_method: Literal["direct_apk", "eas_update", "google_play"]
    version_code: int
    version_name: str
    fingerprint: str | None
    runtime_version: str | None
    eas_build_id: str | None
    eas_update_id: str | None
    eas_update_group_id: str | None


class _LaneLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["android"]
    channel: Literal["production-direct", "production-play"]
    expected_source_commit: str
    expected_ci_run_id: str | None
    latest_published: _LedgerRelease | None


class _IdempotencyRelease(BaseModel):
    """Secret-free immutable tuple consumed by release automation retries."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    platform: Literal["android"]
    channel: Literal["production-direct", "production-play"]
    delivery_method: Literal["direct_apk", "eas_update", "google_play"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    ci_run_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_KEY_PATTERN)
    fingerprint: str | None = Field(default=None, max_length=128)
    runtime_version: str | None = Field(default=None, max_length=255)
    version_code: int = Field(gt=0)
    version_name: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    eas_build_id: str | None = Field(default=None, min_length=1, max_length=128)
    eas_update_id: str | None = Field(default=None, min_length=1, max_length=128)
    eas_update_group_id: str | None = Field(default=None, min_length=1, max_length=128)
    status: Literal["published", "withdrawn"]
    is_mandatory: bool
    min_supported_version_code: int | None = Field(default=None, gt=0)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_size_bytes: int | None = Field(default=None, gt=0)
    release_notes: ReleaseNotes


class _IdempotencyLookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release: _IdempotencyRelease


class _DirectVersionMaximum(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_code: int = Field(gt=0)
    version_name: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )


class _DirectVersionCeiling(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["android"]
    channel: Literal["production-direct"]
    delivery_method: Literal["direct_apk"]
    maximum: _DirectVersionMaximum | None


def _record(release: AppRelease) -> dict:
    return ReleaseRecord.model_validate(release).model_dump(mode="json")


def _release_response(release: AppRelease, *, created: bool | None = None) -> dict:
    payload: dict = {"release": _record(release)}
    if created is not None:
        payload["created"] = created
    return payload


def _publisher_error(error: ReleaseRegistryError) -> HTTPException:
    if isinstance(error, ReleaseNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "release not found")
    if isinstance(error, (StaleReleaseError, ReleaseConflictError, IdempotencyConflictError)):
        return HTTPException(status.HTTP_409_CONFLICT, str(error))
    return HTTPException(status.HTTP_409_CONFLICT, "release publication rejected")


def _storage() -> ReleaseStorage:
    return ReleaseStorage(Path(release_storage_root()))


async def _lane(platform: str, channel: str, db: AsyncSession) -> AppReleaseLane | None:
    return (
        await db.execute(
            select(AppReleaseLane).where(
                and_(
                    AppReleaseLane.platform == platform,
                    AppReleaseLane.channel == channel,
                )
            )
        )
    ).scalar_one_or_none()


async def _release_by_idempotency(db: AsyncSession, key: str) -> AppRelease | None:
    return (
        await db.execute(select(AppRelease).where(AppRelease.idempotency_key == key))
    ).scalar_one_or_none()


async def _maximum_direct_release(db: AsyncSession) -> AppRelease | None:
    return (
        await db.execute(
            select(AppRelease)
            .where(
                and_(
                    AppRelease.platform == "android",
                    AppRelease.channel == "production-direct",
                    AppRelease.delivery_method == "direct_apk",
                )
            )
            .order_by(AppRelease.version_code.desc())
            .limit(1)
        )
    ).scalars().first()


@webhook_router.post("/webhooks/github/mobile-push")
async def github_mobile_push(
    request: Request,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
    x_github_delivery: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Advance both Android lanes only for an authentic main-branch GitHub push."""

    body = await request.body()
    # Signature validation deliberately precedes JSON parsing.
    verify_github_signature(body, x_hub_signature_256)
    if x_github_event != "push" or not x_github_delivery or len(x_github_delivery) > 128:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid GitHub delivery")
    try:
        payload = json.loads(body)
        repository = payload["repository"]["full_name"]
        ref = payload["ref"]
        before = payload["before"]
        source_commit = payload["after"]
        deleted = payload["deleted"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid GitHub push payload")
    if (
        repository != _GITHUB_REPOSITORY
        or ref != _GITHUB_REF
        or deleted is not False
        or not isinstance(before, str)
        or _FULL_SHA.fullmatch(before) is None
        or not isinstance(source_commit, str)
        or _FULL_SHA.fullmatch(source_commit) is None
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unexpected GitHub push")

    try:
        async with db.begin():
            duplicate = await advance_github_mobile_push_targets(
                db, before=before, source_commit=source_commit
            )
    except StaleReleaseError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return {"accepted": True, "duplicate": duplicate, "source_commit": source_commit}


@router.get(
    "/lanes/android/{channel}",
    response_model=_LaneLedger,
    dependencies=[Depends(require_release_publisher)],
)
async def get_lane_ledger(
    channel: Literal["production-direct", "production-play"],
    db: AsyncSession = Depends(get_db),
) -> _LaneLedger:
    lane = await _lane("android", channel, db)
    if lane is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "release lane not found")
    latest = (
        await db.execute(
            select(AppRelease)
            .where(
                and_(
                    AppRelease.platform == "android",
                    AppRelease.channel == channel,
                    AppRelease.status == "published",
                )
            )
            .order_by(AppRelease.version_code.desc(), AppRelease.published_at.desc())
        )
    ).scalars().first()
    latest_payload = (
        _LedgerRelease(
            delivery_method=latest.delivery_method,
            version_code=latest.version_code,
            version_name=latest.version_name,
            fingerprint=latest.fingerprint,
            runtime_version=latest.runtime_version,
            eas_build_id=latest.eas_build_id,
            eas_update_id=latest.eas_update_id,
            eas_update_group_id=latest.eas_update_group_id,
        )
        if latest is not None
        else None
    )
    return _LaneLedger(
        platform="android",
        channel=channel,
        expected_source_commit=lane.expected_source_commit,
        expected_ci_run_id=lane.expected_ci_run_id,
        latest_published=latest_payload,
    )


@router.get(
    "/by-idempotency",
    response_model=_IdempotencyLookupResponse,
    dependencies=[Depends(require_release_publisher)],
)
async def get_release_by_idempotency(
    key: Annotated[str, Query(min_length=1, max_length=128, pattern=_IDEMPOTENCY_KEY_PATTERN)],
    db: AsyncSession = Depends(get_db),
) -> _IdempotencyLookupResponse:
    release = await _release_by_idempotency(db, key)
    if release is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "release not found")
    return _IdempotencyLookupResponse(
        release=_IdempotencyRelease.model_validate(release)
    )


@router.get(
    "/android/direct-apk/version-ceiling",
    response_model=_DirectVersionCeiling,
    dependencies=[Depends(require_release_publisher)],
    include_in_schema=False,
)
async def get_direct_version_ceiling(
    db: AsyncSession = Depends(get_db),
) -> _DirectVersionCeiling:
    maximum = await _maximum_direct_release(db)
    return _DirectVersionCeiling(
        platform="android",
        channel="production-direct",
        delivery_method="direct_apk",
        maximum=(
            _DirectVersionMaximum(
                version_code=maximum.version_code,
                version_name=maximum.version_name,
            )
            if maximum is not None
            else None
        ),
    )


@router.post(
    "/lanes/android/{channel}/ci-run",
    dependencies=[Depends(require_release_publisher)],
)
async def bind_ci_run(
    channel: Literal["production-direct", "production-play"],
    request: _CiRunBindRequest,
    db: AsyncSession = Depends(get_db),
) -> _LaneLedger:
    try:
        lane = await bind_expected_ci_run(
            db, "android", channel, request.source_commit, request.ci_run_id
        )
    except ReleaseRegistryError as error:
        raise _publisher_error(error)
    return _LaneLedger(
        platform="android",
        channel=channel,
        expected_source_commit=lane.expected_source_commit,
        expected_ci_run_id=lane.expected_ci_run_id,
        latest_published=None,
    )


@router.post("/android/direct-apk", dependencies=[Depends(require_release_publisher)])
async def publish_direct_apk(
    channel: Annotated[Literal["production-direct", "production-play"], Form()],
    source_commit: Annotated[str, Form(pattern=r"^[0-9a-f]{40}$")],
    ci_run_id: Annotated[str, Form(min_length=1, max_length=128)],
    version_code: Annotated[int, Form(gt=0)],
    version_name: Annotated[str, Form(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")],
    release_notes_ru: Annotated[str, Form(min_length=1)],
    release_notes_en: Annotated[str, Form(min_length=1)],
    artifact: UploadFile = File(...),
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=_IDEMPOTENCY_KEY_PATTERN,
        ),
    ] = "",
    artifact_sha256: Annotated[str, Header(alias="X-Artifact-SHA256")] = "",
    runtime_version: Annotated[str | None, Form(max_length=255)] = None,
    fingerprint: Annotated[str | None, Form(max_length=128)] = None,
    min_supported_version_code: Annotated[int | None, Form(gt=0)] = None,
    eas_build_id: Annotated[str | None, Form(max_length=128)] = None,
    db: AsyncSession = Depends(get_db),
):
    command = _ReleaseCommand(
        channel=channel,
        source_commit=source_commit,
        ci_run_id=ci_run_id,
        version_code=version_code,
        version_name=version_name,
        runtime_version=runtime_version,
        fingerprint=fingerprint,
        release_notes={"ru": release_notes_ru, "en": release_notes_en},
        min_supported_version_code=min_supported_version_code,
        eas_build_id=eas_build_id,
        idempotency_key=idempotency_key,
    )
    if (
        command.min_supported_version_code is not None
        and command.min_supported_version_code > command.version_code
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid minimum version")
    command = command.model_copy(
        update={"release_notes": command.release_notes.model_dump()}
    )

    storage = _storage()
    staged: StagedArtifact | None = None
    try:
        try:
            staged = await storage.stage(artifact, artifact_sha256)
        except ArtifactValidationError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error))
        try:
            # This owns one physical PostgreSQL connection while the registry
            # transaction commits and the artifact is finalized.  Cleanup uses
            # the same session-level lock through its marker/unlink phases.
            if db.bind is None:
                raise RuntimeError("release database session is not engine-bound")
            async with hold_release_artifact_lock(db.bind) as connection:
                async with AsyncSession(bind=connection, expire_on_commit=False) as locked_db:
                    async with locked_db.begin():
                        stored = StoredArtifact(
                            storage_key=f"android/sha256/{staged.sha256}.apk",
                            sha256=staged.sha256,
                            size_bytes=staged.size_bytes,
                        )
                        result = await publish_direct_release(locked_db, command, stored)
                        if result.created:
                            storage.finalize(staged)
        except ArtifactValidationError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error))
        except ReleaseRegistryError as error:
            raise _publisher_error(error)
    finally:
        if staged is not None and staged.path.exists():
            try:
                storage.discard(staged)
            except ArtifactValidationError:
                # The release-storage implementation refuses an unsafe unlink.
                pass
    response = _release_response(result.release, created=result.created)
    return response if not result.created else JSONResponse(
        status_code=status.HTTP_201_CREATED, content=response
    )


@router.post("/android/eas-update", dependencies=[Depends(require_release_publisher)])
async def publish_eas_update(
    command: EASReleaseCommand,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=_IDEMPOTENCY_KEY_PATTERN,
        ),
    ] = "",
    db: AsyncSession = Depends(get_db),
):
    command = command.model_copy(
        update={
            "idempotency_key": idempotency_key,
            "release_notes": command.release_notes.model_dump(),
        }
    )
    if (
        command.min_supported_version_code is not None
        and command.min_supported_version_code > command.version_code
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid minimum version")
    try:
        async with db.begin():
            result = await publish_eas_release(db, command)
    except ReleaseRegistryError as error:
        raise _publisher_error(error)
    response = _release_response(result.release, created=result.created)
    return response if not result.created else JSONResponse(
        status_code=status.HTTP_201_CREATED, content=response
    )


@router.post("/{release_id}/withdraw", dependencies=[Depends(require_release_publisher)])
async def withdraw(
    release_id: UUID,
    request: _WithdrawalRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        release = await withdraw_release(db, release_id, request.reason)
    except ReleaseRegistryError as error:
        raise _publisher_error(error)
    return _release_response(release)


@router.patch("/{release_id}/mandatory", dependencies=[Depends(require_release_operator)])
async def mandatory(
    release_id: UUID,
    request: _MandatoryRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        release = await set_mandatory(db, release_id, request.mandatory)
    except ReleaseRegistryError as error:
        raise _publisher_error(error)
    return _release_response(release)
