from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import hashlib
from pathlib import Path
from threading import Event

import pytest
from fastapi import UploadFile

from api.services import release_storage
from api.services.release_storage import (
    ArtifactValidationError,
    ReleaseStorage,
    StagedArtifact,
)


def upload_file(payload: bytes) -> UploadFile:
    return UploadFile(file=BytesIO(payload), filename="release.apk")


def apk(payload: bytes = b"signed-apk-fixture") -> bytes:
    return b"PK\x03\x04" + payload


@pytest.mark.asyncio
async def test_stage_hashes_and_atomically_finalizes_apk(tmp_path: Path) -> None:
    payload = apk()
    expected = hashlib.sha256(payload).hexdigest()
    storage = ReleaseStorage(tmp_path, max_bytes=1024)

    staged = await storage.stage(upload_file(payload), expected)
    stored = storage.finalize(staged)

    assert stored.sha256 == expected
    assert stored.size_bytes == len(payload)
    assert stored.storage_key == f"android/sha256/{expected}.apk"
    assert (tmp_path / stored.storage_key).read_bytes() == payload
    assert not staged.path.exists()


@pytest.mark.asyncio
async def test_stage_rejects_hash_mismatch_and_removes_temp(tmp_path: Path) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)

    with pytest.raises(ArtifactValidationError, match="sha256"):
        await storage.stage(upload_file(apk(b"bad")), "0" * 64)

    assert list((tmp_path / ".staging").glob("*")) == []


@pytest.mark.asyncio
async def test_stage_rejects_an_oversize_upload_and_removes_temp(tmp_path: Path) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=8)
    payload = apk(b"too-large")

    with pytest.raises(ArtifactValidationError, match="exceeds"):
        await storage.stage(upload_file(payload), hashlib.sha256(payload).hexdigest())

    assert list((tmp_path / ".staging").glob("*")) == []


@pytest.mark.asyncio
async def test_stage_rejects_empty_upload_and_removes_temp(tmp_path: Path) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)

    with pytest.raises(ArtifactValidationError, match="empty"):
        await storage.stage(upload_file(b""), hashlib.sha256(b"").hexdigest())

    assert list((tmp_path / ".staging").glob("*")) == []


@pytest.mark.asyncio
async def test_stage_rejects_non_zip_magic_and_removes_temp(tmp_path: Path) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = b"not-an-apk"

    with pytest.raises(ArtifactValidationError, match="APK archive"):
        await storage.stage(upload_file(payload), hashlib.sha256(payload).hexdigest())

    assert list((tmp_path / ".staging").glob("*")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected_sha256",
    ["A" * 64, "a" * 63, "g" * 64, "a" * 63 + "\u00e9"],
)
async def test_stage_rejects_noncanonical_expected_sha_before_creating_temp(
    tmp_path: Path, expected_sha256: str
) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)

    with pytest.raises(ArtifactValidationError, match="sha256"):
        await storage.stage(upload_file(apk()), expected_sha256)

    assert list((tmp_path / ".staging").glob("*")) == []


@pytest.mark.asyncio
async def test_finalize_rejects_a_traversal_staged_path(tmp_path: Path) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = apk()
    digest = hashlib.sha256(payload).hexdigest()
    escaped_path = storage.staging_root / ".." / "outside.apk.part"
    escaped_path.write_bytes(payload)

    with pytest.raises(ArtifactValidationError, match="staging"):
        storage.finalize(StagedArtifact(escaped_path, digest, len(payload)))

    assert escaped_path.exists()


