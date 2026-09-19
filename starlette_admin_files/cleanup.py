"""Cleaning up files around a transaction.

The package never deletes on its own: a file stays in the storage until
something removes it explicitly. What lives here are the two selections a unit
of work is built from:

    orphans = orphaned_files(session)      # what a successful commit orphans
    try:
        await session.commit()
    except Exception:
        await delete_files(uploaded_files(session))   # no commit: drop the uploads
        raise
    await delete_files(orphans)                       # committed: drop the old files

`orphaned_files` reads previous values from SQLAlchemy's attribute history,
which is only available for loaded attributes. With `expire_on_commit=True` the
values expire after a commit and the previous file will not be picked up;
`active_history=True` is not an option either, since it raises `MissingGreenlet`
under asyncio. So either use `expire_on_commit=False`, or capture the previous
file yourself: ``old = post.cover.file``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import inspect as sa_inspect

from .attributes import UPLOADED_KEY, BaseFileAttribute
from .file import File

__all__ = ["delete_files", "orphaned_files", "uploaded_files"]

logger = logging.getLogger(__name__)


def _sync_session(session: Any) -> Any:
    """An `AsyncSession` keeps the real session in `sync_session`."""
    return getattr(session, "sync_session", session)


def _file_attributes(model: type) -> list[BaseFileAttribute]:
    found: dict[str, BaseFileAttribute] = {}
    for klass in reversed(model.__mro__):
        for name, value in vars(klass).items():
            if isinstance(value, BaseFileAttribute):
                found[name] = value
    return list(found.values())


def _as_files(value: Any, attribute: BaseFileAttribute) -> list[File]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    return [attribute.file_class(item) for item in items if isinstance(item, dict)]


def uploaded_files(session: Any, *, clear: bool = True) -> list[File]:
    """Files uploaded through model attributes in this session.

    Use them to clean up when a transaction never reaches its commit.
    """
    sync = _sync_session(session)
    raw = sync.info.pop(UPLOADED_KEY, []) if clear else sync.info.get(UPLOADED_KEY, [])
    return [File(item) for item in raw]


def orphaned_files(session: Any) -> list[File]:
    """Files nothing will reference once the session commits.

    These are the previous values of modified file columns, plus every file of
    the rows marked for deletion.
    """
    sync = _sync_session(session)
    orphans: list[File] = []

    for obj in sync.dirty:
        state = sa_inspect(obj)
        for attribute in _file_attributes(type(obj)):
            attr_state = state.attrs.get(attribute.column)
            if attr_state is None:
                continue
            history = attr_state.history
            if not history.deleted:
                continue
            current = {file.key for file in _as_files(attr_state.value, attribute)}
            for old in history.deleted:
                orphans.extend(
                    file
                    for file in _as_files(old, attribute)
                    if file.key and file.key not in current
                )

    for obj in sync.deleted:
        for attribute in _file_attributes(type(obj)):
            orphans.extend(_as_files(getattr(obj, attribute.column, None), attribute))

    return orphans


async def delete_files(files: list[File]) -> list[tuple[File, Exception]]:
    """Delete files without stopping at the first failure.

    Returns the (file, exception) pairs that could not be deleted — when the
    application has no permission to delete from the bucket, for instance.
    """
    failures: list[tuple[File, Exception]] = []
    for file in files:
        try:
            await file.delete()
        except Exception as error:  # noqa: BLE001 - cleanup must not break the request
            logger.warning("failed to delete %r: %s", file.key, error)
            failures.append((file, error))
    return failures
