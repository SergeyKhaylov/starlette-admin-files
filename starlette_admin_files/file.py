"""Read-only values of file columns.

`File` and `Image` describe a file that is already stored. They change nothing,
neither in the storage nor in the model: everything that changes state lives in
the model's file columns (`FileColumn` and friends). That is why these objects
have no "unsaved" state and carry no pending content.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from typing import Any, BinaryIO, cast

from anyio import to_thread
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request
from starlette_admin.storage import BaseStorage, FileInfo, get_storage

from .storage import ObjectStorage

__all__ = [
    "File",
    "Image",
    "Source",
    "as_upload",
    "generate_thumbnail",
    "read_image_size",
    "require_pillow",
    "save_thumbnail",
]

logger = logging.getLogger(__name__)

#: What `save()` accepts: a ready upload, a dict carrying the content, raw
#: bytes, or any object with `read()`.
type Source = UploadFile | dict[str, Any] | bytes | bytearray | memoryview | io.IOBase

#: Pillow calls JPEG "JPEG", while the conventional extension is ".jpg".
_FORMAT_EXTENSIONS = {"JPEG": "jpg"}

#: Modes JPEG cannot store.
_JPEG_UNSUPPORTED_MODES = ("P", "RGBA", "LA", "PA", "I", "I;16", "F")

DEFAULT_FILENAME = "file"
DEFAULT_CONTENT_TYPE = "application/octet-stream"


class File:
    """Metadata of a stored file. Immutable."""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", dict(data))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"{type(self).__name__} is read-only; change the file through the model's file column"
        )

    def __delattr__(self, name: str) -> None:
        self.__setattr__(name, None)

    @classmethod
    def from_info(cls, info: FileInfo) -> File:
        return cls(info.to_dict())

    def to_dict(self) -> dict[str, Any]:
        """The dict exactly as it is stored in the column."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.filename!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, File) and self._data == other._data

    def __hash__(self) -> int:
        return hash(self.key)

    # --- metadata --------------------------------------------------------

    @property
    def filename(self) -> str:
        """The name as the user sent it: Unicode, meant for display."""
        return self._data.get("filename", "")

    @property
    def content_type(self) -> str:
        return self._data.get("content_type", DEFAULT_CONTENT_TYPE)

    @property
    def size(self) -> int:
        return self._data.get("size", 0)

    @property
    def key(self) -> str:
        """The object key in the storage: ASCII only."""
        return self._data.get("key", "")

    @property
    def uploaded_at(self) -> datetime | None:
        value = self._data.get("uploaded_at")
        return datetime.fromisoformat(value) if isinstance(value, str) else value

    @property
    def extra(self) -> dict[str, Any]:
        return self._data.get("extra") or {}

    @property
    def storage(self) -> ObjectStorage:
        resolved: BaseStorage = get_storage(self._data["storage"])
        if not isinstance(resolved, ObjectStorage):
            raise TypeError(f"{resolved!r} is not an ObjectStorage")
        return resolved

    # --- operations ------------------------------------------------------

    async def read(self) -> bytes:
        return await self.storage.read(self.key)

    async def url(
        self, request: Request, *, signed: bool = False, expires: int | None = None
    ) -> str:
        """A fresh URL; the one stored in the database is never used."""
        return await self.storage.url(request, self.key, signed=signed, expires=expires)

    async def delete(self) -> None:
        """Remove the file from the storage.

        The reference held by the model is left alone: clear it through the
        file column (`await post.cover.delete()`), or delete deliberately
        after the transaction commits.
        """
        await self.storage.delete(self.key)


class Image(File):
    """An image: the same metadata plus dimensions and a thumbnail."""

    __slots__ = ()

    @property
    def width(self) -> int:
        return self._data.get("width") or 0

    @property
    def height(self) -> int:
        return self._data.get("height") or 0

    @property
    def thumbnail(self) -> dict[str, Any]:
        return self._data.get("thumbnail") or {}

    async def thumbnail_url(
        self, request: Request, *, signed: bool = False, expires: int | None = None
    ) -> str | None:
        key = self.thumbnail.get("key")
        if not key:
            return None
        return await self.storage.url(request, key, signed=signed, expires=expires)

    async def delete(self) -> None:
        await super().delete()
        if key := self.thumbnail.get("key"):
            await self.storage.delete(key)


# --- sources ------------------------------------------------------------


def _measure(stream: Any) -> int:
    position = stream.tell()
    try:
        stream.seek(0, os.SEEK_END)
        return stream.tell()
    finally:
        stream.seek(position)


