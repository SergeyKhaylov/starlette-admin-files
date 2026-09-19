# starlette-admin-files

File and image columns for [starlette-admin](https://jowilf.github.io/starlette-admin/) and
SQLAlchemy, stored through [obstore](https://developmentseed.org/obstore/) — S3, GCS, Azure,
the local filesystem or memory, behind one interface.

The column keeps a plain JSON dict, exactly the metadata starlette-admin writes there itself, so
the admin's rendering, export and delete paths keep working untouched. On top of that dict, a
model attribute gives application code real objects with `read()`, `url()`, `save()` and
`delete()`.

```python
class Post(Base):
    __tablename__ = "post"

    id: Mapped[int] = mapped_column(primary_key=True)

    _cover: Mapped[dict | None] = file_column("cover")
    cover = ImageAttribute(
        "_cover", storage=storage, upload_folder="covers", thumbnail_size=(200, 200)
    )


class PostView(ModelView):
    fields = ["id", ImageField(**Post.cover.field_params, label="Cover")]
```

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

Requires Python 3.12+. Pillow is only needed for image columns; without it `ImageAttribute` fails
while the model is being declared, with a message saying what to install.

## Quick start

```python
from obstore.store import S3Store
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette_admin import ImageField
from starlette_admin.contrib.sqla import Admin, ModelView

from starlette_admin_files import ImageAttribute, ObjectStorage, file_column

storage = ObjectStorage(name="media", store=S3Store("my-bucket"), prefix="media")


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(default="")

    # Two lines per file: the column that stores the metadata, and the
    # attribute that turns it into an Image and writes it back.
    _photo: Mapped[dict | None] = file_column("photo")
    photo = ImageAttribute("_photo", storage=storage, upload_folder="photos")


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
| `file_column()` / `FileJSON` | The JSON column holding the metadata dict. |
| `FileAttribute` / `ImageAttribute` | The model attribute: parameters, reads, and every write. |
| `FileListAttribute` / `ImageListAttribute` | The same for a list of files. |
| `File` / `Image` | Read-only values: metadata plus `read()`, `url()`, `delete()`. |
| `orphaned_files()` / `uploaded_files()` / `delete_files()` | Material for a cleanup unit of work. |

The admin field points at the **column** (`"_cover"`), application code goes through the
**attribute** (`post.cover`). That is why the column name starts with an underscore and the field
gets an explicit `label`.

## Working with files

Values are immutable, so every change goes through the attribute:

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

`ImageAttribute` records the dimensions and, with `thumbnail_size`, stores a thumbnail next to the
original:

```python
cover = ImageAttribute("_cover", storage=storage, thumbnail_size=(200, 200))

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
`set_transliterator(unidecode)` if you prefer, mind its GPL licence. Call
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
    await delete_files(orphans)
```

`delete_files` never stops at the first failure; it returns the `(file, exception)` pairs it could
not delete. In the admin, the same two lines go into `before_edit`/`after_edit_committed` and
`before_delete`/`after_delete_committed` — see [`examples/fastapi_app.py`](examples/fastapi_app.py).

`orphaned_files` reads previous values from SQLAlchemy's attribute history, which only exists for
loaded attributes. Use `expire_on_commit=False`, or capture the previous file yourself
(`old = post.cover.file`).

## Columns

`file_column()` is a shortcut, not a requirement — this is exactly equivalent:

```python
_cover: Mapped[dict | None] = mapped_column("cover", FileJSON(), nullable=True)
```

It exists so the safe defaults are also the short ones: write validation, and `none_as_null=True`
without which `None` is stored as a JSON null and `where(col.is_(None))` stops matching.

The JSON type underneath is yours to pick:

```python
file_column("cover")  # JSON on every dialect
file_column("cover", JSONB)  # PostgreSQL: indexable, queryable
file_column("cover", JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))
```

A bare `JSONB` does not compile on SQLite or MySQL, so reach for the variant when the same models
also run there — in tests, typically.

## Development

```sh
uv sync
uv run pytest            # 129 tests
uv run ruff check .
uv run ruff format .
uv run pyright
```

## License

MIT — see [LICENSE](LICENSE).
