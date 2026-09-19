"""A FastAPI app with an admin and REST routes over the same file columns.

    uv run uvicorn examples.fastapi_app:app --reload  ->  http://127.0.0.1:8000/admin

The shape: the column stores a plain JSON dict, the admin field points at the
column (`_attachment`), and application code goes through the model attribute
(`post.attachment`). The session dependency shows the cleanup unit of work.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, UploadFile
from fastapi import File as UploadedFile
from obstore.store import MemoryStore
from sqlalchemy import JSON, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette_admin import FileField, ImageField
from starlette_admin.contrib.sqla import Admin, ModelView
from starlette_admin_files import (
    FileAttribute,
    ImageAttribute,
    ImageListAttribute,
    ObjectStorage,
    delete_files,
    orphaned_files,
    uploaded_files,
)

# Any obstore store: S3Store / GCSStore / AzureStore / LocalStore / MemoryStore.
storage = ObjectStorage(name="media", store=MemoryStore(), prefix="media")


class Base(DeclarativeBase):
    pass


class Post(Base):
    __tablename__ = "post"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(default="")

    # The columns hold dicts; their names in the database are
    # "attachment" / "cover" / "shots".
    _attachment: Mapped[dict | None] = mapped_column("attachment", JSON(none_as_null=True))
    _cover: Mapped[dict | None] = mapped_column("cover", JSON(none_as_null=True))
    _shots: Mapped[list | None] = mapped_column("shots", JSON(none_as_null=True))

    attachment = FileAttribute(
        "_attachment",
        storage=storage,
        upload_folder="attachments",
        max_size=10 * 1024 * 1024,
    )
    cover = ImageAttribute(
        "_cover",
        storage=storage,
        upload_folder="covers",
        thumbnail_size=(200, 200),
    )
    shots = ImageListAttribute("_shots", storage=storage, upload_folder="shots")


class PostView(ModelView):
    # The admin field works with the column, hence the leading underscore and
    # the explicit label: the UI would otherwise show "_attachment".
    fields = [
        "id",
        "title",
        FileField(**Post.attachment.field_params, label="Attachment", exclude_from_list=True),
        ImageField(**Post.cover.field_params, label="Cover"),
        ImageField(**Post.shots.field_params, label="Screenshots", exclude_from_list=True),
    ]

    # starlette-admin never removes files, neither on replace nor on delete.
    # Collect what the change orphans before the commit, delete it after.
    async def before_edit(self, request, data: dict, obj: Post) -> None:
        request.state.orphans = orphaned_files(request.state.session)

    async def after_edit_committed(self, request, obj: Post) -> None:
        await delete_files(getattr(request.state, "orphans", []))

    async def before_delete(self, request, obj: Post) -> None:
        request.state.orphans = orphaned_files(request.state.session)

    async def after_delete_committed(self, request, obj: Post) -> None:
        await delete_files(getattr(request.state, "orphans", []))


engine = create_async_engine("sqlite+aiosqlite://")
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


async def get_db() -> AsyncIterator[AsyncSession]:
    """A unit of work: commit, then clean up.

    The package deletes nothing by itself. After a successful commit we remove
    the files nothing references any more; after a failure, the ones this
    transaction had already uploaded.
    """
    async with SessionLocal() as session:
        try:
            yield session
            orphans = orphaned_files(session)
            await session.commit()
        except Exception:
            await session.rollback()
            await delete_files(uploaded_files(session))
            raise
        await delete_files(orphans)


DBSession = Annotated[AsyncSession, Depends(get_db)]

app = FastAPI(lifespan=lifespan)
admin = Admin(engine, title="Files example", secret_key="change-me")  # noqa: S106
admin.add_view(PostView(Post))
admin.mount_to(app)


@app.post("/posts")
async def create_post(
    db: DBSession,
    attachment: Annotated[UploadFile, UploadedFile(...)],
    cover: Annotated[UploadFile, UploadedFile(...)],
    shots: Annotated[list[UploadFile], UploadedFile(...)],
    title: str = "",
) -> dict[str, Any]:
    post = Post(title=title)
    db.add(post)
    # save() returns the stored File/Image, so nothing has to be read back.
    saved_attachment = await post.attachment.save(attachment)
    saved_cover = await post.cover.save(cover)
    saved_shots = await post.shots.save(shots)
    await db.flush()
    return {
        "id": post.id,
        "attachment": saved_attachment.key,
        "cover": saved_cover.key,
        "thumbnail": saved_cover.thumbnail.get("key"),
        "shots": [shot.key for shot in saved_shots],
    }


@app.get("/posts")
async def list_posts(db: DBSession) -> list[dict[str, Any]]:
    posts = []
    for post in (await db.scalars(select(Post))).all():
        attachment = post.attachment.file
        cover = post.cover.image
        posts.append(
            {
                "id": post.id,
                "title": post.title,
                "attachment": attachment.filename if attachment else None,
                "cover": cover.key if cover else None,
                "shots": [shot.key for shot in post.shots],
            }
        )
    return posts
