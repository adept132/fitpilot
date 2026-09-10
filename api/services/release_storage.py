from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import ctypes
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
import sys
import uuid
from ctypes import wintypes

from fastapi import UploadFile


_SUPPORTS_DIRECTORY_FILE_DESCRIPTORS = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.link in os.supports_dir_fd
)


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


class _WindowsDirectoryGuard:
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ = 0x0001
    _FILE_SHARE_WRITE = 0x0002
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
    _FILE_ATTRIBUTE_TAG_INFO = 9
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._handle: int | None = None

    def __enter__(self) -> _WindowsDirectoryGuard:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(self.directory),
            self._FILE_READ_ATTRIBUTES,
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE,
            None,
            self._OPEN_EXISTING,
            self._FILE_FLAG_BACKUP_SEMANTICS | self._FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == self._INVALID_HANDLE_VALUE:
            raise ArtifactValidationError("storage directory guard could not open")

        get_information = kernel32.GetFileInformationByHandleEx
        get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        get_information.restype = wintypes.BOOL
        info = self._AttributeTagInfo()
        if not get_information(
            handle,
            self._FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            self._close_handle(kernel32, handle)
            raise ArtifactValidationError("storage directory guard could not inspect")
        if info.file_attributes & self._FILE_ATTRIBUTE_REPARSE_POINT:
            self._close_handle(kernel32, handle)
            raise ArtifactValidationError("storage directory is a reparse point")

        self._handle = handle
        return self

    def __exit__(self, *args: object) -> None:
        if self._handle is not None:
            self._close_handle(ctypes.WinDLL("kernel32", use_last_error=True), self._handle)
            self._handle = None

    @staticmethod
    def _close_handle(kernel32: object, handle: int) -> None:
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)


