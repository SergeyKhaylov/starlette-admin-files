"""Model columns for files.

A file column is the only place where a file appears and disappears: it
declares the mapped column, holds the parameters (storage, folder, limits),
hands them to the admin field through `field_params`, reads the column as
`File`/`Image`, and writes it on an explicit `save()`, `replace()` or
`delete()`.

    class Post(Base):
        cover = ImageColumn(storage=storage, upload_folder="covers",
                            thumbnail_size=(200, 200))

    class PostView(ModelView):
        fields = [ImageField(**Post.cover.field_params, label="Cover")]

    await post.cover.save(upload)
    if post.cover.image is not None:
        content = await post.cover.image.read()

The declaration above adds a `JSON(none_as_null=True)` column named "cover",
mapped as `Post._cover`. The first two positional arguments are the column's
name and type, exactly as `mapped_column` takes them, and the keyword
arguments it accepts are the ones that mean something on a file column; see
`_COLUMN_PARAMS`. When the model or a `Table` declares the column itself,
`FileColumn.existing("_cover", ...)` attaches to it instead.

`Post.cover` is that column as an expression — `select(Post).where(
Post.cover.is_(None))` — with `field_params` and the column object itself
(`Post.cover.file_column`) reachable through it. `post.cover` is the bound
object the files go through.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import replace as dc_replace
from typing import TYPE_CHECKING, Any, Self, cast, overload

from sqlalchemy import JSON, inspection
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm.attributes import QueryableAttribute
from sqlalchemy.sql.operators import ColumnOperators
from sqlalchemy.types import TypeEngine
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
    "ColumnExpression",
    "FileColumn",
    "FileListColumn",
    "ImageColumn",
    "ImageListColumn",
]

logger = logging.getLogger(__name__)

#: Key in `Session.info` collecting files uploaded during the current
#: transaction. See `cleanup.uploaded_files`.
UPLOADED_KEY = "starlette_admin_files.uploaded"
_REQUIRED_KEYS = frozenset({"filename", "content_type", "size", "storage", "key"})

#: The `mapped_column` keyword arguments a file column passes through. The rest
#: of SQLAlchemy's are left out on purpose, in four groups:
#:
#: * `primary_key`, `system`, `active_history` break the column outright — the
#:   first two make it unwritable, the third raises `MissingGreenlet` under
#:   asyncio when the previous value has to be fetched;
#: * `default`, `insert_default`, `onupdate` write a file value past `save()`,
#:   leaving metadata in the row for a file nothing ever uploaded;
#: * `init`, `repr`, `compare`, `kw_only`, `hash`, `default_factory`,
#:   `dataclass_metadata`, `autoincrement` and `key` do nothing here — the
#:   declared column carries no annotation, so it is not a dataclass field, and
#:   `key` renames only how `table.c` addresses it;
#: * `name` and `type_` duplicate the two positional arguments, and `unique` is
#:   already satisfied by every upload getting its own key.
#:
#: Each defaults to `None` and is forwarded only when set, so nothing has to
#: mirror SQLAlchemy's own defaults or import their private sentinels.
_COLUMN_PARAMS = (
    "nullable",
    "index",
    "deferred",
    "deferred_group",
    "deferred_raiseload",
    "server_default",
    "server_onupdate",
    "comment",
    "doc",
    "info",
    "sort_order",
    "use_existing_column",
    "quote",
)


class _ValidationField:
    """A stand-in for a field: upstream validators only read `name`."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class _ColumnExpression(ColumnOperators):
    """What class access returns: the column expression, and the file column.

    The class-level handle has to be two things at once — what queries are
    written against (`Post.cover.is_(None)`) and what carries `field_params`
    into the admin field. It is built per access rather than stored on the file
    column, because one declared on a mixin serves several mapped classes and
    each needs its own expression.

    Delegating through `ColumnOperators` rather than a bare `__getattr__` proxy
    is what makes it safe: operators are looked up on the type, so a proxy
    without them would answer `Post.cover == value` with Python's identity
    comparison and quietly compile to `false`.
    """

    __slots__ = ("file_column", "_target")

    def __init__(self, file_column: BaseFileColumn[Any], target: QueryableAttribute[Any]) -> None:
        self.file_column = file_column
        self._target = target

    # --- expression -------------------------------------------------------

    def __clause_element__(self) -> QueryableAttribute[Any]:
        return self._target

    def operate(self, op: Any, *other: Any, **kwargs: Any) -> Any:
        return op(self._target.comparator, *other, **kwargs)

    def reverse_operate(self, op: Any, other: Any, **kwargs: Any) -> Any:
        return op(other, self._target.comparator, **kwargs)

    # --- the file column --------------------------------------------------

    @property
    def field_params(self) -> dict[str, Any]:
        """Arguments for `FileField`/`ImageField`; see `BaseFileColumn`."""
        return self.file_column.field_params

    def __getattr__(self, name: str) -> Any:
        # Everything this class does not define belongs to the mapped attribute.
        if name in _ColumnExpression.__slots__:
            raise AttributeError(name)
        return getattr(self._target, name)

    def __repr__(self) -> str:
        return f"{self.file_column!r} -> {self._target}"


