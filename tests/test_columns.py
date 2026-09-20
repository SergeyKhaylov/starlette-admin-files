"""File columns on a model: reading, saving, replacing, deleting, validating."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette_admin import FileField, ImageField
from starlette_admin_files import File, Image, ObjectStorage

from conftest import FakeRequest, exists, png_bytes, upload


@pytest.fixture
async def post(session: AsyncSession, model: type[Any]) -> Any:
    post = model()
    session.add(post)
    await session.flush()
    return post


# --- field parameters ---------------------------------------------------


def test_field_params_carry_everything_the_field_needs(model: type[Any]) -> None:
    params = model.cover.field_params

    assert params["name"] == "_cover"
    assert params["multiple"] is False
    assert params["thumbnail_size"] == (100, 100)
    assert params["accept"] == "image/*"
    assert isinstance(ImageField(**params), ImageField)


def test_field_params_of_a_list_column(model: type[Any]) -> None:
    assert model.shots.field_params["multiple"] is True


def test_max_size_defaults_to_the_upstream_value(model: type[Any]) -> None:
    assert model.cover.field_params["max_size"] == FileField("x").max_size


def test_class_access_carries_the_file_column(model: type[Any]) -> None:
    from starlette_admin_files import ImageColumn

    assert isinstance(model.cover.file_column, ImageColumn)


# --- single file --------------------------------------------------------


async def test_empty_column(post: Any) -> None:
    assert post.attachment.file is None
    assert bool(post.attachment) is False


async def test_save_writes_the_column_and_returns_the_file(post: Any) -> None:
    saved = await post.attachment.save(upload(b"doc", "договор.pdf", "application/pdf"))

    assert isinstance(saved, File)
    assert post._attachment["key"] == saved.key
    assert bool(post.attachment) is True
    assert post.attachment.file == saved


async def test_save_applies_the_upload_folder_and_sanitises_the_name(post: Any) -> None:
    saved = await post.attachment.save(upload(b"doc", "договор №7.pdf", "application/pdf"))

    assert saved.key.startswith("media/attachments/")
    assert saved.filename == "договор_7.pdf"
    assert saved.key.endswith("dogovor_7.pdf")


@pytest.mark.parametrize(
    ("source", "filename", "content_type", "expected_type"),
    [
        (b"bytes", "a.bin", "application/octet-stream", "application/octet-stream"),
        (b"text", "a.txt", "text/plain", "text/plain"),
    ],
)
async def test_save_accepts_raw_sources(
    post: Any, source: bytes, filename: str, content_type: str, expected_type: str
) -> None:
    saved = await post.attachment.save(source, filename, content_type)

    assert saved.content_type == expected_type
    assert await saved.read() == source


async def test_image_column_computes_dimensions_and_thumbnail(post: Any) -> None:
    image = await post.cover.save(upload(png_bytes((400, 300))))

    assert isinstance(image, Image)
    assert (image.width, image.height) == (400, 300)
    assert max(image.thumbnail["width"], image.thumbnail["height"]) == 100
    assert image.thumbnail["key"].isascii()


async def test_image_alias(post: Any, fake_request: FakeRequest) -> None:
    await post.cover.save(upload())

    # Values are immutable and compare by data; only the bound object is cached.
    assert post.cover.image == post.cover.file
    assert post.cover is post.cover
    assert (await post.cover.image.thumbnail_url(fake_request)).endswith(  # type: ignore[arg-type]
        post.cover.image.thumbnail["key"]
    )


async def test_delete_clears_the_column_and_removes_the_file(
    post: Any, storage: ObjectStorage
) -> None:
    saved = await post.attachment.save(b"x", "a.txt", "text/plain")

    removed = await post.attachment.delete()

    assert removed == saved
    assert post._attachment is None
    assert not await exists(storage, saved.key)


async def test_delete_can_keep_the_file(post: Any, storage: ObjectStorage) -> None:
    saved = await post.attachment.save(b"x", "a.txt", "text/plain")

    await post.attachment.delete(remove=False)

    assert post._attachment is None
    assert await exists(storage, saved.key)


async def test_direct_assignment_is_refused(post: Any) -> None:
    with pytest.raises(AttributeError, match="only changes through the file column"):
        post.attachment = None


async def test_the_bound_object_is_cached(post: Any) -> None:
    assert post.attachment is post.attachment


# --- validation ---------------------------------------------------------


async def test_max_size_is_enforced(post: Any) -> None:
    with pytest.raises(ValueError, match="too large"):
        await post.attachment.save(b"x" * (1024 * 1024 + 1), "big.bin", "application/octet-stream")


async def test_accept_is_enforced(post: Any) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        await post.cover.save(b"not an image", "a.txt", "text/plain")


async def test_nothing_is_stored_when_validation_fails(post: Any) -> None:
    with pytest.raises(ValueError, match="not allowed"):
        await post.cover.save(b"not an image", "a.txt", "text/plain")

    assert post._cover is None


# --- list ---------------------------------------------------------------


async def test_list_starts_empty(post: Any) -> None:
    assert list(post.shots) == []
    assert len(post.shots) == 0
    assert bool(post.shots) is False


async def test_save_one_returns_a_list(post: Any) -> None:
    saved = await post.shots.save(upload(filename="one.png"))

    assert len(saved) == 1
    assert len(post.shots) == 1


async def test_save_appends(post: Any) -> None:
    await post.shots.save(upload(filename="one.png"))
    await post.shots.save([upload(filename="two.png"), upload(filename="three.png")])

    assert [shot.filename for shot in post.shots] == ["one.png", "two.png", "three.png"]


async def test_list_indexing_and_slicing(post: Any) -> None:
    await post.shots.save([upload(filename="one.png"), upload(filename="two.png")])

    assert isinstance(post.shots[0], Image)
    assert [shot.filename for shot in post.shots[0:1]] == ["one.png"]
    assert post.shots[-1].filename == "two.png"


async def test_replace_swaps_the_whole_set(post: Any) -> None:
    await post.shots.save([upload(filename="one.png"), upload(filename="two.png")])

    await post.shots.replace(upload(filename="only.png"))

    assert [shot.filename for shot in post.shots] == ["only.png"]


async def test_delete_by_index(post: Any, storage: ObjectStorage) -> None:
    await post.shots.save(
        [upload(filename="one.png"), upload(filename="two.png"), upload(filename="three.png")]
    )
    keys = [shot.key for shot in post.shots]

    removed = await post.shots.delete([0, 2])

    assert [shot.filename for shot in removed] == ["one.png", "three.png"]
    assert [shot.filename for shot in post.shots] == ["two.png"]
    assert not await exists(storage, keys[0])
    assert await exists(storage, keys[1])


async def test_delete_everything(post: Any) -> None:
    await post.shots.save([upload(filename="one.png"), upload(filename="two.png")])

    removed = await post.shots.delete()

    assert len(removed) == 2
    assert post._shots is None


async def test_delete_with_a_bad_index(post: Any) -> None:
    await post.shots.save(upload())

    with pytest.raises(IndexError, match="no file at index 5"):
        await post.shots.delete(5)


async def test_list_column_survives_a_round_trip(
    post: Any, session: AsyncSession, model: type[Any]
) -> None:
    await post.shots.save([upload(filename="one.png"), upload(filename="two.png")])
    await session.commit()
    session.expunge_all()

    reloaded = await session.get(model, post.id)
    assert reloaded is not None

    assert [shot.filename for shot in reloaded.shots] == ["one.png", "two.png"]
    assert all(isinstance(shot, Image) for shot in reloaded.shots)


# --- the column ---------------------------------------------------------


async def test_a_cleared_column_is_sql_null(session: AsyncSession, model: type[Any]) -> None:
    """`delete()` writes `None`; with `none_as_null=False` that would reach the
    database as a JSON `null`, and `is_(None)` would stop matching the row.
    """
    untouched = model()
    cleared = model()
    session.add_all([untouched, cleared])
    await cleared.attachment.save(b"x", "a.txt", "text/plain")
    await cleared.attachment.delete()
    await session.commit()

    found = (await session.scalars(select(model.id).where(model._attachment.is_(None)))).all()

    assert sorted(found) == sorted([untouched.id, cleared.id])


async def test_an_emptied_list_column_is_sql_null(session: AsyncSession, model: type[Any]) -> None:
    post = model()
    session.add(post)
    await post.shots.save([upload(filename="one.png"), upload(filename="two.png")])
    await post.shots.delete()
    await session.commit()

    stored = await session.scalar(
        text("select typeof(shots) from post where id = :id"), {"id": post.id}
    )

    assert stored == "null"  # SQLite for SQL NULL; a JSON null would be 'text'


def test_image_columns_expose_their_parameters(storage: ObjectStorage) -> None:
    """A `*args, **kwargs` wrapper in the MRO would hide them from editors."""
    import inspect

    from starlette_admin_files import ImageColumn, ImageListColumn
    from starlette_admin_files.columns import _COLUMN_PARAMS

    for column_class in (ImageColumn, ImageListColumn):
        parameters = set(inspect.signature(column_class).parameters)
        assert {"storage", "upload_folder", "max_size", "accept", "thumbnail_size"} <= parameters
        assert set(_COLUMN_PARAMS) <= parameters
        assert "kwargs" not in parameters, column_class.__name__


def test_plain_file_columns_take_no_thumbnail_size(storage: ObjectStorage) -> None:
    """It would break `FileField(**field_params)`, which has no such argument."""
    from starlette_admin_files import FileColumn

    with pytest.raises(TypeError, match="thumbnail_size"):
        FileColumn(storage=storage, thumbnail_size=(10, 10))  # type: ignore[call-arg]

    assert FileColumn(storage=storage).thumbnail_size is None
