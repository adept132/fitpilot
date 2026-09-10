"""Unauthenticated Android release discovery and proxy-owned APK delivery."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path, PurePosixPath
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_db
from api.errors import LocalizedHTTPException
from api.schemas.releases import (
    LatestReleaseQuery,
    LatestReleaseResponse,
    PublicRelease,
    ReleaseRecord,
)
from api.services.models import AppRelease
from api.services.release_registry import latest_instruction


router = APIRouter(tags=["app-releases"])
_VERSION_NAME = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_STORAGE_KEY = re.compile(r"^android/sha256/([0-9a-f]{64})\.apk$")


def release_storage_root() -> Path:
    """Return a resolved, server-side-only artifact root."""

    return Path(os.getenv("RELEASE_STORAGE_ROOT", "/var/lib/eurith/releases")).resolve(
        strict=False
    )


async def _release_by_id(db: AsyncSession, release_id: UUID) -> AppRelease | None:
    return (
        await db.execute(select(AppRelease).where(AppRelease.id == release_id))
    ).scalar_one_or_none()


def _public_release(release: AppRelease, request: Request) -> PublicRelease:
    record = ReleaseRecord.model_validate(release)
    direct = record.delivery_method == "direct_apk"
    return PublicRelease(
        id=record.id,
        source_commit=record.source_commit,
        delivery_method=record.delivery_method,
        version_code=record.version_code,
        version_name=record.version_name,
        min_supported_version_code=record.min_supported_version_code,
        runtime_version=record.runtime_version,
        release_notes=record.release_notes,
        published_at=record.published_at,
        download_url=(
            str(request.url_for("download_release", release_id=str(record.id)))
            if direct
            else None
        ),
        sha256=record.artifact_sha256 if direct else None,
        size_bytes=record.artifact_size_bytes if direct else None,
        eas_update_group_id=record.eas_update_group_id,
    )


@router.get(
    "/app-releases/android/latest",
    response_model=LatestReleaseResponse,
)
async def latest_release(
    request: Request,
    response: Response,
    query: Annotated[LatestReleaseQuery, Depends()],
    db: AsyncSession = Depends(get_db),
) -> LatestReleaseResponse:
    """Return a no-cache release instruction without Firebase authentication."""

    result = await latest_instruction(db, query)
    response.headers["Cache-Control"] = "no-store"
    return LatestReleaseResponse(
        current_version_code=result.current_version_code,
        update_available=result.update_available,
        mandatory=result.mandatory,
        current_release_withdrawn=result.current_release_withdrawn,
        release=_public_release(result.release, request) if result.release else None,
    )


def _artifact_path(
    root: Path, storage_key: str | None, expected_sha256: str | None
) -> Path | None:
    """Resolve only storage-generated POSIX keys and never follow an escape."""

    if not isinstance(storage_key, str) or not isinstance(expected_sha256, str):
        return None
    key_match = _STORAGE_KEY.fullmatch(storage_key)
    if key_match is None or key_match.group(1) != expected_sha256:
        return None
    key = PurePosixPath(storage_key)
    if key.is_absolute() or not key.parts or any(part in {"", ".", ".."} for part in key.parts):
        return None
    try:
        root = root.resolve(strict=True)
        path = root.joinpath(*key.parts)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if path.is_symlink() or not resolved.is_file():
        return None
    return resolved


def _download_filename(version_name: object) -> str:
    if isinstance(version_name, str) and _VERSION_NAME.fullmatch(version_name):
        return f"eurith-{version_name}.apk"
    return "eurith-update.apk"


def _download_headers(release: AppRelease) -> tuple[dict[str, str], int] | None:
    sha256 = getattr(release, "artifact_sha256", None)
    size = getattr(release, "artifact_size_bytes", None)
    if (
        not isinstance(sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
    ):
        return None
    return (
        {
            "Content-Type": "application/vnd.android.package-archive",
            "Content-Disposition": f'attachment; filename="{_download_filename(release.version_name)}"',
            "ETag": f'"sha256:{sha256}"',
            "Digest": f"sha-256={base64.b64encode(bytes.fromhex(sha256)).decode('ascii')}",
        },
        size,
    )


async def _download_release_handoff(
    release_id: UUID, db: AsyncSession
) -> StreamingResponse:
    """Hand a validated internal location to the artifact-serving proxy."""

    release = await _release_by_id(db, release_id)
    if release is None or release.delivery_method != "direct_apk":
        raise LocalizedHTTPException(status.HTTP_404_NOT_FOUND, "release.not_found")
    if release.status == "withdrawn":
        raise LocalizedHTTPException(status.HTTP_410_GONE, "release.withdrawn")
    if release.status != "published":
        raise LocalizedHTTPException(status.HTTP_404_NOT_FOUND, "release.not_found")

    handoff = _download_headers(release)
    path = _artifact_path(
        release_storage_root(), release.artifact_storage_key, release.artifact_sha256
    )
    if handoff is None or path is None:
        raise LocalizedHTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "release.artifact_unavailable"
        )
    try:
        headers, expected_size = handoff
        if path.stat().st_size != expected_size:
            raise OSError("stored artifact size does not match release metadata")
    except (OSError, ValueError):
        raise LocalizedHTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "release.artifact_unavailable"
        )
    storage_key = release.artifact_storage_key
    assert isinstance(storage_key, str)  # path validation above guarantees this.
    headers["X-Accel-Redirect"] = f"/_release_files/{storage_key}"
    return StreamingResponse(iter(()), status_code=status.HTTP_200_OK, headers=headers)


@router.get(
    "/app-releases/{release_id}/download",
    name="download_release",
    operation_id="download_release",
)
async def download_release(
    release_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    return await _download_release_handoff(release_id, db)


@router.head(
    "/app-releases/{release_id}/download",
    name="head_download_release",
    include_in_schema=False,
)
async def head_download_release(
    release_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    return await _download_release_handoff(release_id, db)