@pytest.mark.asyncio
async def test_finalize_rejects_a_storage_symlink_that_escapes_root(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    outside = tmp_path / "outside"
    outside.mkdir()
    storage = ReleaseStorage(root, max_bytes=1024)
    try:
        (root / "android").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this runner")
    payload = apk()
    staged = await storage.stage(upload_file(payload), hashlib.sha256(payload).hexdigest())

    with pytest.raises(ArtifactValidationError, match="escapes"):
        storage.finalize(staged)

    assert staged.path.exists()
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_finalize_reuses_existing_identical_content(tmp_path: Path) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = apk()
    digest = hashlib.sha256(payload).hexdigest()
    first = await storage.stage(upload_file(payload), digest)
    storage.finalize(first)
    second = await storage.stage(upload_file(payload), digest)

    stored = storage.finalize(second)

    assert (tmp_path / stored.storage_key).read_bytes() == payload
    assert not second.path.exists()


@pytest.mark.asyncio
async def test_finalize_never_reserves_an_empty_digest_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = apk()
    digest = hashlib.sha256(payload).hexdigest()
    staged = await storage.stage(upload_file(payload), digest)
    target = tmp_path / f"android/sha256/{digest}.apk"
    native_link = release_storage.os.link

    def link_without_placeholder(source: Path, destination: Path, *args, **kwargs) -> None:
        assert not target.exists(), "publication exposed an empty target"
        native_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(release_storage.os, "link", link_without_placeholder)

    stored = storage.finalize(staged)

    assert (tmp_path / stored.storage_key).read_bytes() == payload


@pytest.mark.asyncio
async def test_concurrent_identical_finalizations_converge_without_a_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = apk()
    digest = hashlib.sha256(payload).hexdigest()
    first_staged = await storage.stage(upload_file(payload), digest)
    second_staged = await storage.stage(upload_file(payload), digest)
    release_publication = Event()
    replace_started = Event()
    link_started = Event()
    link_kwargs: list[dict[str, object]] = []
    native_replace = release_storage.os.replace
    native_link = release_storage.os.link

    def delayed_replace(source: Path, destination: Path, *args, **kwargs) -> None:
        replace_started.set()
        assert release_publication.wait(timeout=2)
        native_replace(source, destination, *args, **kwargs)

    def delayed_link(source: Path, destination: Path, *args, **kwargs) -> None:
        link_started.set()
        link_kwargs.append(kwargs)
        assert release_publication.wait(timeout=2)
        native_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(release_storage.os, "replace", delayed_replace)
    monkeypatch.setattr(release_storage.os, "link", delayed_link)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(storage.finalize, first_staged)
        assert replace_started.wait(timeout=0.2) or link_started.wait(timeout=0.2)
        second = executor.submit(storage.finalize, second_staged)
        release_publication.set()
        first_stored = first.result(timeout=2)
        second_stored = second.result(timeout=2)

    assert first_stored == second_stored
    assert link_started.is_set()
    if release_storage._SUPPORTS_DIRECTORY_FILE_DESCRIPTORS:
        assert any(
            {"src_dir_fd", "dst_dir_fd", "follow_symlinks"} <= kwargs.keys()
            for kwargs in link_kwargs
        )
    assert (tmp_path / first_stored.storage_key).read_bytes() == payload
    assert not first_staged.path.exists()
    assert not second_staged.path.exists()


@pytest.mark.asyncio
async def test_stage_rejects_staging_directory_replaced_by_symlink_after_construction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    outside = tmp_path / "outside"
    outside.mkdir()
    storage = ReleaseStorage(root, max_bytes=1024)
    storage.staging_root.rmdir()
    try:
        storage.staging_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this runner")
    payload = apk()

    with pytest.raises(ArtifactValidationError, match="staging|escapes"):
        await storage.stage(upload_file(payload), hashlib.sha256(payload).hexdigest())

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_stage_fails_closed_when_windows_directory_guard_rejects_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class RejectingGuard:
        def __init__(self, directory: Path) -> None:
            self.directory = directory

        def __enter__(self) -> None:
            raise ArtifactValidationError("directory is a reparse point")

        def __exit__(self, *args: object) -> None:
            return None

    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = apk()
    monkeypatch.setattr(release_storage.sys, "platform", "win32")
    monkeypatch.setattr(
        release_storage, "_WindowsDirectoryGuard", RejectingGuard, raising=False
    )

    with pytest.raises(ArtifactValidationError, match="reparse"):
        await storage.stage(upload_file(payload), hashlib.sha256(payload).hexdigest())

    assert list(storage.staging_root.iterdir()) == []


@pytest.mark.asyncio
async def test_stage_cleans_up_before_releasing_windows_directory_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = {"active": False}
    events: list[str] = []
    native_unlink = release_storage.os.unlink

    class TrackingGuard:
        def __init__(self, directory: Path) -> None:
            self.directory = directory

        def __enter__(self) -> None:
            state["active"] = True
            events.append("enter")

        def __exit__(self, *args: object) -> None:
            events.append("exit")
            state["active"] = False

    def unlink_while_guarded(path: str | Path, *args, **kwargs) -> None:
        assert state["active"], "staging cleanup ran after releasing its guard"
        events.append("unlink")
        native_unlink(path, *args, **kwargs)

    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = apk()
    monkeypatch.setattr(release_storage.sys, "platform", "win32")
    monkeypatch.setattr(release_storage, "_WindowsDirectoryGuard", TrackingGuard)
    monkeypatch.setattr(release_storage.os, "unlink", unlink_while_guarded)

    with pytest.raises(ArtifactValidationError, match="sha256"):
        await storage.stage(upload_file(payload), "0" * 64)

    assert events == ["enter", "unlink", "exit"]
    assert list(storage.staging_root.iterdir()) == []


def test_windows_directory_guard_rejects_reparse_attribute_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation

        def __call__(self, *args):
            return self.implementation(*args)

    class FakeKernel32:
        def __init__(self) -> None:
            self.closed_handles: list[int] = []
            self.CreateFileW = FakeFunction(lambda *args: 41)
            self.GetFileInformationByHandleEx = FakeFunction(self._get_information)
            self.CloseHandle = FakeFunction(self._close_handle)

        def _get_information(self, handle, info_class, output, length) -> bool:
            info = release_storage.ctypes.cast(
                output,
                release_storage.ctypes.POINTER(
                    release_storage._WindowsDirectoryGuard._AttributeTagInfo
                ),
            ).contents
            info.file_attributes = 0x0400
            return True

        def _close_handle(self, handle: int) -> bool:
            self.closed_handles.append(handle)
            return True

    kernel32 = FakeKernel32()
    monkeypatch.setattr(release_storage.ctypes, "WinDLL", lambda *args, **kwargs: kernel32)

    with pytest.raises(ArtifactValidationError, match="reparse"):
        with release_storage._WindowsDirectoryGuard(tmp_path):
            pass

    assert kernel32.closed_handles == [41]


@pytest.mark.asyncio
async def test_finalize_fails_closed_when_windows_directory_guard_rejects_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class TargetRejectingGuard:
        def __init__(self, directory: Path) -> None:
            self.directory = directory

        def __enter__(self) -> None:
            if self.directory.name == "sha256":
                raise ArtifactValidationError("directory is a reparse point")

        def __exit__(self, *args: object) -> None:
            return None

    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = apk()
    staged = await storage.stage(upload_file(payload), hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(release_storage.sys, "platform", "win32")
    monkeypatch.setattr(
        release_storage, "_WindowsDirectoryGuard", TargetRejectingGuard, raising=False
    )

    with pytest.raises(ArtifactValidationError, match="reparse"):
        storage.finalize(staged)

    assert staged.path.exists()
    assert not list((tmp_path / "android" / "sha256").glob("*.apk"))


@pytest.mark.asyncio
async def test_finalize_preserves_a_tampered_existing_digest_target(tmp_path: Path) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = apk(b"expected")
    digest = hashlib.sha256(payload).hexdigest()
    staged = await storage.stage(upload_file(payload), digest)
    target = tmp_path / f"android/sha256/{digest}.apk"
    target.parent.mkdir(parents=True)
    tampered = apk(b"tampered")
    target.write_bytes(tampered)

    with pytest.raises(ArtifactValidationError, match="integrity collision"):
        storage.finalize(staged)

    assert target.read_bytes() == tampered
    assert staged.path.exists()


@pytest.mark.asyncio
async def test_finalize_rejects_a_staged_file_changed_after_hashing(tmp_path: Path) -> None:
    storage = ReleaseStorage(tmp_path, max_bytes=1024)
    payload = apk(b"original")
    staged = await storage.stage(upload_file(payload), hashlib.sha256(payload).hexdigest())
    staged.path.write_bytes(apk(b"changed"))

    with pytest.raises(ArtifactValidationError, match="sha256"):
        storage.finalize(staged)

    assert not (tmp_path / f"android/sha256/{staged.sha256}.apk").exists()


@pytest.mark.asyncio
async def test_stage_reads_upload_in_bounded_chunks(tmp_path: Path) -> None:
    class BoundedReadFile(BytesIO):
        def __init__(self, payload: bytes) -> None:
            super().__init__(payload)
            self.read_sizes: list[int] = []

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            if size < 0 or size > 1024 * 1024:
                raise AssertionError("upload was not read in bounded chunks")
            return super().read(size)

    payload = apk(b"x" * (1024 * 1024 + 1))
    input_file = BoundedReadFile(payload)
    storage = ReleaseStorage(tmp_path, max_bytes=len(payload))

    staged = await storage.stage(
        UploadFile(file=input_file, filename="release.apk"),
        hashlib.sha256(payload).hexdigest(),
    )

    assert input_file.read_sizes == [1024 * 1024, 1024 * 1024, 1024 * 1024]
    storage.discard(staged)
