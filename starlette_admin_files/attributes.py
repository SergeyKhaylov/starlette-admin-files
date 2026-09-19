"""Model attributes for file columns.

An attribute is the only place where a file appears and disappears: it holds
the parameters (storage, folder, limits), hands them to the admin field through
`field_params`, reads the column as `File`/`Image`, and writes the column on an
explicit `save()`, `replace()` or `delete()`.

    class Post(Base):
        _cover: Mapped[dict | None] = mapped_column("cover", JSON)
        cover = ImageAttribute("_cover", storage=storage, upload_folder="covers",
                               thumbnail_size=(200, 200))

    class PostView(ModelView):
        fields = [ImageField(**Post.cover.field_params, label="Cover")]

    await post.cover.save(upload)
    if post.cover.image is not None:
        content = await post.cover.image.read()
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace as dc_replace
from typing import Any, Self, cast, overload

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette_admin.fields import DEFAULT_MAX_UPLOAD_SIZE, BaseField
from starlette_admin.storage import FileInfo, secure_filename
from starlette_admin.validators import file_size, file_type

from .file import (
    DEFAULT_CONTENT_TYPE,
    DEFAULT_FILENAME,
    File,
    Image,
    Source,
    as_upload,
    read_image_size,
    require_pillow,
    save_thumbnail,
)
from .storage import ObjectStorage

__all__ = [
    "BoundBase",
    "BoundFile",
    "BoundFileList",
    "BoundImage",
    "BoundImageList",
    "FileAttribute",
    "FileListAttribute",
    "ImageAttribute",
    "ImageListAttribute",
]

logger = logging.getLogger(__name__)

#: Key in `Session.info` collecting files uploaded during the current
#: transaction. See `cleanup.uploaded_files`.
UPLOADED_KEY = "starlette_admin_files.uploaded"
_REQUIRED_KEYS = frozenset({"filename", "content_type", "size", "storage", "key"})

class _ValidationField:
    """A stand-in for a field: upstream validators only read `name`."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class BaseFileAttribute[B: BoundBase]:
    """Shared behaviour of the single-file and list attributes.

    The type parameter is the bound class the descriptor returns, which is what
    makes `post.cover` resolve to `BoundImage` while `Post.cover` resolves to
    `ImageAttribute` itself.
    """

    file_class: type[File] = File
    bound_class: type[B]
    multiple: bool = False
    default_accept: str | None = None
    #: Only image attributes take one; it stays here so `field_params` and
    #: `enrich` can read it unconditionally.
    thumbnail_size: tuple[int, int] | None = None

    def __init__(
        self,
        column: str,
        *,
        storage: ObjectStorage,
        upload_folder: str = "",
        max_size: int | None = DEFAULT_MAX_UPLOAD_SIZE,
        accept: str | None = None,
    ) -> None:
        self.column = column
        self.storage = storage
        self.upload_folder = upload_folder
        self.max_size = max_size
        self.accept = accept if accept is not None else self.default_accept
        self.name = column.lstrip("_")
        self._cache_key = f"__file_attribute_{column}"

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name
        self._cache_key = f"__file_attribute_{name}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.column!r}, storage={self.storage.name!r})"

    # --- admin field parameters ------------------------------------------

    @property
    def field_params(self) -> dict[str, Any]:
        """Arguments for `FileField`/`ImageField`, including the column name.

        ImageField(**Post.cover.field_params, label="Cover")
        """
        params: dict[str, Any] = {
            "name": self.column,
            "storage": self.storage,
            "upload_folder": self.upload_folder,
            "multiple": self.multiple,
            "max_size": self.max_size,
        }
        if self.accept is not None:
            params["accept"] = self.accept
        if self.thumbnail_size is not None:
            params["thumbnail_size"] = self.thumbnail_size
        return params

    # --- descriptor -------------------------------------------------------

    @overload
    def __get__(self, obj: None, owner: type | None = None) -> Self: ...

    @overload
    def __get__(self, obj: object, owner: type | None = None) -> B: ...

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        if obj is None:
            return self
        bound = obj.__dict__.get(self._cache_key)
        if bound is None:
            bound = self.bound_class(self, obj)
            obj.__dict__[self._cache_key] = bound
        return bound

    def __set__(self, obj: Any, value: Any) -> None:
        raise AttributeError(
            f"{self.name} only changes through the attribute: "
            f"await obj.{self.name}.save(...) / .replace(...) / .delete()"
        )

    # --- uploading --------------------------------------------------------

    def validate(self, upload: UploadFile) -> None:
        """Run the same checks the admin form runs.

        The validators are starlette-admin's own, so that the field and the
        backend cannot drift apart.
        """
        # Both validators read `field.name` only and never touch the request.
        request = cast("Request", None)
        field = cast("BaseField", _ValidationField(self.column))
        if self.max_size is not None:
            file_size(self.max_size)(request, field, upload, {})
        if self.accept is not None:
            file_type(self.accept)(request, field, upload, {})

    async def store(
        self,
        source: Source,
        filename: str = DEFAULT_FILENAME,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> dict[str, Any]:
        """Validate and store a file, returning the dict for the column."""
        upload = as_upload(source, filename, content_type)
        self.validate(upload)
        folder = self.upload_folder.strip("/")
        name = secure_filename(upload.filename or filename)
        dest = f"{folder}/{name}" if folder else name
        info = await self.storage.save(upload, dest)
        info = await self.enrich(info, upload)
        return info.to_dict()

    async def enrich(self, info: FileInfo, upload: UploadFile) -> FileInfo:
        """Hook for subclasses; see `ImageAttribute`."""
        return info


class _ImageMixin:
    """Shared behaviour of the image attributes.

    It deliberately does not define `__init__`: a `(*args, **kwargs)` wrapper
    would sit first in the MRO and hide the real parameters from editors and
    type checkers. The image attributes spell their signature out instead.
    """

    file_class = Image
    default_accept = "image/*"

    async def enrich(self: Any, info: FileInfo, upload: UploadFile) -> FileInfo:
        if size := read_image_size(upload):
            info = dc_replace(info, width=size[0], height=size[1])
        if self.thumbnail_size:
            info = await save_thumbnail(self.storage, info, upload, self.thumbnail_size)
        return info


class BoundBase:
    """Shared behaviour of attributes bound to a model instance."""

    __slots__ = ("attribute", "obj")

    def __init__(self, attribute: BaseFileAttribute[Any], obj: Any) -> None:
        self.attribute = attribute
        self.obj = obj

    @property
    def _raw(self) -> Any:
        return getattr(self.obj, self.attribute.column)

    @staticmethod
    def _checked(value: Any) -> dict[str, Any]:
        if isinstance(value, (File, FileInfo)):
            value = value.to_dict()
        if not isinstance(value, dict):
            raise ValueError(
                f"{type(value).__name__} is not a file value: expected File, FileInfo or dict"
            )
        if missing := _REQUIRED_KEYS - value.keys():
            raise ValueError(f"file value is missing keys: {sorted(missing)}")
        if not value["key"]:
            raise ValueError("file value has an empty key: was the file saved?")
        return dict(value)

    def _write(self, value: Any) -> None:
        """Write a validated value into the column; `None` clears it."""
        if value is None:
            written = None
        elif isinstance(value, (list, tuple)):
            written = [self._checked(item) for item in value]
        else:
            written = self._checked(value)
        setattr(self.obj, self.attribute.column, written)

    def _remember_upload(self, data: dict[str, Any]) -> None:
        """Record the upload on the session so a rollback can clean it up."""
        from sqlalchemy.orm import object_session

        session = object_session(self.obj)
        if session is not None:
            session.info.setdefault(UPLOADED_KEY, []).append(data)

    def __bool__(self) -> bool:
        return bool(self._raw)


class BoundFile[F: File](BoundBase):
    """A single-file attribute bound to a model instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.file!r})"

    @property
    def file(self) -> F | None:
        raw = self._raw
        return cast("F | None", self.attribute.file_class(raw)) if raw else None

    async def save(
        self,
        source: Source,
        filename: str = DEFAULT_FILENAME,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> F:
        """Upload a file and write it into the column, replacing the previous one.

        The previous file is left in the storage; see `cleanup.orphaned_files`.
        """
        data = await self.attribute.store(source, filename, content_type)
        self._remember_upload(data)
        self._write(data)
        return cast("F", self.attribute.file_class(data))

    async def delete(self, *, remove: bool = True) -> F | None:
        """Clear the column; with `remove=True` also delete from the storage."""
        current = self.file
        self._write(None)
        if current is not None and remove:
            await current.delete()
        return current


class BoundImage(BoundFile[Image]):
    """A single image bound to a model instance."""

    __slots__ = ()

    @property
    def image(self) -> Image | None:
        """Alias of `file`."""
        return self.file


class BoundFileList[F: File](BoundBase):
    """A file-list attribute bound to a model instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.files!r})"

    @property
    def files(self) -> list[F]:
        return cast("list[F]", [self.attribute.file_class(item) for item in (self._raw or [])])

    def __len__(self) -> int:
        return len(self._raw or [])

    def __iter__(self) -> Iterator[F]:
        return iter(self.files)

    @overload
    def __getitem__(self, index: int) -> F: ...

    @overload
    def __getitem__(self, index: slice) -> list[F]: ...

    def __getitem__(self, index: int | slice) -> F | list[F]:
        return self.files[index]

    async def _store_all(
        self, sources: Source | Sequence[Source], filename: str, content_type: str
    ) -> list[dict[str, Any]]:
        items: list[Source] = (
            list(sources) if isinstance(sources, (list, tuple)) else [cast("Source", sources)]
        )
        stored = []
        for item in items:
            data = await self.attribute.store(item, filename, content_type)
            self._remember_upload(data)
            stored.append(data)
        return stored

    async def save(
        self,
        source: Source | Sequence[Source],
        filename: str = DEFAULT_FILENAME,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> list[F]:
        """Append one file or a list of files to the ones already stored."""
        stored = await self._store_all(source, filename, content_type)
        # A new list rather than a mutation: SQLAlchemy would not see the change.
        self._write([*(self._raw or []), *stored])
        return cast("list[F]", [self.attribute.file_class(item) for item in stored])

    async def replace(
        self,
        source: Source | Sequence[Source],
        filename: str = DEFAULT_FILENAME,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> list[F]:
        """Replace the whole set with one file or a list of files."""
        stored = await self._store_all(source, filename, content_type)
        self._write(stored or None)
        return cast("list[F]", [self.attribute.file_class(item) for item in stored])

    async def delete(self, index: int | list[int] | None = None, *, remove: bool = True) -> list[F]:
        """Drop files by index, by a list of indexes, or all of them."""
        current = self.files
        if index is None:
            removed, kept = current, []
        else:
            indexes = {index} if isinstance(index, int) else set(index)
            for position in indexes:
                if not -len(current) <= position < len(current):
                    raise IndexError(f"no file at index {position}")
            normalized = {position % len(current) for position in indexes}
            removed = [f for i, f in enumerate(current) if i in normalized]
            kept = [f for i, f in enumerate(current) if i not in normalized]
        self._write([f.to_dict() for f in kept] or None)
        if remove:
            for file in removed:
                await file.delete()
        return removed


class BoundImageList(BoundFileList[Image]):
    """A list of images bound to a model instance."""

    __slots__ = ()

    @property
    def images(self) -> list[Image]:
        """Alias of `files`."""
        return self.files


class FileAttribute(BaseFileAttribute[BoundFile[File]]):
    """A single file."""

    bound_class: type[BoundFile[File]] = BoundFile


class ImageAttribute(_ImageMixin, BaseFileAttribute[BoundImage]):
    """A single image; dimensions and the thumbnail are computed on save."""

    bound_class: type[BoundImage] = BoundImage

    def __init__(
        self,
        column: str,
        *,
        storage: ObjectStorage,
        upload_folder: str = "",
        max_size: int | None = DEFAULT_MAX_UPLOAD_SIZE,
        accept: str | None = None,
        thumbnail_size: tuple[int, int] | None = None,
    ) -> None:
        # Fail while the model is being declared, not on the first upload.
        require_pillow(type(self).__name__)
        super().__init__(
            column,
            storage=storage,
            upload_folder=upload_folder,
            max_size=max_size,
            accept=accept,
        )
        self.thumbnail_size = thumbnail_size


class FileListAttribute(BaseFileAttribute[BoundFileList[File]]):
    """A list of files (`multiple=True`)."""

    bound_class: type[BoundFileList[File]] = BoundFileList
    multiple = True


class ImageListAttribute(_ImageMixin, BaseFileAttribute[BoundImageList]):
    """A list of images."""

    bound_class: type[BoundImageList] = BoundImageList
    multiple = True

    def __init__(
        self,
        column: str,
        *,
        storage: ObjectStorage,
        upload_folder: str = "",
        max_size: int | None = DEFAULT_MAX_UPLOAD_SIZE,
        accept: str | None = None,
        thumbnail_size: tuple[int, int] | None = None,
    ) -> None:
        require_pillow(type(self).__name__)
        super().__init__(
            column,
            storage=storage,
            upload_folder=upload_folder,
            max_size=max_size,
            accept=accept,
        )
        self.thumbnail_size = thumbnail_size
