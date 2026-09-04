"""Authenticated publication endpoints for the Android update-center ledger."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.schemas.releases import ReleaseNotes, ReleaseRecord
from api.security.github_webhook import verify_github_signature
from api.security.release_publisher import require_release_publisher
from api.services.models import AppRelease, AppReleaseLane
from api.services.release_registry import (
    IdempotencyConflictError,
    ReleaseConflictError,
    ReleaseNotFoundError,
    ReleaseRegistryError,
    StaleReleaseError,
    publish_direct_release,
    publish_eas_release,
    set_expected_commit,
    set_mandatory,
    withdraw_release,
)
from api.services.release_storage import (
    ArtifactValidationError,
    ReleaseStorage,
    StagedArtifact,
    StoredArtifact,
)
from api.routers.releases import release_storage_root


webhook_router = APIRouter()
router = APIRouter(
    prefix="/internal/app-releases",
    dependencies=[Depends(require_release_publisher)],
)

_GITHUB_REPOSITORY = "adept132/eurith-mobile"
_GITHUB_REF = "refs/heads/main"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_CHANNELS = ("production-direct", "production-play")


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
    eas_update_group_id: str = Field(min_length=1, max_length=128)


class _WithdrawalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class _MandatoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandatory: bool


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


def _manual_operation(value: str | None) -> None:
    """Keep automatic release automation unable to turn on a mandatory gate."""

    if value != "true":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "mandatory changes require an explicit manual operation",
        )


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
        source_commit = payload["after"]
        deleted = payload["deleted"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid GitHub push payload")
    if (
        repository != _GITHUB_REPOSITORY
        or ref != _GITHUB_REF
        or deleted is not False
        or not isinstance(source_commit, str)
        or _FULL_SHA.fullmatch(source_commit) is None
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unexpected GitHub push")

    async with db.begin():
        existing = [await _lane("android", channel, db) for channel in _CHANNELS]
        duplicate = all(
            lane is not None
            and lane.expected_source_commit == source_commit
            and lane.expected_ci_run_id == x_github_delivery
            for lane in existing
        )
        if not duplicate:
            for channel in _CHANNELS:
                await set_expected_commit(
                    db, "android", channel, source_commit, x_github_delivery
                )
    return {"accepted": True, "duplicate": duplicate, "source_commit": source_commit}


@router.get("/lanes/android/{channel}")
async def get_lane_ledger(
    channel: Literal["production-direct", "production-play"],
    db: AsyncSession = Depends(get_db),
) -> dict:
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
    return {
        "platform": "android",
        "channel": channel,
        "expected_source_commit": lane.expected_source_commit,
        "expected_ci_run_id": lane.expected_ci_run_id,
        "latest_published": _record(latest) if latest is not None else None,
    }


@router.post("/android/direct-apk")
async def publish_direct_apk(
    channel: Annotated[Literal["production-direct", "production-play"], Form()],
    source_commit: Annotated[str, Form(pattern=r"^[0-9a-f]{40}$")],
    ci_run_id: Annotated[str, Form(min_length=1, max_length=128)],
    version_code: Annotated[int, Form(gt=0)],
    version_name: Annotated[str, Form(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")],
    release_notes_ru: Annotated[str, Form(min_length=1)],
    release_notes_en: Annotated[str, Form(min_length=1)],
    artifact: UploadFile = File(...),
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)] = "",
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
            async with db.begin():
                stored = StoredArtifact(
                    storage_key=f"android/sha256/{staged.sha256}.apk",
                    sha256=staged.sha256,
                    size_bytes=staged.size_bytes,
                )
                result = await publish_direct_release(db, command, stored)
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


@router.post("/android/eas-update")
async def publish_eas_update(
    command: EASReleaseCommand,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)] = "",
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


@router.post("/{release_id}/withdraw")
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


@router.patch("/{release_id}/mandatory")
async def mandatory(
    release_id: UUID,
    request: _MandatoryRequest,
    x_release_manual_operation: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    _manual_operation(x_release_manual_operation)
    try:
        release = await set_mandatory(db, release_id, request.mandatory)
    except ReleaseRegistryError as error:
        raise _publisher_error(error)
    return _release_response(release)
