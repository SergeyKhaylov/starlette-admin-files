"""What a file column adds to the model: the column it declares, the arguments
it accepts and refuses, and the expression class access hands out.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from obstore.store import MemoryStore
from sqlalchemy import JSON, Column, Integer, MetaData, Table, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    MappedAsDataclass,
    load_only,
    mapped_column,
    registry,
)
from sqlalchemy.orm.attributes import QueryableAttribute
from starlette_admin import FileField
from starlette_admin_files import FileColumn, ImageColumn, ObjectStorage

from conftest import upload


class Base(DeclarativeBase):
    pass


def table_of(model: type[Any]) -> Table:
    """`__table__` is declared as a `FromClause` on a mapped class."""
    return cast("Table", model.__table__)


def none_as_null(column: Any) -> bool:
    return cast("JSON", column.type).none_as_null


class Typed(Base):
    """A model with a concrete type, unlike the `Any` the fixtures hand out.

    It exists so that `uv run pyright` checks the call sites below the way a
    user's code would be checked.
    """

    __tablename__ = "typed"

    id: Mapped[int] = mapped_column(primary_key=True)
    scan = FileColumn(storage=ObjectStorage(name="typed", store=MemoryStore()))


@pytest.fixture(scope="module")
def declared(storage: ObjectStorage) -> type[Any]:
    class Article(Base):
        __tablename__ = "article"

        id: Mapped[int] = mapped_column(primary_key=True)
        attachment = FileColumn(storage=storage, upload_folder="attachments")
        cover = ImageColumn(storage=storage, upload_folder="covers")

    return Article


@pytest.fixture
async def articles(declared: type[Any]) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# --- the declared column -------------------------------------------------


def test_the_column_declares_itself(declared: type[Any]) -> None:
    column = declared.__table__.c.attachment

    assert isinstance(column.type, JSON)
    assert column.type.none_as_null is True
    assert column.nullable is True
    # The plain name stays the descriptor; the column is mapped underneath it.
    assert declared.attachment.key == "_attachment"
    assert declared.attachment.file_column.column == "_attachment"


def test_the_column_points_back_at_the_file_column(declared: type[Any]) -> None:
    """`info` is how a column reached from the table finds its file column."""
    assert declared.__table__.c.cover.info["file_column"] is declared.cover.file_column


@pytest.mark.parametrize(
    ("positional", "db_name", "is_jsonb"),
    [
        ((), "scan", False),
        (("scan_data",), "scan_data", False),
        ((JSONB(none_as_null=True),), "scan", True),
        (("scan_data", JSONB(none_as_null=True)), "scan_data", True),
    ],
    ids=["neither", "name", "type", "both"],
)
def test_the_two_positional_arguments(
    storage: ObjectStorage, positional: tuple, db_name: str, is_jsonb: bool
) -> None:
    """Name, type, both or neither — as `mapped_column` takes them."""

    # Its own base per parameter: the shared one would keep the replaced class
    # in its registry and warn, and a bare JSONB left on the shared metadata
    # would break every later create_all — it does not compile on SQLite.
    class ParamBase(DeclarativeBase):
        pass

    class Positional(ParamBase):
        __tablename__ = "positional"

        id: Mapped[int] = mapped_column(primary_key=True)
        scan = FileColumn(*positional, storage=storage)

    column = table_of(Positional).c[db_name]
    assert isinstance(column.type, JSONB) is is_jsonb
    assert none_as_null(column) is True


def test_two_names_or_two_types_are_refused(storage: ObjectStorage) -> None:
    with pytest.raises(TypeError, match="two column names"):
        FileColumn("a", "b", storage=storage)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="two column types"):
        FileColumn(JSON(none_as_null=True), JSON(none_as_null=True), storage=storage)


@pytest.mark.parametrize(
    ("kwargs", "check"),
    [
        ({"nullable": False}, lambda c, t: c.nullable is False),
        ({"index": True}, lambda c, t: any(i.name == "ix_kw_scan" for i in t.indexes)),
        ({"comment": "the scan"}, lambda c, t: c.comment == "the scan"),
        ({"doc": "docstring"}, lambda c, t: c.doc == "docstring"),
        ({"server_default": "null"}, lambda c, t: c.server_default is not None),
        ({"sort_order": -1}, lambda c, t: list(t.columns)[0].name == "scan"),
        ({"quote": True}, lambda c, t: c.name == "scan"),
        ({"info": {"mine": 1}}, lambda c, t: c.info["mine"] == 1),
        ({"deferred": True}, lambda c, t: True),
        ({"deferred_group": "files"}, lambda c, t: True),
        ({"deferred_raiseload": True}, lambda c, t: True),
        ({"use_existing_column": True}, lambda c, t: True),
    ],
)
def test_each_column_argument_reaches_the_column(
    storage: ObjectStorage, kwargs: dict[str, Any], check: Any
) -> None:
    # See `test_the_two_positional_arguments`: one base per parameter.
    class ParamBase(DeclarativeBase):
        pass

    class Keyword(ParamBase):
        __tablename__ = "kw"

        id: Mapped[int] = mapped_column(primary_key=True)
        scan = FileColumn(storage=storage, **kwargs)

    assert check(table_of(Keyword).c.scan, table_of(Keyword))


def test_a_user_info_keeps_the_back_reference(storage: ObjectStorage) -> None:
    """Passing `info` must not displace the entry the column adds itself."""

    class InfoMerge(Base):
        __tablename__ = "info_merge"

        id: Mapped[int] = mapped_column(primary_key=True)
        scan = FileColumn(storage=storage, info={"mine": 1})

    info = table_of(InfoMerge).c.scan.info
    assert info["mine"] == 1
    assert info["file_column"] is InfoMerge.scan.file_column


def test_unset_arguments_are_not_forwarded(storage: ObjectStorage) -> None:
    """Only what the caller set is passed on, so nothing has to mirror
    SQLAlchemy's own defaults or import their private sentinels.
    """
    assert FileColumn(storage=storage)._column_kwargs == {}
    assert FileColumn(storage=storage, index=True)._column_kwargs == {"index": True}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"unique": True},
        {"default": {}},
        {"insert_default": {}},
        {"onupdate": {}},
        {"primary_key": True},
        {"system": True},
        {"active_history": True},
        {"autoincrement": True},
        {"key": "other"},
        {"name": "other"},
        {"type_": JSON()},
        {"init": False},
        {"repr": False},
        {"compare": False},
        {"kw_only": True},
        {"hash": False},
        {"default_factory": dict},
        {"dataclass_metadata": {}},
    ],
)
def test_the_excluded_column_arguments_are_refused(
    storage: ObjectStorage, kwargs: dict[str, Any]
) -> None:
    """They break the column, write past `save()`, or do nothing at all — see
    `_COLUMN_PARAMS`. A refusal beats accepting them silently.
    """
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        FileColumn(storage=storage, **kwargs)


def test_quote_works_without_an_explicit_name(storage: ObjectStorage) -> None:
    """The name is always forwarded, so `quote` has one to apply to — SQLAlchemy
    refuses it otherwise with `Explicit 'name' is required`.
    """

    class Quoted(Base):
        __tablename__ = "quoted"

        id: Mapped[int] = mapped_column(primary_key=True)
        scan = FileColumn(storage=storage, quote=True)

    assert table_of(Quoted).c.scan.name == "scan"


# --- none_as_null --------------------------------------------------------


def test_a_nullable_column_may_store_json_null(storage: ObjectStorage) -> None:
    """The model's choice: a JSON `null` tells "had a file, cleared" apart from
    "never had one" — see `test_json_null_separates_cleared_from_never_set`.
    """

    class KeepsJsonNull(Base):
        __tablename__ = "keeps_json_null"

        id: Mapped[int] = mapped_column(primary_key=True)
        scan = FileColumn(JSON(), storage=storage)

    assert none_as_null(table_of(KeepsJsonNull).c.scan) is False


@pytest.mark.parametrize(
    "column_type",
    [
        JSON(),
        JSON(none_as_null=True).with_variant(JSONB(), "postgresql"),
    ],
    ids=["plain", "variant"],
)
def test_not_null_with_json_null_is_refused(storage: ObjectStorage, column_type: Any) -> None:
    """NOT NULL would accept exactly the state it was added to forbid: a
    cleared column holds a JSON `null`, and `null` passes NOT NULL.
    """
    with pytest.raises(TypeError, match="passes NOT NULL"):
        FileColumn(column_type, storage=storage, nullable=False)


def test_a_type_without_the_notion_is_left_alone(storage: ObjectStorage) -> None:
    """`getattr(..., False)` would not tell "switched off" from "no such
    notion" apart, and would reject a type that never had the setting.
    """
    from sqlalchemy import Text

    FileColumn(Text(), storage=storage, nullable=False)


async def test_json_null_separates_cleared_from_never_set(storage: ObjectStorage) -> None:
    class Split(Base):
        __tablename__ = "split"

        id: Mapped[int] = mapped_column(primary_key=True)
        scan = FileColumn(JSON(), storage=storage)

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        never, cleared, held = Split(), Split(), Split()
        session.add_all([never, cleared, held])
        await cleared.scan.save(upload(b"x", "a.txt", "text/plain"))
        await held.scan.save(upload(b"x", "b.txt", "text/plain"))
        await cleared.scan.delete()
        await session.commit()

        untouched = (await session.scalars(select(Split.id).where(Split.scan.is_(None)))).all()
        emptied = (await session.scalars(select(Split.id).where(Split.scan == JSON.NULL))).all()
        # The trap that comes with it: a cleared row counts as filled here.
        filled = (await session.scalars(select(Split.id).where(Split.scan.isnot(None)))).all()
    await engine.dispose()

    assert list(untouched) == [never.id]
    assert list(emptied) == [cleared.id]
    assert sorted(filled) == sorted([cleared.id, held.id])


# --- refusals at declaration time ----------------------------------------


def test_an_underscored_attribute_name_is_refused(storage: ObjectStorage) -> None:
    with pytest.raises(TypeError, match="cannot start with an underscore"):

        class Underscored(Base):
            __tablename__ = "underscored"

            id: Mapped[int] = mapped_column(primary_key=True)
            _doc = FileColumn(storage=storage)


def test_a_clashing_column_points_at_existing(storage: ObjectStorage) -> None:
    with pytest.raises(TypeError, match=r"already exists; use FileColumn\.existing"):

        class Clash(Base):
            __tablename__ = "clash"

            id: Mapped[int] = mapped_column(primary_key=True)
            _doc: Mapped[dict | None] = mapped_column("doc", JSON(none_as_null=True))
            doc = FileColumn(storage=storage)


def test_field_params_before_the_column_is_assigned(storage: ObjectStorage) -> None:
    with pytest.raises(RuntimeError, match="no column yet"):
        _ = FileColumn(storage=storage).field_params


# --- existing() ----------------------------------------------------------


def test_existing_attaches_to_a_column_the_model_declares(storage: ObjectStorage) -> None:
    class Explicit(Base):
        __tablename__ = "explicit"

        id: Mapped[int] = mapped_column(primary_key=True)
        _doc: Mapped[dict | None] = mapped_column("doc", JSON(none_as_null=True))
        doc = FileColumn.existing("_doc", storage=storage)

    assert [c.name for c in Explicit.__table__.columns] == ["id", "doc"]
    assert Explicit.doc.field_params["name"] == "_doc"


def test_existing_takes_no_column_arguments(storage: ObjectStorage) -> None:
    """The column is the model's; its shape is settled there."""
    with pytest.raises(TypeError, match="takes no column arguments"):
        FileColumn.existing("_doc", storage=storage, index=True)


