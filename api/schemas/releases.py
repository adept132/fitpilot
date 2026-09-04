"""Strict public contracts for Android release discovery and delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Platform = Literal["android"]
ReleaseChannel = Literal["production-direct", "production-play"]
DeliveryMethod = Literal["direct_apk", "eas_update", "google_play"]
ReleaseStatus = Literal["published", "withdrawn"]
VersionCode = Annotated[int, Field(gt=0)]
ArtifactSize = Annotated[int, Field(gt=0)]
SourceCommit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
ArtifactSha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
VersionName = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")]


class ReleaseNotes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ru: str
    en: str

    @field_validator("ru", "en")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("release notes must be nonblank")
        return value


class LatestReleaseQuery(BaseModel):
    """The platform is fixed by the path, while channel and build are explicit."""

    model_config = ConfigDict(extra="forbid")

    platform: Platform = "android"
    channel: ReleaseChannel
    current_version_code: VersionCode
    runtime_version: str | None = Field(default=None, max_length=255)


class ReleaseRecord(BaseModel):
    """Validated registry representation used while producing public responses."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    platform: Platform
    channel: ReleaseChannel
    delivery_method: DeliveryMethod
    status: ReleaseStatus
    version_code: VersionCode
    version_name: VersionName
    runtime_version: str | None = None
    release_notes: ReleaseNotes
    is_mandatory: bool
    min_supported_version_code: int | None = None
    artifact_storage_key: str | None = None
    artifact_sha256: ArtifactSha256 | None = None
    artifact_size_bytes: ArtifactSize | None = None
    eas_update_group_id: str | None = None
    source_commit: SourceCommit
    published_at: datetime

    @model_validator(mode="after")
    def validate_delivery_payload(self) -> "ReleaseRecord":
        direct = self.delivery_method == "direct_apk"
        if direct and not (
            self.artifact_storage_key
            and self.artifact_sha256
            and self.artifact_size_bytes
        ):
            raise ValueError("direct APK release requires artifact metadata")
        if not direct and any(
            value is not None
            for value in (
                self.artifact_storage_key,
                self.artifact_sha256,
                self.artifact_size_bytes,
            )
        ):
            raise ValueError("non-APK release cannot contain artifact metadata")
        if self.delivery_method == "eas_update" and not (
            self.runtime_version and self.eas_update_group_id
        ):
            raise ValueError("EAS update requires runtime and update group")
        return self


class PublicRelease(BaseModel):
    """An instruction suitable for an unauthenticated mobile installation."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    delivery_method: DeliveryMethod
    version_code: VersionCode
    version_name: VersionName
    runtime_version: str | None = None
    release_notes: ReleaseNotes
    published_at: datetime
    download_url: str | None = None
    sha256: ArtifactSha256 | None = None
    size_bytes: ArtifactSize | None = None
    eas_update_group_id: str | None = None

    @model_validator(mode="after")
    def validate_public_delivery_payload(self) -> "PublicRelease":
        if self.delivery_method == "direct_apk":
            if not (self.download_url and self.sha256 and self.size_bytes):
                raise ValueError("direct APK instruction requires download metadata")
        elif any(value is not None for value in (self.download_url, self.sha256, self.size_bytes)):
            raise ValueError("non-APK instruction cannot contain download metadata")
        if self.delivery_method == "eas_update" and not self.eas_update_group_id:
            raise ValueError("EAS instruction requires update group")
        return self


class LatestReleaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_version_code: VersionCode
    update_available: bool
    mandatory: bool
    current_release_withdrawn: bool
    release: PublicRelease | None
