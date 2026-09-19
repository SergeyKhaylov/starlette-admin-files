"""Shared fixtures: an in-memory storage, a model with every kind of column,
an admin, and helpers for building uploads.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import pytest
from obstore.store import MemoryStore
from PIL import Image as PILImage
from sqlalchemy import JSON
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request
from starlette_admin import FileField, ImageField
from starlette_admin.contrib.sqla import Admin, ModelView
from starlette_admin.types import RequestAction
from starlette_admin_files import (
    FileAttribute,
    ImageAttribute,
    ImageListAttribute,
    ObjectStorage,
    allow_unicode_filenames,
    set_transliterator,
)


def png_bytes(size: tuple[int, int] = (400, 300), color: str = "teal", fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", size, color).save(buffer, format=fmt)
    return buffer.getvalue()


def upload(
    data: bytes | None = None,
    filename: str = "cat.png",
    content_type: str = "image/png",
) -> UploadFile:
    data = png_bytes() if data is None else data
    return UploadFile(
        io.BytesIO(data),
        size=len(data),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


class FakeRequest:
    """Enough of a Request for `ObjectStorage.url` and field serialization."""

    class app:  # noqa: N801
        class state:
            ROUTE_NAME = "admin"

    def url_for(self, name: str, **path_params: str) -> str:
        return f"/admin/_files/{path_params['storage']}/{path_params['path']}"


class Base(DeclarativeBase):
    pass


@pytest.fixture(scope="session")
def storage() -> ObjectStorage:
    return ObjectStorage(name="media", store=MemoryStore(), prefix="media")


@pytest.fixture(scope="session")
def model(storage: ObjectStorage) -> type[Any]:
    class Post(Base):
        __tablename__ = "post"

        id: Mapped[int] = mapped_column(primary_key=True)
        title: Mapped[str] = mapped_column(default="")

        # `none_as_null=True` is what the README recommends: a cleared column
        # goes back to SQL NULL instead of holding a JSON `null`.
        _attachment: Mapped[dict | None] = mapped_column("attachment", JSON(none_as_null=True))
        _cover: Mapped[dict | None] = mapped_column("cover", JSON(none_as_null=True))
        _shots: Mapped[list | None] = mapped_column("shots", JSON(none_as_null=True))

        attachment = FileAttribute(
            "_attachment",
            storage=storage,
            upload_folder="attachments",
            max_size=1024 * 1024,
        )
        cover = ImageAttribute(
            "_cover", storage=storage, upload_folder="covers", thumbnail_size=(100, 100)
        )
        shots = ImageListAttribute("_shots", storage=storage, upload_folder="shots")

    return Post


@pytest.fixture
async def engine() -> AsyncIterator[Any]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine: Any) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.fixture
def sessionmaker(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(scope="session")
def view_class(model: type[Any]) -> type[ModelView]:
    class PostView(ModelView):
        fields = [
            "id",
            "title",
            FileField(**model.attachment.field_params, label="Attachment"),
            ImageField(**model.cover.field_params, label="Cover"),
            ImageField(**model.shots.field_params, label="Screenshots"),
        ]

    return PostView


@pytest.fixture
def admin(engine: Any, model: type[Any], view_class: type[ModelView]) -> Admin:
    admin = Admin(engine, title="tests", secret_key="test")  # noqa: S106
    admin.add_view(view_class(model))
    return admin


@pytest.fixture
def view(admin: Admin) -> ModelView:
    return cast("ModelView", admin._views[0])


@pytest.fixture
def request_factory(session: AsyncSession) -> Any:
    def make(action: RequestAction = RequestAction.CREATE) -> Request:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "headers": [],
                "path": "/admin",
                "query_string": b"",
                "app": None,
                "state": {},
            }
        )
        request.state.session = session
        request.state.action = action
        return request

    return make


@pytest.fixture
def fake_request() -> FakeRequest:
    return FakeRequest()


@pytest.fixture
def asgi_body() -> Any:
    """Drive a StreamingResponse through the ASGI protocol and collect the body."""

    async def run(response: Any) -> bytes:
        import asyncio

        messages: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            await asyncio.sleep(3600)  # never disconnect
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"spec_version": "2.4"},
            "method": "GET",
            "headers": [],
        }
        await asyncio.wait_for(response(scope, receive, send), 10)
        return b"".join(message.get("body", b"") for message in messages)

    return run


@pytest.fixture(autouse=True)
def reset_global_switches() -> Iterator[None]:
    """Keep the module-level switches from leaking between tests."""
    yield
    allow_unicode_filenames(True)
    set_transliterator(None)


async def exists(storage: ObjectStorage, key: str) -> bool:
    try:
        await storage.read(key)
    except FileNotFoundError:
        return False
    return True
