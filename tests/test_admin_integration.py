"""The paths starlette-admin itself walks: form processing, rendering, export."""

from __future__ import annotations

import os
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette_admin.contrib.sqla import Admin, ModelView
from starlette_admin.storage import delete_stored_files
from starlette_admin.types import RequestAction
from starlette_admin_files import Image, ObjectStorage

from conftest import FakeRequest, exists, upload


def form_data(**overrides: Any) -> dict[str, Any]:
    """What `parse_form_data` hands to `_process_file_fields`."""
    data: dict[str, Any] = {
        "title": "t",
        "_attachment": (None, False),
        "_cover": (None, False),
        "_shots": ([], False),
    }
    data.update(overrides)
    return data


async def test_create_through_the_admin(
    admin: Admin, view: ModelView, request_factory: Any, session: AsyncSession, model: type[Any]
) -> None:
    request = request_factory(RequestAction.CREATE)
    data = form_data(
        _attachment=(upload(b"doc", "договор.pdf", "application/pdf"), False),
        _cover=(upload(), False),
        _shots=([upload(filename="one.png")], False),
    )

    await admin._process_file_fields(request, view, data)
    post = await view.create(request, data)
    await session.commit()

    assert isinstance(data["_cover"], dict)
    assert post._attachment["filename"] == "договор.pdf"
    assert post._cover["key"].isascii()
    assert post._cover["thumbnail"]["key"].isascii()
    assert len(post._shots) == 1


async def test_file_columns_read_what_the_admin_wrote(
    admin: Admin, view: ModelView, request_factory: Any, session: AsyncSession, model: type[Any]
) -> None:
    request = request_factory(RequestAction.CREATE)
    data = form_data(_cover=(upload(), False), _shots=([upload(filename="one.png")], False))
    await admin._process_file_fields(request, view, data)
    post = await view.create(request, data)
    await session.commit()
    session.expunge_all()

    reloaded = await session.get(model, post.id)
    assert reloaded is not None

    assert isinstance(reloaded.cover.image, Image)
    assert reloaded.cover.image.width > 0
    assert [shot.filename for shot in reloaded.shots] == ["one.png"]


async def test_edit_keeps_an_untouched_file(
    admin: Admin, view: ModelView, request_factory: Any, session: AsyncSession
) -> None:
    request = request_factory(RequestAction.CREATE)
    data = form_data(_cover=(upload(), False))
    await admin._process_file_fields(request, view, data)
    post = await view.create(request, data)
    await session.commit()
    key = post._cover["key"]

    edit = form_data()
    await admin._process_file_fields(request_factory(RequestAction.EDIT), view, edit, obj=post)

    assert edit["_cover"]["key"] == key


async def test_edit_with_the_delete_checkbox(
    admin: Admin, view: ModelView, request_factory: Any, session: AsyncSession
) -> None:
    request = request_factory(RequestAction.CREATE)
    data = form_data(_cover=(upload(), False))
    await admin._process_file_fields(request, view, data)
    post = await view.create(request, data)
    await session.commit()

    edit = form_data(_cover=(None, True))
    await admin._process_file_fields(request_factory(RequestAction.EDIT), view, edit, obj=post)

    assert edit["_cover"] is None


async def test_serialize_value_refreshes_urls(
    storage: ObjectStorage, view: ModelView, fake_request: FakeRequest
) -> None:
    from starlette_admin_files.file import save_thumbnail

    info = await storage.save(upload(), "covers/cat.png")
    info = await save_thumbnail(storage, info, upload(), (50, 50))
    field = next(f for f in view.fields if f.name == "_cover")

    serialized = await field.serialize_value(cast("Request", fake_request), info.to_dict())

    assert serialized["url"].endswith(info.key)
    assert info.thumbnail is not None
    assert serialized["thumbnail_url"].endswith(info.thumbnail["key"])
    # The Unicode display name survives all the way to the template data.
    assert serialized["filename"] == info.filename


async def test_templates_render_the_value(
    storage: ObjectStorage, view: ModelView, fake_request: FakeRequest
) -> None:
    import starlette_admin
    from jinja2 import Environment, FileSystemLoader

    info = await storage.save(upload(), "covers/cat.png")
    field = next(f for f in view.fields if f.name == "_cover")
    serialized = await field.serialize_value(cast("Request", fake_request), info.to_dict())

    environment = Environment(  # noqa: S701 - the admin's own templates
        loader=FileSystemLoader(
            os.path.join(os.path.dirname(starlette_admin.__file__), "templates")
        )
    )
    environment.filters.update(safe_url=lambda value: value, file_icon=lambda value: "icon")
    rendered = environment.get_template("fields/detail/image.html").render(
        data=serialized, field=field
    )

    assert f'href="{serialized["url"]}"' in rendered


async def test_delete_stored_files_understands_the_column_value(
    storage: ObjectStorage, session: AsyncSession, model: type[Any]
) -> None:
    post = model()
    session.add(post)
    image = await post.cover.save(upload())

    await delete_stored_files(post._cover)

    assert not await exists(storage, image.key)
    assert not await exists(storage, image.thumbnail["key"])


async def test_file_fields_are_not_searchable_or_sortable(view: ModelView) -> None:
    assert "_cover" not in (view.sortable_fields or [])
    assert "_attachment" not in (view.searchable_fields or [])