def as_upload(
    source: Source,
    filename: str = DEFAULT_FILENAME,
    content_type: str = DEFAULT_CONTENT_TYPE,
) -> UploadFile:
    """Normalise any supported source into an `UploadFile`.

    The size is always filled in: without it the `max_size` check cannot run.
    """
    if isinstance(source, UploadFile):
        if source.size is None:
            return UploadFile(
                source.file,
                size=_measure(source.file),
                filename=source.filename,
                headers=source.headers,
            )
        return source
    if isinstance(source, dict):
        try:
            content = source["content"]
        except KeyError as err:
            raise TypeError("a dict source must carry a 'content' key") from err
        return as_upload(
            content,
            source.get("filename") or filename,
            source.get("content_type") or content_type,
        )
    if isinstance(source, (bytes, bytearray, memoryview)):
        source = io.BytesIO(bytes(source))
    if not hasattr(source, "read"):
        raise TypeError(
            f"{type(source).__name__} is not a file source: expected an UploadFile, "
            "a {'content': ...} dict, bytes, or an object with read()"
        )
    return UploadFile(
        cast("BinaryIO", source),
        size=_measure(source),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


# --- thumbnails ---------------------------------------------------------


def require_pillow(feature: str = "Image support") -> None:
    """Check that Pillow is installed and say what to do when it is not.

    Pillow is only needed for images (dimensions and thumbnails), so it is an
    extra rather than a hard dependency.
    """
    try:
        import PIL  # noqa: F401
    except ImportError as err:
        raise ImportError(
            f"{feature} requires Pillow. Install it with: "
            "pip install 'starlette-admin-files[image]' (or uv add pillow)"
        ) from err


def read_image_size(upload: UploadFile) -> tuple[int, int] | None:
    """Image dimensions, or None when Pillow cannot open the upload."""
    from PIL import Image as PILImage

    try:
        upload.file.seek(0)
        with PILImage.open(upload.file) as image:
            return image.size
    except Exception:
        return None
    finally:
        upload.file.seek(0)


def generate_thumbnail(
    upload: UploadFile, thumbnail_size: tuple[int, int]
) -> tuple[bytes, str, tuple[int, int]]:
    """Build a thumbnail bounded by `thumbnail_size`.

    Returns the bytes, the file extension, and the resulting size. The aspect
    ratio is preserved, the image is never upscaled, the source format is kept
    when possible, and modes JPEG cannot store are converted first.
    """
    from PIL import Image as PILImage

    try:
        upload.file.seek(0)
        with PILImage.open(upload.file) as source:
            image_format = source.format or "PNG"
            thumbnail = source.copy()
    finally:
        upload.file.seek(0)

    thumbnail.thumbnail(thumbnail_size)
    if image_format in ("JPEG", "JPG") and thumbnail.mode in _JPEG_UNSUPPORTED_MODES:
        thumbnail = thumbnail.convert("RGB")
    buffer = io.BytesIO()
    try:
        thumbnail.save(buffer, format=image_format)
    except (OSError, KeyError, ValueError):
        # The source format cannot be written back (an ICO in an odd mode, say).
        image_format = "PNG"
        buffer = io.BytesIO()
        thumbnail = thumbnail.convert("RGBA")
        thumbnail.save(buffer, format=image_format)
    extension = _FORMAT_EXTENSIONS.get(image_format, image_format.lower())
    return buffer.getvalue(), extension, thumbnail.size


async def save_thumbnail(
    storage: BaseStorage,
    info: FileInfo,
    upload: UploadFile,
    thumbnail_size: tuple[int, int],
) -> FileInfo:
    """Store a thumbnail next to the original and record it in `FileInfo`.

    A failure to generate one never fails the upload, mirroring
    `ImageField._post_store` upstream.

    Unlike upstream, the Pillow work runs in a worker thread. Building a
    thumbnail from a 12MP photo costs around 60ms of CPU, which is 60ms the
    event loop spends serving nobody — and an admin is usually mounted into
    an application that is answering other requests at the same time. Pillow
    releases the GIL for the decode, the resize and the encode, so the thread
    is real parallelism rather than a way of yielding.
    """
    from dataclasses import replace

    try:
        content, extension, (width, height) = await to_thread.run_sync(
            generate_thumbnail, upload, thumbnail_size
        )
        stem = info.key.rsplit(".", 1)[0] if "." in info.key else info.key
        dest = f"{stem}.thumb.{extension}"
        thumbnail_info = await storage.save(
            UploadFile(
                io.BytesIO(content),
                size=len(content),
                filename=dest.rsplit("/", 1)[-1],
                headers=Headers({"content-type": f"image/{extension}"}),
            ),
            dest,
        )
    except ImportError:
        # Never disguise a missing dependency as "the thumbnail did not work".
        raise
    except Exception:
        logger.warning(
            "thumbnail generation failed for key=%r; falling back to the full image",
            info.key,
            exc_info=True,
        )
        return info
    return replace(
        info,
        thumbnail={
            "key": thumbnail_info.key,
            "width": width,
            "height": height,
            "size": thumbnail_info.size,
        },
    )