class ReleaseStorage:
    _CHUNK_BYTES = 1024 * 1024
    _APK_MAGIC = b"PK\x03\x04"
    _PUBLISHED_MODE = 0o640

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
        self._validate_staging_root()
        temp = self.staging_root / f"{uuid.uuid4()}.apk.part"
        digest = hashlib.sha256()
        size = 0

        with self._directory_guard(self.staging_root):
            try:
                with self._open_new_staged_file(temp) as output:
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
                    output.seek(0)
                    magic = output.read(4)
                    if magic != self._APK_MAGIC:
                        raise ArtifactValidationError("artifact is not an APK archive")
                    if not secrets.compare_digest(digest.hexdigest(), expected_sha256):
                        raise ArtifactValidationError("artifact sha256 mismatch")
                self._validate_staging_root()

                return StagedArtifact(temp, digest.hexdigest(), size)
            except BaseException:
                self._discard_temp_if_safe(temp)
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

        with self._directory_guard(self.staging_root), self._directory_guard(target.parent):
            try:
                self._link_staged_file(source, target)
            except FileExistsError:
                self._validate_existing_target(target)
                if not self._files_are_identical(source, target):
                    raise ArtifactValidationError("storage integrity collision")
                self._set_file_mode(target.parent, target.name, self._PUBLISHED_MODE)
                self.discard(staged)
                return StoredArtifact(storage_key, staged.sha256, staged.size_bytes)
            except OSError as error:
                if error.errno == errno.EXDEV:
                    raise ArtifactValidationError(
                        "staging and storage must be on the same filesystem"
                    ) from error
                raise
            else:
                # Publication itself is atomic.  Only after the complete inode
                # is reachable at its final name do we grant nginx group read.
                self._set_file_mode(target.parent, target.name, self._PUBLISHED_MODE)
                self._fsync_parent(target.parent)
                self.discard(staged)
                return StoredArtifact(storage_key, staged.sha256, staged.size_bytes)

    def discard(self, staged: StagedArtifact) -> None:
        path = self._validated_staged_path(staged)
        self._unlink_staged_file(path.name)

    def _validated_staged_path(self, staged: StagedArtifact) -> Path:
        path = Path(staged.path)
        if path.parent != self.staging_root:
            raise ArtifactValidationError("staged artifact is outside the staging directory")
        self._validate_staging_root()
        if path.is_symlink() or not path.is_file():
            raise ArtifactValidationError("staged artifact is not a regular file")
        self._ensure_under_root(path)
        return path

    def _create_safe_directory(self, directory: Path) -> None:
        relative = directory.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current /= part
            if current.exists() or current.is_symlink():
                if self._is_reparse_point(current) or not current.is_dir():
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
        if self._is_reparse_point(target) or not target.is_file():
            raise ArtifactValidationError("storage integrity collision")

    def _validate_staging_root(self) -> None:
        try:
            if self._is_reparse_point(self.staging_root) or not self.staging_root.is_dir():
                raise ArtifactValidationError("staging directory is unsafe")
            self._ensure_under_root(self.staging_root)
        except OSError as error:
            raise ArtifactValidationError("staging directory is unsafe") from error

    def _open_new_staged_file(self, temp: Path):
        if self._supports_directory_file_descriptors():
            directory_descriptor = self._open_staging_directory()
            try:
                descriptor = os.open(
                    temp.name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            finally:
                os.close(directory_descriptor)
            return os.fdopen(descriptor, "w+b")

        # Windows lacks dir_fd and O_NOFOLLOW in Python's os module. Revalidate
        # every use and reject every reparse point instead of following it.
        self._validate_staging_root()
        descriptor = os.open(temp, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            self._validate_staging_root()
        except BaseException:
            os.close(descriptor)
            raise
        return os.fdopen(descriptor, "w+b")

    def _open_staging_directory(self) -> int:
        self._validate_staging_root()
        try:
            return os.open(
                self.staging_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError as error:
            raise ArtifactValidationError("staging directory is unsafe") from error

    @staticmethod
    def _directory_guard(directory: Path):
        if sys.platform == "win32":
            return _WindowsDirectoryGuard(directory)
        return nullcontext()

    def _link_staged_file(self, source: Path, target: Path) -> None:
        if self._supports_directory_file_descriptors():
            source_directory = self._open_staging_directory()
            try:
                target_directory = self._open_directory(target.parent)
                try:
                    os.link(
                        source.name,
                        target.name,
                        src_dir_fd=source_directory,
                        dst_dir_fd=target_directory,
                        follow_symlinks=False,
                    )
                finally:
                    os.close(target_directory)
            finally:
                os.close(source_directory)
            return

        self._validate_staging_root()
        self._ensure_under_root(target.parent)
        os.link(source, target)

    def _open_directory(self, directory: Path) -> int:
        self._ensure_under_root(directory)
        if self._is_reparse_point(directory) or not directory.is_dir():
            raise ArtifactValidationError("storage path escapes configured root")
        try:
            return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise ArtifactValidationError("storage path escapes configured root") from error

    def _set_file_mode(self, directory: Path, filename: str, mode: int) -> None:
        """Change a regular file through an opened directory, never a symlink."""
        if os.name != "posix":
            return
        directory_descriptor = self._open_directory(directory)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ArtifactValidationError("release artifact is not a regular file")
            os.fchmod(descriptor, mode)
        except OSError as error:
            raise ArtifactValidationError("release artifact permissions could not be set") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory_descriptor)

    def _discard_temp_if_safe(self, temp: Path) -> None:
        try:
            if temp.parent == self.staging_root:
                self._unlink_staged_file(temp.name)
        except (ArtifactValidationError, FileNotFoundError):
            # A replaced staging directory may no longer safely name this file.
            # Leaving an unreachable partial file is safer than unlinking outside it.
            return

    def _unlink_staged_file(self, filename: str) -> None:
        if self._supports_directory_file_descriptors():
            directory_descriptor = self._open_staging_directory()
            try:
                os.unlink(filename, dir_fd=directory_descriptor)
            except FileNotFoundError:
                return
            finally:
                os.close(directory_descriptor)
            return

        self._validate_staging_root()
        path = self.staging_root / filename
        if self._is_reparse_point(path):
            raise ArtifactValidationError("staged artifact is not a regular file")
        path.unlink(missing_ok=True)

    @staticmethod
    def _supports_directory_file_descriptors() -> bool:
        return _SUPPORTS_DIRECTORY_FILE_DESCRIPTORS

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        metadata = os.lstat(path)
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_attribute)

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
