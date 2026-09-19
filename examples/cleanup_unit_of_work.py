"""Who deletes the files, and when.

The package removes nothing on its own: a file lives in the storage until the
application deletes it. `orphaned_files` and `uploaded_files` give a unit of
work the two lists it needs.

    uv run python -m examples.cleanup_unit_of_work
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from obstore.store import MemoryStore
from sqlalchemy import JSON, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette_admin_files import (
    FileAttribute,
    ObjectStorage,
    delete_files,
    orphaned_files,
    uploaded_files,
)

storage = ObjectStorage(name="media", store=MemoryStore(), prefix="media")


class Base(DeclarativeBase):
    pass


class Contract(Base):
    __tablename__ = "contract"

    id: Mapped[int] = mapped_column(primary_key=True)
    _scan: Mapped[dict | None] = mapped_column("scan", JSON(none_as_null=True))
    scan = FileAttribute("_scan", storage=storage, upload_folder="scans")


engine = create_async_engine("sqlite+aiosqlite://")
# expire_on_commit=False matters: `orphaned_files` reads the previous value
# from SQLAlchemy's attribute history, which expired attributes do not keep.
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def unit_of_work() -> AsyncGenerator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            orphans = orphaned_files(session)
            await session.commit()
        except Exception:
            await session.rollback()
            await delete_files(uploaded_files(session))
            raise
        # delete_files never stops at the first failure; it returns the pairs
        # it could not delete, so a bucket without delete permission does not
        # break the request.
        failures = await delete_files(orphans)
        for file, error in failures:
            print("could not delete", file.key, error)


async def exists(key: str) -> bool:
    try:
        await storage.read(key)
    except FileNotFoundError:
        return False
    return True


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with unit_of_work() as session:
        contract = Contract()
        session.add(contract)
        first = await contract.scan.save(b"first", "first.txt", "text/plain")
    first_key = first.key

    # Replacing a file: the old one is orphaned by the commit.
    async with unit_of_work() as session:
        contract = (await session.scalars(select(Contract))).one()
        second = await contract.scan.save(b"second", "second.txt", "text/plain")
        second_key = second.key
    print("after replace: old exists =", await exists(first_key))
    print("               new exists =", await exists(second_key))

    # Deleting a row: every file it holds is orphaned.
    async with unit_of_work() as session:
        contract = (await session.scalars(select(Contract))).one()
        await session.delete(contract)
    print("after row delete: exists =", await exists(second_key))

    # A failed transaction: the upload is removed instead.
    third_key = ""
    try:
        async with unit_of_work() as session:
            contract = Contract()
            session.add(contract)
            third = await contract.scan.save(b"third", "third.txt", "text/plain")
            third_key = third.key
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    print("after rollback:   exists =", await exists(third_key))


if __name__ == "__main__":
    asyncio.run(main())
