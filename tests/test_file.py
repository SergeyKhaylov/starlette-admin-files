"""File and Image values, and the source normaliser behind `save()`."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import cast

import pytest
from starlette_admin_files import File, Image, ObjectStorage
from starlette_admin_files.file import (
    as_upload,
    generate_thumbnail,
    read_image_size,
    save_thumbnail,
)

from conftest import FakeRequest, png_bytes, upload

DATA = {
    "filename": "отчёт.pdf",
    "content_type": "application/pdf",
    "size": 11,
    "storage": "media",
    "key": "media/docs/ab12cd34/otchet.pdf",
    "url": "",
    "extra": {"author": "me"},
    "uploaded_at": "2026-09-19T10:00:00+00:00",
}


def test_metadata_is_exposed_as_properties() -> None:
    file = File(DATA)

    assert file.filename == "отчёт.pdf"
    assert file.content_type == "application/pdf"
    assert file.size == 11
    assert file.key == DATA["key"]
    assert file.extra == {"author": "me"}
    assert file.uploaded_at == datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


def test_file_is_read_only() -> None:
    file = File(DATA)

    with pytest.raises(AttributeError, match="read-only"):
        file.filename = "other.pdf"  # type: ignore[misc]
    with pytest.raises(AttributeError, match="read-only"):
        del file.filename  # type: ignore[misc]


def test_to_dict_round_trips_and_copies() -> None:
    file = File(DATA)
    dumped = file.to_dict()
    dumped["filename"] = "changed"

    assert File(file.to_dict()) == file
    assert file.filename == "отчёт.pdf"


def test_equality_and_hash() -> None:
    assert File(DATA) == File(dict(DATA))
    assert File(DATA) != File({**DATA, "key": "other"})
    assert len({File(DATA), File(dict(DATA))}) == 1


def test_image_defaults_when_metadata_is_missing() -> None:
    image = Image(DATA)

    assert (image.width, image.height) == (0, 0)
    assert image.thumbnail == {}


async def test_image_thumbnail_url_is_none_without_a_thumbnail(
    storage: ObjectStorage, fake_request: FakeRequest
) -> None:
    image = Image({**DATA, "storage": storage.name})

    assert await image.thumbnail_url(fake_request) is None  # type: ignore[arg-type]


async def test_read_url_and_delete_go_through_the_storage(
    storage: ObjectStorage, fake_request: FakeRequest
) -> None:
    info = await storage.save(upload(b"payload", "a.txt", "text/plain"), "a.txt")
    file = File.from_info(info)

    assert await file.read() == b"payload"
    assert (await file.url(fake_request)).endswith(file.key)  # type: ignore[arg-type]

    await file.delete()
    with pytest.raises(FileNotFoundError):
        await file.read()


async def test_image_delete_also_removes_the_thumbnail(storage: ObjectStorage) -> None:
    info = await storage.save(upload(), "covers/cat.png")
    info = await save_thumbnail(storage, info, upload(), (50, 50))
    image = cast("Image", Image.from_info(info))

    await image.delete()

    with pytest.raises(FileNotFoundError):
        await storage.read(image.key)
    with pytest.raises(FileNotFoundError):
        await storage.read(image.thumbnail["key"])


def test_unknown_storage_name_raises() -> None:
    from starlette_admin.storage import UnknownStorageError

    with pytest.raises(UnknownStorageError):
        _ = File({**DATA, "storage": "nowhere"}).storage


# --- sources ------------------------------------------------------------


def test_as_upload_passes_an_upload_through() -> None:
    original = upload(b"x", "a.txt", "text/plain")

    assert as_upload(original) is original


def test_as_upload_fills_in_a_missing_size() -> None:
    from starlette.datastructures import UploadFile

    sized = as_upload(UploadFile(io.BytesIO(b"12345"), filename="a.txt"))

    assert sized.size == 5


@pytest.mark.parametrize(
    "source",
    [b"hello", bytearray(b"hello"), memoryview(b"hello"), io.BytesIO(b"hello")],
)
def test_as_upload_accepts_binary_sources(source: object) -> None:
    result = as_upload(source, "a.txt", "text/plain")  # type: ignore[arg-type]

    assert result.size == 5
    assert result.filename == "a.txt"
    assert result.content_type == "text/plain"


def test_as_upload_accepts_a_dict() -> None:
    result = as_upload({"content": b"hello", "filename": "d.txt", "content_type": "text/plain"})

    assert (result.filename, result.content_type, result.size) == ("d.txt", "text/plain", 5)


def test_as_upload_defaults() -> None:
    result = as_upload(b"hello")

    assert result.filename == "file"
    assert result.content_type == "application/octet-stream"


def test_as_upload_rejects_other_types() -> None:
    with pytest.raises(TypeError, match="not a file source"):
        as_upload(42)  # type: ignore[arg-type]


def test_as_upload_requires_content_in_a_dict() -> None:
    with pytest.raises(TypeError, match="'content' key"):
        as_upload({"filename": "a.txt"})


# --- thumbnails ---------------------------------------------------------


def test_read_image_size() -> None:
    assert read_image_size(upload(png_bytes((320, 200)))) == (320, 200)


def test_read_image_size_returns_none_for_non_images() -> None:
    assert read_image_size(upload(b"not an image", "a.txt", "text/plain")) is None


@pytest.mark.parametrize(
    ("mode", "fmt", "extension"),
    [("RGB", "PNG", "png"), ("RGB", "JPEG", "jpg"), ("RGBA", "PNG", "png")],
)
def test_generate_thumbnail_keeps_the_format(mode: str, fmt: str, extension: str) -> None:
    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new(mode, (400, 200)).save(buffer, format=fmt)

    content, ext, size = generate_thumbnail(upload(buffer.getvalue()), (100, 100))

    assert ext == extension
    assert size == (100, 50)  # aspect ratio preserved
    assert len(content) > 0


def test_generate_thumbnail_never_upscales() -> None:
    _, _, size = generate_thumbnail(upload(png_bytes((40, 20))), (100, 100))

    assert size == (40, 20)


async def test_save_thumbnail_failure_keeps_the_upload(storage: ObjectStorage) -> None:
    info = await storage.save(upload(b"not an image", "a.txt", "text/plain"), "a.txt")

    result = await save_thumbnail(
        storage, info, upload(b"not an image", "a.txt", "text/plain"), (10, 10)
    )

    assert result.thumbnail is None
    assert await storage.read(info.key) == b"not an image"