def test_existing_serves_a_declarative_class_with_an_explicit_table(
    storage: ObjectStorage,
) -> None:
    """Declarative refuses to add a column to a `__table__` given by hand, so a
    reflected or hand-written schema can only be attached to.
    """
    metadata = MetaData()
    table = Table(
        "legacy_report",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("document", JSON(none_as_null=True)),
    )

    class LegacyBase(DeclarativeBase):
        pass

    class Report(LegacyBase):
        __table__ = table
        _document = table.c.document
        document = FileColumn.existing("_document", storage=storage)

    assert "legacy_report.document" in str(select(Report.document))


def test_existing_serves_an_imperatively_mapped_class(storage: ObjectStorage) -> None:
    metadata = MetaData()
    table = Table(
        "imperative_report",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("document", JSON(none_as_null=True)),
    )

    class Report:
        document = FileColumn.existing("_document", storage=storage)

    registry().map_imperatively(Report, table, properties={"_document": table.c.document})

    assert "imperative_report.document" in str(select(Report.document))


# --- mixins and inheritance ----------------------------------------------


def test_a_mixin_serves_several_models(storage: ObjectStorage) -> None:
    """One file column, one `__set_name__`, a separate column per model."""

    class HasScan:
        scan = FileColumn(storage=storage, upload_folder="scans")

    class Invoice(Base, HasScan):
        __tablename__ = "invoice"
        id: Mapped[int] = mapped_column(primary_key=True)

    class Receipt(Base, HasScan):
        __tablename__ = "receipt"
        id: Mapped[int] = mapped_column(primary_key=True)

    assert Invoice.__table__.c.scan is not Receipt.__table__.c.scan
    assert Invoice.scan.class_ is Invoice
    assert Receipt.scan.class_ is Receipt
    # The mixin is not mapped, so there is no expression to hand out; the file
    # column itself answers instead, and still knows its parameters.
    assert HasScan.scan.field_params["name"] == "_scan"