@inspection._inspects(_ColumnExpression)
def _inspect_column_expression(target: _ColumnExpression) -> Any:
    """Let `inspect()` see the mapped attribute behind the handle.

    The loader options — `load_only`, `defer`, `undefer` — inspect their
    argument instead of calling `__clause_element__`, and refuse outright
    anything `inspect()` does not recognise. Later 2.0.x releases coerce
    first and hide the gap; 2.0.36, the oldest release this package
    supports, raises `ArgumentError`.
    """
    return sa_inspect(target.__clause_element__())


if TYPE_CHECKING:

    class ColumnExpression(QueryableAttribute[Any]):
        # The static face of `_ColumnExpression`, for type checkers only: the
        # runtime object is not a QueryableAttribute, it forwards to one.
        # Declaring the inheritance is what keeps `select()`, `where()` and
        # `load_only()` type-checking at the call site.
        file_column: BaseFileColumn[Any]

        @property
        def field_params(self) -> dict[str, Any]: ...

else:
    ColumnExpression = _ColumnExpression


class BaseFileColumn[B: BoundBase]:
    """Shared behaviour of the single-file and list columns.

    The type parameter is the bound class the descriptor returns, which is what
    makes `post.cover` resolve to `BoundImage` while `Post.cover` resolves to
    the column expression.
    """

    file_class: type[File] = File
    bound_class: type[B]
    multiple: bool = False
    default_accept: str | None = None
    #: Only image columns take one; it stays here so `field_params` and
    #: `enrich` can read it unconditionally.
    thumbnail_size: tuple[int, int] | None = None

    def __init__(
        self,
        name_pos: str | TypeEngine[Any] | None = None,
        type_pos: TypeEngine[Any] | None = None,
        /,
        *,
        storage: ObjectStorage,
        upload_folder: str = "",
        max_size: int | None = DEFAULT_MAX_UPLOAD_SIZE,
        accept: str | None = None,
        nullable: bool | None = None,
        index: bool | None = None,
        deferred: bool | None = None,
        deferred_group: str | None = None,
        deferred_raiseload: bool | None = None,
        server_default: Any | None = None,
        server_onupdate: Any | None = None,
        comment: str | None = None,
        doc: str | None = None,
        info: dict[Any, Any] | None = None,
        sort_order: int | None = None,
        use_existing_column: bool | None = None,
        quote: bool | None = None,
    ) -> None:
        # The two positional arguments are `mapped_column`'s own: either of
        # them may be the type, and the name may be left out entirely, in which
        # case it is taken from the attribute name in `__set_name__`.
        self.storage = storage
        self.upload_folder = upload_folder
        self.max_size = max_size
        self.accept = accept if accept is not None else self.default_accept
        #: Name of the mapped attribute holding the dict. `__set_name__` fills
        #: it in when the column declares itself.
        self.column = ""
        self.name = ""
        self._cache_key = "__file_column_"
        self._declares_column = True
        self._column_name, self._column_type = self._split_positional(name_pos, type_pos)
        given = locals()
        self._column_kwargs = {key: given[key] for key in _COLUMN_PARAMS if given[key] is not None}
        if self._column_kwargs.get("nullable") is False:
            self._check_none_as_null(self._column_type)

    @classmethod
    def existing(
        cls,
        column: str,
        *,
        storage: ObjectStorage,
        upload_folder: str = "",
        max_size: int | None = DEFAULT_MAX_UPLOAD_SIZE,
        accept: str | None = None,
        **kwargs: Any,
    ) -> Self:
        """Attach to a column the model declares, instead of declaring one.

        Needed wherever the column cannot come from here: a declarative class
        with an explicit `__table__` (a reflected or hand-written schema), an
        imperatively mapped class, or a column other code also maps.

            class Report(Base):
                __table__ = legacy_table
                _document = legacy_table.c.document
                document = FileColumn.existing("_document", storage=storage)

        `column` is the name of the *mapped attribute*, not of the column in
        the database.
        """
        self = cls(
            storage=storage,
            upload_folder=upload_folder,
            max_size=max_size,
            accept=accept,
            **kwargs,
        )
        if self._column_kwargs:
            raise TypeError(
                f"{cls.__name__}.existing() describes a column the model already "
                f"declares, so it takes no column arguments: "
                f"{sorted(self._column_kwargs)}"
            )
        self.column = column
        self.name = column.lstrip("_")
        self._cache_key = f"__file_column_{column}"
        self._declares_column = False
        return self

    @staticmethod
    def _split_positional(
        name_pos: str | TypeEngine[Any] | None, type_pos: TypeEngine[Any] | None
    ) -> tuple[str | None, TypeEngine[Any]]:
        """Sort `mapped_column`'s two positional arguments into name and type."""
        name: str | None = None
        type_: TypeEngine[Any] | None = None
        for value in (name_pos, type_pos):
            if value is None:
                continue
            if isinstance(value, str):
                if name is not None:
                    raise TypeError(f"two column names given: {name!r} and {value!r}")
                name = value
            elif type_ is not None:
                raise TypeError(f"two column types given: {type_!r} and {value!r}")
            else:
                type_ = value
        return name, type_ if type_ is not None else JSON(none_as_null=True)

    @staticmethod
    def _check_none_as_null(column_type: TypeEngine[Any]) -> None:
        """Refuse a NOT NULL column whose type stores a cleared value as JSON null.

        With `none_as_null=False` an emptied column holds a JSON `null`, which
        satisfies NOT NULL — so the constraint would accept exactly the state it
        was added to forbid. With a nullable column the choice is the model's:
        a JSON `null` tells "had a file, cleared" apart from "never had one".
        """
        variants = getattr(column_type, "_variant_mapping", {})
        for label, candidate in [
            ("", column_type),
            *((f" for {d!r}", v) for d, v in variants.items()),
        ]:
            # `None` means the type has no such notion; only an explicit False is wrong.
            if getattr(candidate, "none_as_null", None) is False:
                raise TypeError(
                    f"nullable=False with {candidate!r}{label}: clearing the column would "
                    f"store a JSON null, which passes NOT NULL. Use none_as_null=True, or "
                    f"drop nullable=False if a cleared file should stay distinguishable."
                )

    def __set_name__(self, owner: type, name: str) -> None:
        if not self._declares_column:
            return
        self.name = name
        self.column = f"_{name}"
        self._cache_key = f"__file_column_{name}"
        self._declare_column(owner, name)

    def _declare_column(self, owner: type, name: str) -> None:
        """Add the mapped column this file column reads and writes.

        `__set_name__` runs inside `type.__new__`, which is before the
        declarative base maps the class, so the column is in place by the time
        SQLAlchemy looks for one. The plain name (`cover`) stays the descriptor
        and the mapped column takes the underscored one (`_cover`).
        """
        if name.startswith("_"):
            raise TypeError(
                f"{type(self).__name__} names its column after the attribute, so the "
                f"attribute cannot start with an underscore: {owner.__name__}.{name}. "
                f"Declare the column in the model and use {type(self).__name__}.existing()."
            )
        if self.column in owner.__dict__:
            raise TypeError(
                f"{owner.__name__}.{self.column} already exists; use "
                f"{type(self).__name__}.existing({self.column!r}, ...) to attach to it"
            )
        kwargs = dict(self._column_kwargs)
        # The file column claims a slot in `info`; anything the caller put there
        # is kept alongside it rather than replaced.
        kwargs["info"] = {**kwargs.get("info", {}), "file_column": self}
        # The name is always passed on: left out, `mapped_column` would name the
        # column after the mapped attribute and it would come out underscored.
        setattr(
            owner,
            self.column,
            mapped_column(self._column_name or name, self._column_type, **kwargs),
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.column!r}, storage={self.storage.name!r})"

    # --- admin field parameters ------------------------------------------

    @property
    def field_params(self) -> dict[str, Any]:
        """Arguments for `FileField`/`ImageField`, including the column name.

        ImageField(**Post.cover.field_params, label="Cover")
        """
        if not self.column:
            raise RuntimeError(
                f"{type(self).__name__} has no column yet: the name is only settled "
                f"once the file column is assigned in a model"
            )
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
    def __get__(self, obj: None, owner: type | None = None) -> ColumnExpression: ...

    @overload
    def __get__(self, obj: object, owner: type | None = None) -> B: ...

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        if obj is None:
            return self._expression(owner)
        bound = obj.__dict__.get(self._cache_key)
        if bound is None:
            bound = self.bound_class(self, obj)
            obj.__dict__[self._cache_key] = bound
        return bound

    def _expression(self, owner: type | None) -> Any:
        """The handle class access returns.

        Falls back to the file column itself while there is no expression to hand
        out: on a mixin that is not mapped, and on a class still being declared.
        Both keep `field_params` working, which is all they are asked for.
        """
        mapped = getattr(owner, self.column, None) if owner is not None else None
        if not isinstance(mapped, QueryableAttribute):
            return self
        return _ColumnExpression(self, mapped)

    def __set__(self, obj: Any, value: Any) -> None:
        raise AttributeError(
            f"{self.name} only changes through the file column: "
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
        """Hook for subclasses; see `ImageColumn`."""
        return info


class _ImageMixin:
    """Shared behaviour of the image columns.

    It deliberately does not define `__init__`: a `(*args, **kwargs)` wrapper
    would sit first in the MRO and hide the real parameters from editors and
    type checkers. The image columns spell their signature out instead.
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
    """Shared behaviour of file columns bound to a model instance."""

    __slots__ = ("file_column", "obj")

    def __init__(self, file_column: BaseFileColumn[Any], obj: Any) -> None:
        self.file_column = file_column
        self.obj = obj

    @property
    def _raw(self) -> Any:
        return getattr(self.obj, self.file_column.column)

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
        setattr(self.obj, self.file_column.column, written)

    def _remember_upload(self, data: dict[str, Any]) -> None:
        """Record the upload on the session so a rollback can clean it up."""
        from sqlalchemy.orm import object_session

        session = object_session(self.obj)
        if session is not None:
            session.info.setdefault(UPLOADED_KEY, []).append(data)

    def __bool__(self) -> bool:
        return bool(self._raw)


class BoundFile[F: File](BoundBase):
    """A single file bound to a model instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.file!r})"

    @property
    def file(self) -> F | None:
        raw = self._raw
        return cast("F | None", self.file_column.file_class(raw)) if raw else None

    async def save(
        self,
        source: Source,
        filename: str = DEFAULT_FILENAME,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> F:
        """Upload a file and write it into the column, replacing the previous one.

        The previous file is left in the storage; see `cleanup.orphaned_files`.
        """
        data = await self.file_column.store(source, filename, content_type)
        self._remember_upload(data)
        self._write(data)
        return cast("F", self.file_column.file_class(data))

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
    """A list of files bound to a model instance."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.files!r})"

    @property
    def files(self) -> list[F]:
        return cast("list[F]", [self.file_column.file_class(item) for item in (self._raw or [])])

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
            data = await self.file_column.store(item, filename, content_type)
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
        return cast("list[F]", [self.file_column.file_class(item) for item in stored])

    async def replace(
        self,
        source: Source | Sequence[Source],
        filename: str = DEFAULT_FILENAME,
        content_type: str = DEFAULT_CONTENT_TYPE,
    ) -> list[F]:
        """Replace the whole set with one file or a list of files."""
        stored = await self._store_all(source, filename, content_type)
        self._write(stored or None)
        return cast("list[F]", [self.file_column.file_class(item) for item in stored])

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


class FileColumn(BaseFileColumn[BoundFile[File]]):
    """A single file."""

    bound_class: type[BoundFile[File]] = BoundFile


class ImageColumn(_ImageMixin, BaseFileColumn[BoundImage]):
    """A single image; dimensions and the thumbnail are computed on save."""

    bound_class: type[BoundImage] = BoundImage

    def __init__(
        self,
        name_pos: str | TypeEngine[Any] | None = None,
        type_pos: TypeEngine[Any] | None = None,
        /,
        *,
        storage: ObjectStorage,
        upload_folder: str = "",
        max_size: int | None = DEFAULT_MAX_UPLOAD_SIZE,
        accept: str | None = None,
        thumbnail_size: tuple[int, int] | None = None,
        nullable: bool | None = None,
        index: bool | None = None,
        deferred: bool | None = None,
        deferred_group: str | None = None,
        deferred_raiseload: bool | None = None,
        server_default: Any | None = None,
        server_onupdate: Any | None = None,
        comment: str | None = None,
        doc: str | None = None,
        info: dict[Any, Any] | None = None,
        sort_order: int | None = None,
        use_existing_column: bool | None = None,
        quote: bool | None = None,
    ) -> None:
        # Fail while the model is being declared, not on the first upload.
        require_pillow(type(self).__name__)
        super().__init__(
            name_pos,
            type_pos,
            storage=storage,
            upload_folder=upload_folder,
            max_size=max_size,
            accept=accept,
            nullable=nullable,
            index=index,
            deferred=deferred,
            deferred_group=deferred_group,
            deferred_raiseload=deferred_raiseload,
            server_default=server_default,
            server_onupdate=server_onupdate,
            comment=comment,
            doc=doc,
            info=info,
            sort_order=sort_order,
            use_existing_column=use_existing_column,
            quote=quote,
        )
        self.thumbnail_size = thumbnail_size


class FileListColumn(BaseFileColumn[BoundFileList[File]]):
    """A list of files (`multiple=True`)."""

    bound_class: type[BoundFileList[File]] = BoundFileList
    multiple = True


class ImageListColumn(_ImageMixin, BaseFileColumn[BoundImageList]):
    """A list of images."""

    bound_class: type[BoundImageList] = BoundImageList
    multiple = True

    def __init__(
        self,
        name_pos: str | TypeEngine[Any] | None = None,
        type_pos: TypeEngine[Any] | None = None,
        /,
        *,
        storage: ObjectStorage,
        upload_folder: str = "",
        max_size: int | None = DEFAULT_MAX_UPLOAD_SIZE,
        accept: str | None = None,
        thumbnail_size: tuple[int, int] | None = None,
        nullable: bool | None = None,
        index: bool | None = None,
        deferred: bool | None = None,
        deferred_group: str | None = None,
        deferred_raiseload: bool | None = None,
        server_default: Any | None = None,
        server_onupdate: Any | None = None,
        comment: str | None = None,
        doc: str | None = None,
        info: dict[Any, Any] | None = None,
        sort_order: int | None = None,
        use_existing_column: bool | None = None,
        quote: bool | None = None,
    ) -> None:
        require_pillow(type(self).__name__)
        super().__init__(
            name_pos,
            type_pos,
            storage=storage,
            upload_folder=upload_folder,
            max_size=max_size,
            accept=accept,
            nullable=nullable,
            index=index,
            deferred=deferred,
            deferred_group=deferred_group,
            deferred_raiseload=deferred_raiseload,
            server_default=server_default,
            server_onupdate=server_onupdate,
            comment=comment,
            doc=doc,
            info=info,
            sort_order=sort_order,
            use_existing_column=use_existing_column,
            quote=quote,
        )
        self.thumbnail_size = thumbnail_size
