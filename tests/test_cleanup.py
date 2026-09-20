"""Cleanup helpers: what a commit orphans, what a rollback leaves behind."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette_admin_files import (
    File,
    ObjectStorage,
    delete_files,
    orphaned_files,
    uploaded_files,
)

from conftest import exists, upload


async def test_replacing_a_file_orphans_the_previous_one(
    session: AsyncSession, model: type[Any], storage: ObjectStorage
) -> None:
    post = model()
    session.add(post)
    first = await post.attachment.save(b"first", "first.txt", "text/plain")
    await session.commit()

    await post.attachment.save(b"second", "second.txt", "text/plain")
    orphans = orphaned_files(session)

    assert [file.key for file in orphans] == [first.key]

    await session.commit()
    await delete_files(orphans)
    assert not await exists(storage, first.key)
    assert await exists(storage, post.attachment.file.key)


async def test_orphans_include_the_thumbnail(
    session: AsyncSession, model: type[Any], storage: ObjectStorage
) -> None:
    post = model()
    session.add(post)
    first = await post.cover.save(upload())
    await session.commit()

    await post.cover.save(upload())
    await delete_files(orphaned_files(session))

    assert not await exists(storage, first.key)
    assert not await exists(storage, first.thumbnail["key"])


async def test_deleting_a_row_orphans_every_file(
    session: AsyncSession, model: type[Any], storage: ObjectStorage
) -> None:
    post = model()
    session.add(post)
    attachment = await post.attachment.save(b"x", "a.txt", "text/plain")
    shots = await post.shots.save([upload(filename="one.png"), upload(filename="two.png")])
    await session.commit()

    await session.delete(post)
    orphans = orphaned_files(session)

    expected = {attachment.key, *(shot.key for shot in shots)}
    assert {file.key for file in orphans} == expected

    await session.commit()
    await delete_files(orphans)
    for key in expected:
        assert not await exists(storage, key)


async def test_an_unchanged_session_orphans_nothing(
    session: AsyncSession, model: type[Any]
) -> None:
    post = model()
    session.add(post)
    await post.attachment.save(b"x", "a.txt", "text/plain")
    await session.commit()

    assert orphaned_files(session) == []


async def test_uploaded_files_track_the_current_transaction(
    session: AsyncSession, model: type[Any], storage: ObjectStorage
) -> None:
    post = model()
    session.add(post)
    saved = await post.attachment.save(b"x", "a.txt", "text/plain")

    pending = uploaded_files(session, clear=False)
    assert [file.key for file in pending] == [saved.key]

    await session.rollback()
    await delete_files(uploaded_files(session))

    assert not await exists(storage, saved.key)


async def test_uploaded_files_clears_by_default(session: AsyncSession, model: type[Any]) -> None:
    post = model()
    session.add(post)
    await post.attachment.save(b"x", "a.txt", "text/plain")

    assert len(uploaded_files(session)) == 1
    assert uploaded_files(session) == []


async def test_a_commit_does_not_clear_the_uploads(session: AsyncSession, model: type[Any]) -> None:
    """Which is why post-commit cleanup belongs in `else` and not in the `try`.

    Under the handler, anything failing after a successful commit would hand
    `delete_files(uploaded_files(session))` the files that commit just made
    live.
    """
    post = model()
    session.add(post)
    saved = await post.attachment.save(b"x", "a.txt", "text/plain")
    await session.commit()

    assert [file.key for file in uploaded_files(session)] == [saved.key]


async def test_delete_files_reports_failures_instead_of_raising(
    storage: ObjectStorage,
) -> None:
    class Boom(File):
        __slots__ = ()

        async def delete(self) -> None:
            raise PermissionError("no delete permission on the bucket")

    good = File.from_info(await storage.save(upload(b"x", "a.txt", "text/plain"), "a.txt"))
    bad = Boom(good.to_dict())

    failures = await delete_files([bad, good])

    assert len(failures) == 1
    assert isinstance(failures[0][1], PermissionError)
    assert not await exists(storage, good.key)


async def test_expired_attributes_hide_the_previous_value(engine: Any, model: type[Any]) -> None:
    """The documented limitation: history needs loaded attributes."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(engine, expire_on_commit=True)
    async with maker() as session:
        post = model()
        session.add(post)
        await post.attachment.save(b"first", "first.txt", "text/plain")
        await session.commit()  # attributes expire here

        await post.attachment.save(b"second", "second.txt", "text/plain")

        assert orphaned_files(session) == []


@pytest.mark.parametrize("clear", [True, False])
async def test_uploaded_files_on_a_session_without_uploads(
    session: AsyncSession, clear: bool
) -> None:
    assert uploaded_files(session, clear=clear) == []