def test_a_dataclass_model_keeps_the_column_out_of_init(storage: ObjectStorage) -> None:
    class DataBase(MappedAsDataclass, DeclarativeBase):
        pass

    class Ticket(DataBase):
        __tablename__ = "ticket"

        id: Mapped[int] = mapped_column(primary_key=True)
        scan = FileColumn(storage=storage)

    # No annotation, so it is a mapped column but not a dataclass field — which
    # is also why the dataclass arguments are refused: they would do nothing.
    assert Ticket.__table__.c.scan.nullable is True
    assert Ticket(id=1)._scan is None  # type: ignore[attr-defined]


# --- the class-level expression ------------------------------------------


def test_class_access_gives_the_column_expression(declared: type[Any]) -> None:
    assert isinstance(declared.cover.__clause_element__(), QueryableAttribute)
    assert str(select(declared.cover)) == "SELECT article.cover \nFROM article"


def test_comparison_compiles_to_a_comparison(declared: type[Any]) -> None:
    """A `__getattr__`-only proxy would answer this with Python's `==` and
    compile the clause to a constant `false`.
    """
    rendered = str(select(declared.id).where(declared.cover == {"key": "k"}))

    assert "article.cover = " in rendered


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (lambda m: select(m.id).where(m.cover.is_(None)), "article.cover IS NULL"),
        (lambda m: select(m.id).where(m.cover.isnot(None)), "article.cover IS NOT NULL"),
        (lambda m: select(m.id).order_by(m.cover), "ORDER BY article.cover"),
        (lambda m: select(m.cover.label("c")), "article.cover AS c"),
        (lambda m: select(m.id).where(m.cover.in_([None])), "article.cover IN"),
        (lambda m: update(m).values({m.cover: None}), "SET cover="),
        (
            lambda m: select(m.id).where(m.cover["filename"].as_string() == "a.png"),
            "article.cover[",
        ),
        (lambda m: select(m).options(load_only(m.cover)), "SELECT article.id, article.cover"),
    ],
)
def test_the_expression_works_where_a_mapped_attribute_does(
    declared: type[Any], build: Any, expected: str
) -> None:
    assert expected in str(build(declared))


