"""The FileJSON column type: what it accepts, what it refuses, how NULL works."""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette_admin.storage import FileInfo
from starlette_admin_files import File, FileJSON

from conftest import upload

VALID = {
    "filename": "a.txt",
    "content_type": "text/plain",
    "size": 1,
    "storage": "media",
    "key": "media/docs/ab12cd34/a.txt",
}


def test_accepts_a_dict_a_file_and_a_file_info() -> None:
    column = FileJSON()
    info = FileInfo(**VALID)  # type: ignore[arg-type]

    assert column.process_bind_param(dict(VALID), None) == VALID  # type: ignore[arg-type]
    assert column.process_bind_param(File(VALID), None)["key"] == VALID["key"]  # type: ignore[arg-type,index]
    assert column.process_bind_param(info, None)["key"] == VALID["key"]  # type: ignore[arg-type,index]


def test_accepts_a_list() -> None:
    bound = FileJSON().process_bind_param([File(VALID), dict(VALID)], None)  # type: ignore[arg-type]

    assert isinstance(bound, list)
    assert len(bound) == 2


def test_none_passes_through() -> None:
    assert FileJSON().process_bind_param(None, None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (42, "not a file value"),
        ({"filename": "a.txt"}, "missing keys"),
        ({**VALID, "key": ""}, "empty key"),
    ],
)
def test_refuses_broken_values(value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        FileJSON().process_bind_param(value, None)  # type: ignore[arg-type]


async def test_none_is_stored_as_sql_null(session: AsyncSession, model: type[Any]) -> None:
    """With a plain JSON column `None` would become a JSON null instead."""
    empty = model()
    filled = model()
    session.add_all([empty, filled])
    await filled.attachment.save(upload(b"x", "a.txt", "text/plain"))
    await session.commit()

    found = (await session.scalars(select(model).where(model._attachment.is_(None)))).all()

    assert [row.id for row in found] == [empty.id]


async def test_round_trip_keeps_every_key(session: AsyncSession, model: type[Any]) -> None:
    post = model()
    session.add(post)
    saved = await post.cover.save(upload())
    await session.commit()
    session.expunge_all()

    reloaded = await session.get(model, post.id)
    assert reloaded is not None

    assert reloaded._cover == saved.to_dict()
    assert reloaded.cover.image is not None
    assert reloaded.cover.image.thumbnail["key"] == saved.thumbnail["key"]


def ddl_for(column_type: Any, dialect: Any) -> str:
    from sqlalchemy import Column, Integer, MetaData, Table
    from sqlalchemy.schema import CreateTable

    table = Table(
        "t", MetaData(), Column("id", Integer, primary_key=True), Column("cover", column_type)
    )
    statement = CreateTable(table).compile(dialect=dialect).string
    return next(line.strip().rstrip(",") for line in statement.splitlines() if "cover" in line)


@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite", "mysql"])
def test_json_by_default_everywhere(dialect_name: str) -> None:
    import importlib

    dialect = importlib.import_module(f"sqlalchemy.dialects.{dialect_name}").dialect()

    assert ddl_for(FileJSON(), dialect) == "cover JSON"


def test_an_explicit_type_is_used() -> None:
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects.postgresql import JSONB

    assert ddl_for(FileJSON(JSONB), postgresql.dialect()) == "cover JSONB"


def test_a_dialect_specific_type_does_not_compile_elsewhere() -> None:
    """The cost of being explicit; `with_variant` is the portable form."""
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.exc import CompileError

    with pytest.raises(CompileError):
        ddl_for(FileJSON(JSONB), sqlite.dialect())


def test_a_variant_covers_both() -> None:
    from sqlalchemy import JSON
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.dialects.postgresql import JSONB

    variant = FileJSON(JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))

    assert ddl_for(variant, postgresql.dialect()) == "cover JSONB"
    assert ddl_for(variant, sqlite.dialect()) == "cover JSON"


def test_a_type_class_gets_none_as_null() -> None:
    from sqlalchemy import JSON
    from sqlalchemy.dialects.postgresql import JSONB

    impl = cast("JSON", FileJSON(JSONB).impl)

    assert impl.none_as_null is True


def test_a_type_instance_is_used_as_configured() -> None:
    from sqlalchemy import JSON

    configured = JSON(none_as_null=False)

    assert FileJSON(configured).impl is configured


def test_the_type_is_part_of_the_cache_key() -> None:
    from sqlalchemy.dialects.postgresql import JSONB

    assert FileJSON()._static_cache_key != FileJSON(JSONB)._static_cache_key


async def test_file_column_is_equivalent_to_mapped_column(model: type[Any]) -> None:
    """`file_column` is a shortcut, not a requirement."""
    column = model.__table__.c.cover

    assert isinstance(column.type, FileJSON)
    assert column.nullable is True


def test_a_type_that_does_not_take_none_as_null() -> None:
    """Not every JSON-shaped type has the flag; it must still be usable."""
    from sqlalchemy import JSON

    class PlainJSON(JSON):
        def __init__(self) -> None:
            super().__init__()

    assert isinstance(FileJSON(PlainJSON).impl, PlainJSON)
