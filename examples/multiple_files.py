"""Working with a list column: append, replace, drop by index.

uv run python -m examples.multiple_files
"""

import asyncio
import io

from obstore.store import MemoryStore
from PIL import Image as PILImage
from sqlalchemy import JSON
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette_admin_files import ImageListAttribute, ObjectStorage

storage = ObjectStorage(name="media", store=MemoryStore(), prefix="media")


class Base(DeclarativeBase):
    pass


class Gallery(Base):
    __tablename__ = "gallery"

    id: Mapped[int] = mapped_column(primary_key=True)
    _shots: Mapped[list | None] = mapped_column("shots", JSON(none_as_null=True))
    shots = ImageListAttribute(
        "_shots", storage=storage, upload_folder="shots", thumbnail_size=(100, 100)
    )


engine = create_async_engine("sqlite+aiosqlite://")
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


def png(color: str) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (300, 200), color).save(buffer, format="PNG")
    return buffer.getvalue()


async def main() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        gallery = Gallery()
        session.add(gallery)

        # One item or a list: a single source is wrapped internally.
        await gallery.shots.save(png("red"), "red.png", "image/png")
        await gallery.shots.save(
            [
                {"content": png("green"), "filename": "green.png", "content_type": "image/png"},
                {"content": png("blue"), "filename": "blue.png", "content_type": "image/png"},
            ]
        )
        print("after save:", [shot.filename for shot in gallery.shots])

        # The bound attribute behaves like a sequence.
        print("count:", len(gallery.shots))
        print("first:", gallery.shots[0].filename, gallery.shots[0].thumbnail["key"])
        print("bytes of the first:", len(await gallery.shots[0].read()))

        # Drop by index or by a list of indexes; remove=False keeps the files
        # in the storage and only detaches them from the row.
        dropped = await gallery.shots.delete([0, 2])
        print("dropped:", [shot.filename for shot in dropped])
        print("left:", [shot.filename for shot in gallery.shots])

        # replace() swaps the whole set.
        await gallery.shots.replace(png("black"), "black.png", "image/png")
        print("after replace:", [shot.filename for shot in gallery.shots])

        # delete() with no arguments clears the column.
        await gallery.shots.delete()
        print("after delete():", list(gallery.shots), bool(gallery.shots))

        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
