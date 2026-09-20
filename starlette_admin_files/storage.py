"""An obstore-backed storage backend for starlette-admin."""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import uuid4

import starlette_admin.storage.base as sa_storage
from anyascii import anyascii
from obstore import sign_async
from obstore.store import (
    AzureStore,
    GCSStore,
    HTTPStore,
    LocalStore,
    MemoryStore,
    S3Store,
)
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response, StreamingResponse
from starlette_admin.storage import BaseStorage, FileInfo

__all__ = [
    "ObjectStorage",
    "ObjectStore",
    "allow_unicode_filenames",
    "ascii_filename",
    "set_transliterator",
]

logger = logging.getLogger(__name__)

type ObjectStore = AzureStore | GCSStore | S3Store | LocalStore | HTTPStore | MemoryStore

#: Stores that can produce pre-signed URLs.
_SIGNABLE = (AzureStore, GCSStore, S3Store)

#: `LocalStore` raises `NotImplementedError` for `put` with attributes.
_NO_ATTRIBUTES = (LocalStore,)

#: Types a browser may display instead of downloading. The list is deliberately
#: narrow: `serve()` returns files from the admin's own origin, so anything the
#: browser can execute (HTML, SVG, XML) must download rather than open.
_INLINE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "image/tiff",
        "application/pdf",
    }
)
_INLINE_PREFIXES = ("video/", "audio/")

#: What `ObjectStorage(disposition=...)` accepts. A typo would otherwise reach
#: the stored header verbatim and only show up in a browser.
_DISPOSITIONS = frozenset({"auto", "inline", "attachment"})

#: Never inline, even when the object's own metadata says so.
_NEVER_INLINE = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "application/xml",
        "text/xml",
        "application/xhtml",
    }
)

#: starlette-admin's rule: everything outside [A-Za-z0-9_.-] collapses to "_",
#: which turns a Cyrillic name into "____.pdf".
_ASCII_FILENAME_RE = sa_storage._FILENAME_SANITIZE_RE
#: Ours: `\w` with the UNICODE flag keeps letters of any script.
_UNICODE_FILENAME_RE = re.compile(r"[^\w.-]+", re.UNICODE)

#: Transliteration for object keys. `anyascii` (ISC) covers every script and is
#: a hard dependency rather than an optional one detected at import: keys are
#: persisted, so they must not depend on what happens to be installed.
#: `Unidecode` does the same job under GPL-2.0-or-later, so this package does
#: not depend on it — opt in yourself with `set_transliterator(unidecode)` if
#: your application's license allows it.
_transliterate: Callable[[str], str] = anyascii


def set_transliterator(func: Callable[[str], str] | None) -> None:
    """Replace the transliterator; `None` restores `anyascii`.

    It affects the ASCII name inside object keys and the `filename` parameter
    of `Content-Disposition`. The Unicode name kept in the metadata is
    untouched.

    Avoid switching it against a live database: keys already stored stay as
    they are, while new ones start following different rules.
    """
    global _transliterate
    _transliterate = func if func is not None else anyascii


def _to_ascii(text: str) -> str:
    """Transliterate, then apply starlette-admin's rule to what is left.

    The NFKD pass and the non-ASCII drop are a safety net for custom functions
    passed to `set_transliterator`: `anyascii` already returns pure ASCII, but
    someone else's function may return anything.
    """
    text = unicodedata.normalize("NFKD", _transliterate(text))
    text = text.encode("ascii", "ignore").decode("ascii")
    return _ASCII_FILENAME_RE.sub("_", text).strip("._")


def _ascii_path(path: str) -> str:
    """Transliterate every path segment; object keys must be pure ASCII."""
    segments = [_to_ascii(segment) for segment in path.strip("/").split("/")]
    return "/".join(segment for segment in segments if segment)


def ascii_filename(name: str) -> str:
    """Build the ASCII name used in object keys and in the `filename` parameter.

    The extension is handled separately: otherwise a name made entirely of
    non-ASCII characters ("日本.pdf") would collapse into "pdf", turning the
    extension into the name.
    """
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    stem = _to_ascii(stem) or "file"
    extension = _to_ascii(extension)
    return f"{stem}.{extension}" if extension else stem


def allow_unicode_filenames(enabled: bool = True) -> None:
    """Allow Unicode in uploaded file names.

    `starlette_admin.storage.secure_filename` collapses everything outside
    ``[A-Za-z0-9_.-]`` into ``_``, so "договор.pdf" becomes "pdf". The switch
    patches a private global of starlette-admin.

    Importing this package turns it on — that is the behaviour it exists to
    provide, and the admin field does its own sanitising through the same
    global, so nothing else could reach it in time. The patch is process-wide:
    it applies to every `FileField` in the application, including ones backed
    by another storage. Call ``allow_unicode_filenames(False)`` to restore the
    upstream behaviour everywhere.
    """
    sa_storage._FILENAME_SANITIZE_RE = _UNICODE_FILENAME_RE if enabled else _ASCII_FILENAME_RE


