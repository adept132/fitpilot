from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
import sys
import uuid

from fastapi import UploadFile


class ArtifactValidationError(ValueError):
    """Raised when an uploaded artifact cannot be safely stored."""


@dataclass(frozen=True)
class StagedArtifact:
    path: Path
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class StoredArtifact:
    storage_key: str
    sha256: str
    size_bytes: int


class ReleaseStorage:
    _CHUNK_BYTES = 1024 * 1024
    _APK_MAGIC = b"PK\x03\x04"

    def __init__(self, root: Path, max_bytes: int = 250 * 1024 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

        self.root = Path(root).resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ArtifactValidationError("storage root is not a directory")
        self.root = self.root.resolve()
        self.max_bytes = max_bytes
        self.staging_root = self.root / ".staging"
        self._create_safe_directory(self.staging_root)

    async def stage(
        self, upload: UploadFile, expected_sha256: str
    ) -> StagedArtifact:
        self._validate_sha(expected_sha256)
        temp = self.staging_root / f"{uuid.uuid4()}.apk.part"
        digest = hashlib.sha256()
        size = 0

        try:
            with temp.open("xb") as output:
                while chunk := await upload.read(self._CHUNK_BYTES):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ArtifactValidationError(
                            f"artifact exceeds {self.max_bytes} bytes"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())

            if size == 0:
                raise ArtifactValidationError("artifact is empty")
            with temp.open("rb") as staged_file:
                magic = staged_file.read(4)
            if magic != self._APK_MAGIC:
                raise ArtifactValidationError("artifact is not an APK archive")
            if not secrets.compare_digest(digest.hexdigest(), expected_sha256):
                raise ArtifactValidationError("artifact sha256 mismatch")

            return StagedArtifact(temp, digest.hexdigest(), size)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def finalize(self, staged: StagedArtifact) -> StoredArtifact:
        source = self._validated_staged_path(staged)
        self._validate_sha(staged.sha256)
        if staged.size_bytes <= 0:
            raise ArtifactValidationError("staged artifact size must be positive")
        self._verify_staged_contents(source, staged)

        storage_key = f"android/sha256/{staged.sha256}.apk"
        target = self.root / storage_key
        self._create_safe_directory(target.parent)

        try:
            reservation = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            self._validate_existing_target(target)
            if not self._files_are_identical(source, target):
                raise ArtifactValidationError("storage integrity collision")
            self.discard(staged)
            return StoredArtifact(storage_key, staged.sha256, staged.size_bytes)

        os.close(reservation)
        try:
            os.replace(source, target)
            self._fsync_parent(target.parent)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

        return StoredArtifact(storage_key, staged.sha256, staged.size_bytes)

    def discard(self, staged: StagedArtifact) -> None:
        self._validated_staged_path(staged).unlink(missing_ok=True)

    def _validated_staged_path(self, staged: StagedArtifact) -> Path:
        path = Path(staged.path)
        if path.parent != self.staging_root:
            raise ArtifactValidationError("staged artifact is outside the staging directory")
        if path.is_symlink() or not path.is_file():
            raise ArtifactValidationError("staged artifact is not a regular file")
        self._ensure_under_root(path)
        return path

    def _create_safe_directory(self, directory: Path) -> None:
        relative = directory.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current /= part
            if current.exists():
                if current.is_symlink() or not current.is_dir():
                    raise ArtifactValidationError("storage path escapes configured root")
            else:
                current.mkdir()
            self._ensure_under_root(current)

    def _ensure_under_root(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise ArtifactValidationError("storage path escapes configured root") from error
        return resolved

    def _validate_existing_target(self, target: Path) -> None:
        self._ensure_under_root(target)
        if target.is_symlink() or not target.is_file():
            raise ArtifactValidationError("storage integrity collision")

    def _verify_staged_contents(self, path: Path, staged: StagedArtifact) -> None:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as staged_file:
            magic = staged_file.read(4)
            digest.update(magic)
            size += len(magic)
            while chunk := staged_file.read(self._CHUNK_BYTES):
                size += len(chunk)
                if size > self.max_bytes:
                    raise ArtifactValidationError(
                        f"artifact exceeds {self.max_bytes} bytes"
                    )
                digest.update(chunk)

        if magic != self._APK_MAGIC:
            raise ArtifactValidationError("artifact is not an APK archive")
        if not secrets.compare_digest(digest.hexdigest(), staged.sha256):
            raise ArtifactValidationError("staged artifact sha256 mismatch")
        if size != staged.size_bytes:
            raise ArtifactValidationError("staged artifact size mismatch")

    def _files_are_identical(self, first: Path, second: Path) -> bool:
        with first.open("rb") as first_file, second.open("rb") as second_file:
            while True:
                first_chunk = first_file.read(self._CHUNK_BYTES)
                second_chunk = second_file.read(self._CHUNK_BYTES)
                if first_chunk != second_chunk:
                    return False
                if not first_chunk:
                    return True

    @staticmethod
    def _validate_sha(expected_sha256: str) -> None:
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise ArtifactValidationError("sha256 must be lowercase 64-character hex")

    @staticmethod
    def _fsync_parent(parent: Path) -> None:
        if not sys.platform.startswith("linux"):
            return
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