def test_the_expression_works_from_the_right_hand_side(declared: type[Any]) -> None:
    """Reflected operators go through `reverse_operate`, and must keep the
    operands in the order they were written.
    """
    assert "|| article.cover" in str(select(declared.id).where("x" + declared.cover == "y"))


def test_an_uninitialised_handle_does_not_recurse() -> None:
    """`__getattr__` forwards to a slot, so it has to refuse the slots
    themselves; without the guard a half-built handle recurses instead of
    raising.
    """
    from starlette_admin_files.columns import _ColumnExpression

    handle = _ColumnExpression.__new__(_ColumnExpression)

    with pytest.raises(AttributeError):
        handle.file_column  # noqa: B018


async def test_expression_round_trip(
    articles: async_sessionmaker[AsyncSession], declared: type[Any]
) -> None:
    async with articles() as session:
        empty, filled = declared(), declared()
        session.add_all([empty, filled])
        await filled.attachment.save(upload(b"x", "a.txt", "text/plain"))
        await session.commit()

        without = (
            await session.scalars(select(declared.id).where(declared.attachment.is_(None)))
        ).all()
        name = await session.scalar(
            select(declared.attachment["filename"].as_string()).where(declared.id == filled.id)
        )

    assert list(without) == [empty.id]
    assert name == "a.txt"


def test_the_expression_type_checks_at_the_call_site() -> None:
    """Guards the `ColumnExpression` declaration: a type checker has to see
    class access as the mapped attribute, or every query written against it
    starts reporting errors in user code.
    """
    statement = select(Typed).where(Typed.scan.is_(None)).order_by(Typed.scan)
    field = FileField(**Typed.scan.field_params, label="Scan")

    assert "typed.scan IS NULL" in str(statement)
    assert field.name == "_scan"