# On by default: the name stays Unicode all the way from the form to the stored
# metadata — it is what the admin UI shows and what `Content-Disposition:
# filename*` carries. Only what must be ASCII becomes ASCII: the object key and
# the `filename` parameter. See `allow_unicode_filenames` for the scope of the
# patch and how to turn it off.
allow_unicode_filenames()


def _content_disposition(filename: str, disposition: str) -> str:
    """Build a Content-Disposition header that survives non-ASCII names.

    A header value must be ASCII (RFC 9110; in practice `filename` tops out at
    latin-1), otherwise obstore and S3 reject the request. So the original name
    goes into ``filename*`` as percent-encoded UTF-8, while ``filename`` keeps
    the transliteration for clients that do not understand ``filename*``. When
    both are present, clients must prefer ``filename*`` (RFC 6266 §4.3).
    """
    ascii_name = ascii_filename(filename)
    header = f'{disposition}; filename="{ascii_name}"'
    if ascii_name != filename:
        header += f"; filename*=UTF-8''{quote(filename, safe='')}"
    return header


def _media_type(content_type: str) -> str:
    """The bare media type: parameters dropped, lower-cased.

    Every comparison against `_NEVER_INLINE` and friends goes through this.
    A `Content-Type` arrives from the multipart part the client sent, so
    "text/html" and "text/html; charset=utf-8" must not be told apart.
    """
    return content_type.split(";", 1)[0].strip().lower()


def _is_inline(content_type: str) -> bool:
    media_type = _media_type(content_type)
    if media_type in _NEVER_INLINE:
        return False
    return media_type in _INLINE_TYPES or media_type.startswith(_INLINE_PREFIXES)


def _as_attachment(header: str) -> str:
    """Swap the disposition type for `attachment`, keeping the parameters.

    The stored header is not necessarily one this package wrote: it may say
    `inline` with no parameters at all, or in a different case. Rebuilding it
    is what makes the downgrade in `serve()` hold for any of those.
    """
    _, semicolon, parameters = header.partition(";")
    return f"attachment{semicolon}{parameters}"


def _upload_size(upload: UploadFile) -> int:
    if upload.size is not None:
        return upload.size
    position = upload.file.tell()
    try:
        upload.file.seek(0, os.SEEK_END)
        return upload.file.tell()
    finally:
        upload.file.seek(position)


