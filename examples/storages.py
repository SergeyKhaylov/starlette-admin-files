"""Configuring the storage: backends, URLs, file names.

Nothing here talks to the network except the S3 section, which only builds
pre-signed URLs (signing is local).

    uv run python -m examples.storages
"""

import asyncio
from typing import cast

from obstore.store import LocalStore, MemoryStore, S3Store
from starlette.requests import Request
from starlette_admin_files import ObjectStorage, allow_unicode_filenames, ascii_filename

# --- backends -----------------------------------------------------------

# In memory: handy in tests.
memory = ObjectStorage(name="memory", store=MemoryStore())

# On disk. Note: LocalStore does not support object attributes, so
# Content-Type and Content-Disposition are not stored alongside the file;
# `serve()` falls back to guessing the type from the key.
local = ObjectStorage(name="local", store=LocalStore(prefix="uploads", mkdir=True))

# S3 and friends. `prefix` acts as a folder inside the bucket.
s3 = ObjectStorage(
    name="s3",
    store=S3Store(
        "my-bucket",
        region="eu-central-1",
        access_key_id="AKIAEXAMPLE",  # noqa: S106 - example credentials
        secret_access_key="secret",  # noqa: S106
    ),
    prefix="media",
    expires=900,
)

# A public bucket or a CDN in front of one: plain links, signed only on demand.
cdn = ObjectStorage(
    name="cdn",
    store=S3Store(
        "my-bucket",
        region="eu-central-1",
        access_key_id="AKIAEXAMPLE",  # noqa: S106
        secret_access_key="secret",  # noqa: S106
    ),
    base_url="https://cdn.example.com",
)

# Force every file to download, whatever its type.
downloads = ObjectStorage(name="downloads", store=MemoryStore(), disposition="attachment")


# --- columns ------------------------------------------------------------

# The column is plain JSON unless you say otherwise. On PostgreSQL, JSONB is
# usually the better choice: it is indexable and queryable with expressions
# such as `Post._cover["key"].astext`.
#
#     _cover: Mapped[dict | None] = file_column("cover")          # JSON
#     _cover: Mapped[dict | None] = file_column("cover", JSONB)   # PostgreSQL only
#
# A bare JSONB does not compile on SQLite or MySQL, so use a variant when the
# same models also run there (in tests, typically):
#
#     _cover: Mapped[dict | None] = file_column(
#         "cover", JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
#     )
#
# `file_column` is only a shortcut; this is exactly the same:
#
#     _cover: Mapped[dict | None] = mapped_column("cover", FileJSON(), nullable=True)


class FakeRequest:
    """`url()` needs a request only for storages served through the admin route."""

    class app:  # noqa: N801
        class state:
            ROUTE_NAME = "admin"

    def url_for(self, name: str, **path_params: str) -> str:
        return f"/admin/{path_params['storage']}/{path_params['path']}"


async def main() -> None:
    request = cast("Request", FakeRequest())
    key = "media/covers/ab12cd34/photo.png"

    # S3/GCS/Azure sign by default; other stores go through the admin route.
    print("s3     :", (await s3.url(request, key))[:70], "...")
    print("cdn    :", await cdn.url(request, key))
    print("cdn+sig:", (await cdn.url(request, key, signed=True))[:70], "...")
    print("memory :", await memory.url(request, key))

    # --- file names -----------------------------------------------------

    # Unicode names are kept in the metadata (that is what the admin shows and
    # what Content-Disposition carries) while the object key is transliterated.
    print("\nascii_filename:")
    for name in ["договор №7.pdf", "München Straße.png", "東京タワー.png", "📎 note.txt"]:
        print(f"  {name:22} -> {ascii_filename(name)}")

    # Restore the upstream behaviour if you would rather have ASCII-only names
    # everywhere, including the metadata.
    allow_unicode_filenames(False)
    from starlette_admin.storage import secure_filename

    print("\nwith allow_unicode_filenames(False):", secure_filename("договор.pdf"))
    allow_unicode_filenames(True)
    print("with allow_unicode_filenames(True) :", secure_filename("договор.pdf"))


if __name__ == "__main__":
    asyncio.run(main())