def test_the_handle_carries_the_field_parameters(declared: type[Any]) -> None:
    params = declared.attachment.field_params

    assert params["name"] == "_attachment"
    assert params["upload_folder"] == "attachments"
    assert isinstance(FileField(**params), FileField)


def test_the_handle_reprs_as_both_halves(declared: type[Any]) -> None:
    assert repr(declared.cover) == f"{declared.cover.file_column!r} -> Article._cover"


# --- migrations and indexes ----------------------------------------------


def test_alembic_renders_the_declared_column(declared: type[Any]) -> None:
    """Autogenerate reads the metadata, not the source, so a column the file
    column declared renders like any other — `none_as_null` included, because a
    type's repr keeps the arguments that differ from the default.
    """
    pytest.importorskip("alembic")
    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    metadata = declared.__table__.metadata
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"target_metadata": metadata})
            migrations = produce_migrations(context, metadata)
            assert migrations.upgrade_ops is not None
            rendered = render_python_code(migrations.upgrade_ops)
    finally:
        # Otherwise the pool hands the sqlite connection to the garbage
        # collector, which raises ResourceWarning in whichever test runs next.
        engine.dispose()

    assert "sa.Column('attachment', sa.JSON(none_as_null=True), nullable=True)" in rendered
    assert "sa.Column('cover', sa.JSON(none_as_null=True), nullable=True)" in rendered


def test_an_index_on_a_json_path_is_portable(storage: ObjectStorage) -> None:
    """`index=True` puts a btree on the whole document, which PostgreSQL only
    accepts for JSONB and MySQL not at all. An index on a path compiles to each
    dialect's own syntax from one declaration, and needs no JSONB.
    """
    from sqlalchemy import Index, create_engine
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateIndex

    class Indexed(Base):
        __tablename__ = "indexed"

        id: Mapped[int] = mapped_column(primary_key=True)
        document = FileColumn(storage=storage)

    index = Index("ix_indexed_document_filename", Indexed.document["filename"].as_string())
    try:
        assert index in table_of(Indexed).indexes
        assert "JSON_EXTRACT" in str(CreateIndex(index).compile(dialect=sqlite.dialect()))
        assert "->>" in str(CreateIndex(index).compile(dialect=postgresql.dialect()))

        # And SQLite really uses it, rather than just accepting the DDL.
        engine = create_engine("sqlite://")
        try:
            table_of(Indexed).create(engine)
            with engine.connect() as connection:
                # The path has to be spelled the way the index spells it:
                # SQLAlchemy emits `$."filename"`, and a hand-written
                # `$.filename` is a different expression to SQLite.
                plan = connection.exec_driver_sql(
                    "explain query plan select id from indexed "
                    "where json_extract(document, '$.\"filename\"') = 'a.png'"
                ).fetchall()
            assert "ix_indexed_document_filename" in plan[0][-1]
        finally:
            engine.dispose()
    finally:
        Base.metadata.remove(table_of(Indexed))


def test_alembic_skips_an_index_on_a_json_path(storage: ObjectStorage) -> None:
    """Documented alongside the recipe above, because the gap is silent: the
    index is created by `create_all` in tests and missing in a migrated
    database unless `op.create_index` is written by hand.
    """
    pytest.importorskip("alembic")
    from alembic.autogenerate import produce_migrations, render_python_code
    from alembic.migration import MigrationContext
    from sqlalchemy import Index, create_engine

    class SkipBase(DeclarativeBase):
        pass

    class Skipped(SkipBase):
        __tablename__ = "skipped"

        id: Mapped[int] = mapped_column(primary_key=True)
        document = FileColumn(storage=storage)

    Index("ix_skipped_document_filename", Skipped.document["filename"].as_string())

    engine = create_engine("sqlite://")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"target_metadata": SkipBase.metadata}
            )
            # The warning is the only announcement alembic makes, so assert it
            # rather than tolerate it: the gap must not become silent.
            with pytest.warns(UserWarning, match="autogenerate skipping"):
                migrations = produce_migrations(context, SkipBase.metadata)
            assert migrations.upgrade_ops is not None
            rendered = render_python_code(migrations.upgrade_ops)
    finally:
        engine.dispose()

    assert "create_table" in rendered
    assert "ix_skipped_document_filename" not in rendered