class ObjectStorage(BaseStorage):
    """A starlette-admin storage backend on top of any obstore `ObjectStore`.

    Parameters:
        name: Registry name; it is recorded in every file's metadata.
        store: A configured obstore store (S3/GCS/Azure/Local/Memory/HTTP).
        prefix: Key prefix inside the bucket, acting like a folder. The name
            is reserved: an `upload_folder` equal to it, or starting with it,
            is folded into it rather than nested under it — see
            `_strip_prefix`. With ``prefix="media"``, an ``upload_folder`` of
            ``"media"`` gives ``media/<uuid>/name`` and not
            ``media/media/<uuid>/name``. Pick a prefix your folders do not
            use, or leave the prefix out and put it in every folder instead.
        base_url: Public base URL (a CDN or a public bucket). When set, `url()`
            returns a direct link and only signs when asked to. Without it,
            S3/GCS/Azure always sign and other stores are served through the
            admin's own route.
        expires: Lifetime of pre-signed URLs, in seconds.
        disposition: ``"auto"`` picks ``inline`` for images, video, audio and
            PDF and ``attachment`` for everything else; pass ``"inline"`` or
            ``"attachment"`` to force one. Anything else is a `ValueError`.

    Raises:
        ValueError: `disposition` is not one of ``"auto"``, ``"inline"`` or
            ``"attachment"``.
    """

    def __init__(
        self,
        name: str,
        store: ObjectStore,
        prefix: str = "",
        *,
        base_url: str | None = None,
        expires: int = 3600,
        disposition: str = "auto",
    ) -> None:
        if disposition not in _DISPOSITIONS:
            raise ValueError(
                f"disposition must be one of {sorted(_DISPOSITIONS)}, got {disposition!r}"
            )
        self.store = store
        self.prefix = _ascii_path(prefix)
        self.base_url = base_url.rstrip("/") if base_url else None
        self.expires = expires
        self.disposition = disposition
        self.supports_attributes = not isinstance(store, _NO_ATTRIBUTES)
        super().__init__(name)

    def _strip_prefix(self, folder: str) -> str:
        """Drop the storage prefix when `dest` already carries it.

        `ImageField._save_thumbnail` derives the thumbnail path from
        `FileInfo.key` — an already prefixed key — and hands it back to
        `save()`. Without this the prefix would double: ``media/media/...``.

        The two cases are indistinguishable from `dest` alone, so the rule is
        positional and a folder that happens to match the prefix collapses
        with it. With ``prefix="media"``:

            "media/a.txt"      ->  media/<uuid>/a.txt        (not media/media/…)
            "media/sub/a.txt"  ->  media/sub/<uuid>/a.txt    (not media/media/sub/…)
            "mediafiles/a.txt" ->  media/mediafiles/<uuid>/a.txt
            "docs/a.txt"       ->  media/docs/<uuid>/a.txt

        Only a whole leading segment counts ("mediafiles" is left alone), and
        nothing is lost: the files land one level up, in the prefix itself.
        """
        folder = folder.strip("/")
        if self.prefix and (folder == self.prefix or folder.startswith(f"{self.prefix}/")):
            folder = folder[len(self.prefix) :].strip("/")
        return folder

    def _key(self, dest: str) -> str:
        """Build ``{prefix}/{folder}/{uuid}/{filename}``.

        The random segment is always added: `BaseStorage.save` must never
        overwrite an existing file, and a check-then-write pair would still
        race.
        """
        folder, _, name = dest.rpartition("/")
        parts = [
            self.prefix,
            _ascii_path(self._strip_prefix(folder)),
            uuid4().hex[:8],
            ascii_filename(name or "file"),
        ]
        return "/".join(part for part in parts if part)

    async def save(self, upload: UploadFile, dest: str) -> FileInfo:
        key = self._key(dest)
        # Metadata keeps the name as the user sent it (that is what the admin
        # displays); the key keeps only its ASCII transliteration.
        filename = dest.rsplit("/", 1)[-1] or "file"
        content_type = (
            upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        size = _upload_size(upload)
        upload.file.seek(0)
        await self.store.put_async(
            path=key,
            file=upload.file,
            attributes=self._attributes(filename, content_type),
        )
        logger.debug("saved %r (%d bytes) to storage %r", key, size, self.name)
        return FileInfo(
            filename=filename,
            content_type=content_type,
            size=size,
            storage=self.name,
            key=key,
            url="",
            uploaded_at=datetime.now(UTC),
        )

    def _attributes(self, filename: str, content_type: str) -> dict[str, str] | None:
        if not self.supports_attributes:
            return None
        disposition = self.disposition
        if disposition == "inline" and _media_type(content_type) in _NEVER_INLINE:
            disposition = "attachment"
        elif disposition == "auto":
            disposition = "inline" if _is_inline(content_type) else "attachment"
        return {
            "Content-Type": content_type,
            "Content-Disposition": _content_disposition(filename, disposition),
        }

    async def delete(self, key: str) -> None:
        try:
            await self.store.delete_async(paths=key)
        except FileNotFoundError:
            # BaseStorage.delete must not fail when the file is already gone.
            logger.debug("delete no-op: %r not found in storage %r", key, self.name)

    async def read(self, key: str) -> bytes:
        result = await self.store.get_async(path=key)
        return (await result.bytes_async()).to_bytes()

    async def url(
        self,
        request: Request,
        key: str,
        *,
        signed: bool = False,
        expires: int | None = None,
    ) -> str:
        if self.base_url and not signed:
            return f"{self.base_url}/{quote(key, safe='/')}"
        if isinstance(self.store, _SIGNABLE):
            return await sign_async(
                store=self.store,
                method="GET",
                paths=key,
                expires_in=timedelta(seconds=self.expires if expires is None else expires),
            )
        if self.base_url:
            return f"{self.base_url}/{quote(key, safe='/')}"
        route_name = request.app.state.ROUTE_NAME
        return str(request.url_for(f"{route_name}:file", storage=self.name, path=key))

    async def serve(self, request: Request, key: str) -> Response:
        if isinstance(self.store, _SIGNABLE) or self.base_url:
            return RedirectResponse(await self.url(request, key, signed=True))
        try:
            result = await self.store.get_async(path=key)
        except FileNotFoundError as err:
            raise HTTPException(404) from err
        attributes = result.attributes
        media_type = attributes.get("Content-Type") or (
            mimetypes.guess_type(key)[0] or "application/octet-stream"
        )
        headers = {
            "content-length": str(result.meta["size"]),
            # Files are served from the admin's own origin: never let the
            # browser sniff a type and execute the content.
            "x-content-type-options": "nosniff",
        }
        disposition = attributes.get("Content-Disposition")
        if disposition and not _is_inline(media_type):
            disposition = _as_attachment(disposition)
        if disposition:
            headers["content-disposition"] = disposition
        return StreamingResponse(result.stream(), media_type=media_type, headers=headers)
