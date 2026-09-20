# starlette-admin-files

File and image columns for [starlette-admin](https://jowilf.github.io/starlette-admin/) and
SQLAlchemy, stored through [obstore](https://developmentseed.org/obstore/) — S3, GCS, Azure,
the local filesystem or memory, behind one interface.

The column keeps a plain JSON dict, exactly the metadata starlette-admin writes there itself, so
the admin's rendering, export and delete paths keep working untouched. On top of that dict, a
file column gives application code real objects with `read()`, `url()`, `save()` and
`delete()`.

```python
class Post(Base):
    __tablename__ = "post"

    id: Mapped[int] = mapped_column(primary_key=True)

    cover = ImageColumn(storage=storage, upload_folder="covers", thumbnail_size=(200, 200))


class PostView(ModelView):
    fields = ["id", ImageField(**Post.cover.field_params, label="Cover")]
```

`ImageColumn` declares the column it needs — a `JSON(none_as_null=True)` named `cover`, mapped as
`Post._cover` — so the model states once what it means. Its first two positional arguments are the
column's name and type, exactly as `mapped_column` takes them; see [Columns](#columns).

```python
await post.cover.save(upload)  # an UploadFile, bytes, a stream or a dict
if post.cover.image is not None:
    data = await post.cover.image.read()
```

## Installation

```sh
pip install starlette-admin-files          # files only
pip install "starlette-admin-files[image]" # + Pillow, for images and thumbnails
```

Requires Python 3.12+. Pillow is only needed for image columns; without it `ImageColumn` fails
while the model is being declared, with a message saying what to install.

## Quick start

```python
from obstore.store import S3Store
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette_admin import ImageField
from starlette_admin.contrib.sqla import Admin, ModelView

from starlette_admin_files import ImageColumn, ObjectStorage

storage = ObjectStorage(name="media", store=S3Store("my-bucket"), prefix="media")


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(default="")

    # One line per file: the file column adds the column that stores the
    # metadata and turns it into an Image on the way out.
    photo = ImageColumn(storage=storage, upload_folder="photos")


class ProductView(ModelView):
    # field_params carries the column name, the storage and the limits, so the
    # form and the backend can never disagree.
    fields = ["id", "name", ImageField(**Product.photo.field_params, label="Photo")]


admin = Admin(engine, title="Shop")
admin.add_view(ProductView(Product))
admin.mount_to(app)
```

More in [`examples/`](examples/): a minimal admin, a FastAPI app with REST routes, generated
documents, list columns, cleanup, and storage configuration.

## How it fits together

| Piece | Role |
| --- | --- |
| `ObjectStorage` | A starlette-admin storage backend over any obstore store. |
| A `JSON` column | Holds the metadata dict. Plain SQLAlchemy — see [Columns](#columns). |
| `FileColumn` / `ImageColumn` | The model's file column: the column it declares, the parameters, reads, and every write. |
| `FileListColumn` / `ImageListColumn` | The same for a list of files. |
| `File` / `Image` | Read-only values: metadata plus `read()`, `url()`, `delete()`. |
| `orphaned_files()` / `uploaded_files()` / `delete_files()` | Material for a cleanup unit of work. |

The admin field points at the **mapped column** (`"_cover"`), application code goes through the
**file column** (`post.cover`). That is why the mapped column carries the underscore and the field
gets an explicit `label`.

`Post.cover` — the file column on the class — is that column as an expression, so queries read the
way they would against any mapped attribute, and `field_params` and the file column itself stay
reachable through it:

```python
select(Post).where(Post.cover.is_(None))
select(Post).where(Post.cover["filename"].as_string().endswith(".png"))

Post.cover.field_params            # -> the arguments for ImageField
Post.cover.file_column.storage     # -> the ObjectStorage
```

## Working with files

Values are immutable, so every change goes through the file column:

```python
saved = await post.attachment.save(upload)  # UploadFile
saved = await post.attachment.save(pdf_bytes, "report.pdf", "application/pdf")
saved = await post.attachment.save(io.BytesIO(data), "report.pdf", "application/pdf")
saved = await post.attachment.save(
    {"content": data, "filename": "report.pdf", "content_type": "application/pdf"}
)
```

`save()` returns the stored `File`, so nothing has to be read back. Pass `content_type`
explicitly for generated files: there is no upload to infer it from, and the fallback
`application/octet-stream` also makes browsers download instead of display.

```python
file = post.attachment.file  # File | None
if post.attachment:  # same check, shorter
    data = await file.read()
    href = await file.url(request)  # signed or public, never the URL from the database
await post.attachment.delete()  # clears the column and removes the file
await post.attachment.delete(remove=False)  # only detaches it from the row
```

Lists take one item or several, and drop by index:

```python
await post.shots.save(upload)  # append
await post.shots.save([first, second])  # append several
await post.shots.replace(upload)  # swap the whole set
await post.shots.delete([0, 2])  # drop by index, or delete() for all

len(post.shots), post.shots[0].filename, [shot.key for shot in post.shots]
```

`max_size` and `accept` are checked by the same validators the admin form uses, so a file
rejected by the form is rejected in code too, with the same message.

## Images

`ImageColumn` records the dimensions and, with `thumbnail_size`, stores a thumbnail next to the
original:

```python
cover = ImageColumn(storage=storage, thumbnail_size=(200, 200))

image = await post.cover.save(upload)
image.width, image.height, image.thumbnail["key"]
await image.thumbnail_url(request)
```

Aspect ratio is preserved, images are never upscaled, the source format is kept when it can be
written back, and a failure to build the thumbnail never fails the upload.

## Storage

```python
ObjectStorage(name="media", store=MemoryStore())  # tests
ObjectStorage(name="media", store=LocalStore(prefix="uploads", mkdir=True))
ObjectStorage(name="media", store=S3Store("bucket"), prefix="media", expires=900)
ObjectStorage(name="media", store=S3Store("bucket"), base_url="https://cdn.example.com")
```

S3, GCS and Azure produce pre-signed URLs; with `base_url` they produce plain links and sign only
when asked (`url(request, signed=True)`). Everything else is served through the admin's own
`/_files` route.

Files served from that route carry `X-Content-Type-Options: nosniff`, and anything a browser could
execute — SVG, HTML, XML — is always sent as an attachment, because the admin serves it from its
own origin.

`LocalStore` does not support object attributes, so `Content-Type` and `Content-Disposition` are
not stored alongside local files; `serve()` falls back to guessing from the key.

`disposition` takes `"auto"` (the default), `"inline"` or `"attachment"`; anything else is a
`ValueError` at construction, rather than a broken header noticed in a browser.

The `prefix` name is reserved as a folder name. A key is built as
`{prefix}/{upload_folder}/{uuid}/{filename}`, but an `upload_folder` equal to the prefix — or
starting with it — folds into it instead of nesting under it, because a thumbnail comes back from
starlette-admin as an already prefixed key and the two cases cannot be told apart:

```python
ObjectStorage(name="media", store=S3Store("bucket"), prefix="media")

FileColumn(storage=storage, upload_folder="media")      # media/<uuid>/a.txt
FileColumn(storage=storage, upload_folder="media/sub")  # media/sub/<uuid>/a.txt
FileColumn(storage=storage, upload_folder="mediafiles") # media/mediafiles/<uuid>/a.txt
FileColumn(storage=storage, upload_folder="docs")       # media/docs/<uuid>/a.txt
```

Nothing is lost — the files land one level up — but pick a prefix your folders do not use, or
leave the prefix out and put it in every folder instead.

## File names

The name the user sent stays Unicode all the way into the metadata — it is what the admin shows —
while the object key is transliterated to ASCII:

```
договор №7.pdf     ->  media/docs/ab12cd34/dogovor_No7.pdf
München Straße.png ->  media/docs/cd34ef56/Munchen_Strasse.png
```

`Content-Disposition` carries both: the transliteration in `filename` and the original in
`filename*` as percent-encoded UTF-8 (RFC 6266), because a header value cannot hold non-ASCII.

Transliteration uses [anyascii](https://github.com/anyascii/anyascii) (ISC). Swap it with
`set_transliterator(unidecode)` if you prefer, mind its GPL licence.

Unicode names are on from the moment the package is imported: starlette-admin sanitises through a
private global, and patching it is the only place the behaviour can come from. The patch is
process-wide, so it also applies to `FileField`s backed by another storage. Call
`allow_unicode_filenames(False)` to restore starlette-admin's ASCII-only names everywhere.

## Cleaning up

The package never deletes on its own: a file lives in the storage until something removes it.
That keeps deletion under your control — a bucket may not even grant it. Two selections make a
unit of work:

```python
async with SessionLocal() as session:
    try:
        yield session
        orphans = orphaned_files(session)  # what a successful commit orphans
        await session.commit()
    except Exception:
        await session.rollback()
        await delete_files(uploaded_files(session))  # no commit: drop the uploads
        raise
    else:
        await delete_files(orphans)
```

The `else` is load-bearing. Inside the `try` that last call would be covered by the handler above,
and anything failing after the commit would delete the files the commit just made live; in a
`finally` it would run after a rollback, when the rows still reference them.

`delete_files` never stops at the first failure; it returns the `(file, exception)` pairs it could
not delete. In the admin, the same two lines go into `before_edit`/`after_edit_committed` and
`before_delete`/`after_delete_committed` — see [`examples/fastapi_app.py`](examples/fastapi_app.py).

`orphaned_files` reads previous values from SQLAlchemy's attribute history, which only exists for
loaded attributes. Use `expire_on_commit=False`, or capture the previous file yourself
(`old = post.cover.file`).

## Columns

A file column declares the mapped column it reads and writes. Its first two positional arguments
are that column's name and type, exactly as `mapped_column` takes them — either may be left out:

```python
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

class Post(Base):
    cover = ImageColumn(storage=storage)                    # "cover", JSON(none_as_null=True)
    scan = FileColumn(JSONB(none_as_null=True), storage=storage)          # same name, JSONB
    shots = ImageListColumn("shot_metadata", storage=storage)             # a name of its own
```

The name of the attribute (`cover`) stays the descriptor; the column is mapped under `_cover`, and
in the database it is called `cover` unless you say otherwise.

### What the column accepts

Thirteen of `mapped_column`'s keyword arguments come through: `nullable`, `index`, `deferred`,
`deferred_group`, `deferred_raiseload`, `server_default`, `server_onupdate`, `comment`, `doc`,
`info`, `sort_order`, `use_existing_column` and `quote`.

The rest are refused rather than accepted quietly, in four groups:

| | Why |
| --- | --- |
| `primary_key`, `system`, `active_history` | Break the column: the first two make it unwritable, the third raises `MissingGreenlet` under asyncio when the previous value has to be fetched. |
| `default`, `insert_default`, `onupdate` | Write a file value past `save()`, leaving metadata in the row for a file nothing ever uploaded. |
| `init`, `repr`, `compare`, `kw_only`, `hash`, `default_factory`, `dataclass_metadata`, `autoincrement`, `key` | Do nothing here. The declared column carries no annotation, so it is not a dataclass field; `key` renames only how `table.c` addresses it. |
| `name`, `type_`, `unique` | The first two duplicate the positional arguments; uniqueness is already satisfied by every upload getting its own key. |

`deferred` is the one worth knowing about: it keeps the metadata out of every query that does not
ask for it.

### Indexes

`index=True` puts a btree on the whole document. That works on SQLite, on PostgreSQL only with
`JSONB` — the `json` type has no default operator class — and on MySQL not at all, where a JSON
column cannot be indexed directly.

An index on a path is usually what you want anyway, and it is portable: one declaration compiles
to each dialect's own syntax, and it needs no `JSONB`.

```python
Index("ix_post_cover_filename", Post.cover["filename"].as_string())
```
```sql
-- sqlite     CREATE INDEX ... ON post (JSON_EXTRACT(cover, '$."filename"'))
-- postgresql CREATE INDEX ... ON post ((CAST(cover ->> 'filename' AS VARCHAR)))
```

Alembic does not carry these into a migration — see [Migrations](#migrations).

### A column the model declares

When the column cannot come from here, `existing()` attaches to one the model declares: a
declarative class with an explicit `__table__` (a reflected or hand-written schema), an
imperatively mapped class, or a column something else maps too.

```python
class Report(Base):
    __table__ = legacy_table
    _document = legacy_table.c.document
    document = FileColumn.existing("_document", storage=storage)
```

The argument is the name of the *mapped attribute*, not of the column in the database. `existing()`
takes no column arguments — the column's shape is settled where it is declared.

### `none_as_null`

**`none_as_null=True` is the default, and what the file column supplies when you pass no type.** A
row that never had a file holds SQL NULL, but clearing one — `delete()`, `replace()` with nothing,
dropping the last item of a list — writes Python `None`, and SQLAlchemy stores that as a JSON
`null` unless the type says otherwise. Both read back as `None`, so application code sees no
difference; SQL does:

```python
await post.cover.delete()  # with none_as_null=False the column holds 'null', not NULL

select(Post).where(Post.cover.is_(None))  # ...so this row is missing from the result
```

Keeping the JSON `null` is a legitimate choice, and it buys something real: it tells "had a file,
cleared" apart from "never had one".

```python
scan = FileColumn(JSON(), storage=storage)          # none_as_null=False

select(Post).where(Post.scan.is_(None))             # never had a file
select(Post).where(Post.scan == JSON.NULL)          # had one, cleared
select(Post).where(Post.scan.isnot(None))           # careful: counts the cleared ones too
```

That last line is the trap that comes with it — "rows with a file" needs
`isnot(None) & (Post.scan != JSON.NULL)`. Python is unaffected either way: `post.scan.file` is
`None` in both.

One combination is refused: **`nullable=False` together with `none_as_null=False`**. A cleared
column would hold a JSON `null`, `null` passes NOT NULL, and the constraint would accept exactly
the state it was added to forbid.

The JSON type underneath is yours to pick:

```python
JSON(none_as_null=True)   # JSON on every dialect
JSONB(none_as_null=True)  # PostgreSQL: indexable, queryable

# Both, from the same model:
JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
```

A bare `JSONB` does not compile on SQLite or MySQL, so reach for the variant when the same models
also run there — in tests, typically.

Nothing else is needed: every write goes through the file column, which refuses anything that is
not a file dict (`filename`, `content_type`, `size`, `storage`, and a non-empty `key`).

## Migrations

Autogenerate reads the metadata, not the model source, so a column a `FileColumn` declared is no
different from one you wrote out — as long as `env.py` imports the models, which is what puts those
columns on the metadata in the first place:

```python
from myapp import models  # noqa: F401  — the columns appear as the classes are defined

target_metadata = models.Base.metadata
```

`none_as_null=True` survives into the migration, because a type's `repr` keeps the arguments that
differ from the default:

```python
sa.Column("attachment", sa.JSON(none_as_null=True), nullable=True)
```

It also never causes a spurious diff on later runs: it changes how values are bound, not the DDL,
so `compare_type=True` sees nothing to alter. The other side of that is that switching it in the
model produces no migration either; rows already holding a JSON `null` stay that way until a
one-off `UPDATE ... SET col = NULL WHERE col = 'null'`.

**Indexes on a JSON path do not survive autogenerate.** Alembic skips expression-based indexes —
it cannot reflect them, so it cannot compare them — with a warning and nothing in the migration.
The gap is quiet: the index is there in tests, where the schema comes from `create_all`, and
missing in a migrated database. Write it by hand:

```python
op.create_index("ix_post_cover_filename", "post",
                [sa.text("json_extract(cover, '$.\"filename\"')")])
```

`JSONB` needs one fix, and it is Alembic's, not this package's: the inner type renders unprefixed
(`astext_type=Text()`) and the migration fails with `NameError`. A `render_item` hook settles it:

```python
from sqlalchemy import JSON
from sqlalchemy.dialects import postgresql


def _render_json(type_, autogen_context):
    if isinstance(type_, postgresql.JSONB):
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return f"postgresql.JSONB(none_as_null={type_.none_as_null}, astext_type=sa.Text())"
    if isinstance(type_, JSON):
        return f"sa.JSON(none_as_null={type_.none_as_null})"
    return None


def render_item(obj_type, obj, autogen_context):
    if obj_type != "type":
        return False
    rendered = _render_json(obj, autogen_context)
    if rendered is None:
        return False  # every other type goes to the default renderer
    for dialect, variant in obj._variant_mapping.items():
        rendered_variant = _render_json(variant, autogen_context)
        if rendered_variant is None:
            return False
        rendered += f".with_variant({rendered_variant}, {dialect!r})"
    return rendered


context.configure(connection=connection, target_metadata=target_metadata,
                  render_item=render_item)
```

## Development

```sh
uv sync
uv run pytest            # 227 tests
uv run ruff check .
uv run ruff format .
uv run pyright
```

## License

MIT — see [LICENSE](LICENSE).
