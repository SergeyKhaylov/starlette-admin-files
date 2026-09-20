"""Storing a document the backend produced itself.

`save()` takes bytes, a stream, a dict or an UploadFile, so a generated report
never has to be wrapped into a fake upload.

    uv run python -m examples.generated_documents
"""

import asyncio
import io

from obstore.store import MemoryStore
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette_admin_files import (
    FileColumn,
    ObjectStorage,
    delete_files,
    uploaded_files,
)

storage = ObjectStorage(name="media", store=MemoryStore(), prefix="media")


class Base(DeclarativeBase):
    pass


class Report(Base):
    __tablename__ = "report"

    id: Mapped[int] = mapped_column(primary_key=True)
    document = FileColumn(storage=storage, upload_folder="reports")


engine = create_async_engine("sqlite+aiosqlite://")
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def build_pdf(title: str) -> bytes:
    """Stand-in for a real report generator."""
    return f"%PDF-1.4\n% {title}\n".encode()


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        report = Report()
        session.add(report)

        # Bytes are enough. Pass content_type explicitly: a generated file has
        # no upload to infer it from, and the default is application/octet-stream
        # (which also makes the browser download instead of display it).
        document = await report.document.save(
            build_pdf("September"),
            filename="September report.pdf",
            content_type="application/pdf",
        )
        print("stored:", document.key, document.size, "bytes")
        print("display name:", document.filename)

        await session.commit()

    # A stream and a dict work the same way.
    async with SessionLocal() as session:
        report = Report()
        session.add(report)
        await report.document.save(
            io.BytesIO(build_pdf("October")),
            filename="October report.pdf",
            content_type="application/pdf",
        )
        await report.document.save(
            {
                "content": build_pdf("November"),
                "filename": "November report.pdf",
                "content_type": "application/pdf",
            }
        )
        # The second save replaced the first one, so the October file is now
        # unreferenced. Left in the storage on purpose: see cleanup_unit_of_work.
        await session.commit()

    # Uploading and then failing: remove what this transaction wrote.
    async with SessionLocal() as session:
        try:
            report = Report()
            session.add(report)
            await report.document.save(build_pdf("Broken"), "broken.pdf", "application/pdf")
            raise RuntimeError("something went wrong before the commit")
        except RuntimeError as error:
            await session.rollback()
            await delete_files(uploaded_files(session))
            print("rolled back and cleaned up:", error)


if __name__ == "__main__":
    asyncio.run(main())
