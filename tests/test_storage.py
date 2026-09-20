"""ObjectStorage: keys, names, headers, URLs, serving."""

from __future__ import annotations

import io
import tempfile
from typing import Any

import pytest
from obstore.store import LocalStore, MemoryStore, S3Store
from starlette.responses import Response
from starlette_admin.storage import secure_filename
from starlette_admin_files import (
    ObjectStorage,
    allow_unicode_filenames,
    ascii_filename,
    set_transliterator,
)

from conftest import FakeRequest, exists, png_bytes, upload

RU_NAME = "договор №7.pdf"


async def test_key_layout(storage: ObjectStorage) -> None:
    info = await storage.save(upload(b"x", "a.txt", "text/plain"), "docs/a.txt")

    prefix, folder, unique, name = info.key.split("/")
    assert (prefix, folder, name) == ("media", "docs", "a.txt")
    assert len(unique) == 8


async def test_keys_never_collide(storage: ObjectStorage) -> None:
    first = await storage.save(upload(b"x", "a.txt", "text/plain"), "a.txt")
    second = await storage.save(upload(b"y", "a.txt", "text/plain"), "a.txt")

    assert first.key != second.key
    assert await storage.read(first.key) == b"x"


async def test_unicode_name_kept_in_metadata_key_is_ascii(storage: ObjectStorage) -> None:
    info = await storage.save(upload(b"x", RU_NAME, "application/pdf"), f"docs/{RU_NAME}")

    # `save` stores the name as given; sanitising is the file column's job.
    assert info.filename == RU_NAME
    assert info.key.isascii()
    assert "dogovor_No7.pdf" in info.key


async def test_prefix_is_not_doubled(storage: ObjectStorage) -> None:
    """Upstream derives the thumbnail path from an already prefixed key."""
    info = await storage.save(upload(), "media/covers/abc/cat.thumb.png")

    assert info.key.startswith("media/covers/")
    assert "media/media" not in info.key


@pytest.mark.parametrize(
    ("dest", "expected_folders"),
    [
        # A folder named after the prefix folds into it; that is the documented
        # price of telling a caller's folder from an already prefixed key.
        ("media/a.txt", ["media"]),
        ("media/sub/a.txt", ["media", "sub"]),
        # Only a whole leading segment counts.
        ("mediafiles/a.txt", ["media", "mediafiles"]),
        ("docs/a.txt", ["media", "docs"]),
    ],
)
async def test_a_folder_named_after_the_prefix_folds_into_it(
    storage: ObjectStorage, dest: str, expected_folders: list[str]
) -> None:
    info = await storage.save(upload(b"x", "a.txt", "text/plain"), dest)

    assert info.key.split("/")[:-2] == expected_folders


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("договор.pdf", "dogovor.pdf"),
        ("München Straße.png", "Munchen_Strasse.png"),
        ("Øresund æble.txt", "Oresund_aeble.txt"),
        ("café.png", "cafe.png"),
        ('a"b.txt', "a_b.txt"),
        ("plain.txt", "plain.txt"),
    ],
)
def test_ascii_filename(name: str, expected: str) -> None:
    assert ascii_filename(name) == expected


def test_ascii_filename_keeps_the_extension_when_the_stem_vanishes() -> None:
    set_transliterator(lambda text: "".join(char for char in text if char.isascii()))

    assert ascii_filename("日本語.png") == "file.png"


def test_custom_transliterator_and_reset() -> None:
    set_transliterator(str.upper)
    assert ascii_filename("abc.txt") == "ABC.TXT"

    set_transliterator(None)
    assert ascii_filename("привет.txt") == "privet.txt"


def test_allow_unicode_filenames_switch() -> None:
    allow_unicode_filenames(False)
    assert secure_filename(RU_NAME) == "7.pdf"

    allow_unicode_filenames(True)
    assert secure_filename(RU_NAME) == "договор_7.pdf"


async def test_content_disposition_is_ascii_with_filename_star(storage: ObjectStorage) -> None:
    info = await storage.save(upload(b"x", RU_NAME, "application/pdf"), RU_NAME)

    header = (await storage.store.get_async(path=info.key)).attributes["Content-Disposition"]
    assert header.isascii()
    assert 'filename="dogovor_No7.pdf"' in header
    assert "filename*=UTF-8''" in header
    # A header value must survive latin-1 encoding, which is what Starlette does.
    Response(b"", headers={"content-disposition": header})


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("image/png", "inline"),
        ("application/pdf", "inline"),
        ("video/mp4", "inline"),
        ("text/plain", "attachment"),
        ("application/zip", "attachment"),
        # Executable in the admin's own origin: never inline. The type comes
        # from the multipart part the client sent, so the parameters and the
        # case are its to choose and must not change the decision.
        ("image/svg+xml", "attachment"),
        ("text/html", "attachment"),
        ("text/html; charset=utf-8", "attachment"),
        ("image/svg+xml; charset=utf-8", "attachment"),
        ("TEXT/HTML", "attachment"),
        ("  text/html  ", "attachment"),
        # Normalisation must not cost the inline types their inlining.
        ("image/png; qs=0.9", "inline"),
    ],
)
async def test_disposition_rules(storage: ObjectStorage, content_type: str, expected: str) -> None:
    info = await storage.save(upload(b"x", "f.bin", content_type), "f.bin")

    header = (await storage.store.get_async(path=info.key)).attributes["Content-Disposition"]
    assert header.startswith(expected)


@pytest.mark.parametrize(
    "content_type",
    [
        "image/svg+xml",
        "text/html",
        # The parameters and the case belong to the client, so the guard
        # has to compare the bare media type and not the raw header value.
        "text/html; charset=utf-8",
        "image/svg+xml; charset=utf-8",
        "TEXT/HTML",
        "  text/html  ",
    ],
)
async def test_forced_inline_still_refuses_dangerous_types(content_type: str) -> None:
    storage = ObjectStorage(
        name=f"forced-{content_type}", store=MemoryStore(), disposition="inline"
    )

    info = await storage.save(upload(b"<svg/>", "x.svg", content_type), "x.svg")

    header = (await storage.store.get_async(path=info.key)).attributes["Content-Disposition"]
    assert header.startswith("attachment")


@pytest.mark.parametrize("disposition", ["inlien", "INLINE", "", "download"])
def test_an_unknown_disposition_is_refused(disposition: str) -> None:
    """A typo would otherwise reach the stored header verbatim."""
    with pytest.raises(ValueError, match=r"disposition must be one of"):
        ObjectStorage(name=f"bad-{disposition}", store=MemoryStore(), disposition=disposition)


@pytest.mark.parametrize("disposition", ["auto", "inline", "attachment"])
def test_the_three_dispositions_are_accepted(disposition: str) -> None:
    storage = ObjectStorage(name=f"ok-{disposition}", store=MemoryStore(), disposition=disposition)

    assert storage.disposition == disposition


async def test_local_store_without_attributes() -> None:
    """LocalStore rejects `put` with attributes; the backend must cope."""
    storage = ObjectStorage(name="local", store=LocalStore(prefix=tempfile.mkdtemp()))

    info = await storage.save(upload(b"local", "f.txt", "text/plain"), "docs/f.txt")

    assert await storage.read(info.key) == b"local"


async def test_delete_is_a_noop_for_a_missing_key(storage: ObjectStorage) -> None:
    assert await storage.delete("media/nothing/here.txt") is None


async def test_read_raises_file_not_found(storage: ObjectStorage) -> None:
    with pytest.raises(FileNotFoundError):
        await storage.read("media/nothing/here.txt")


async def test_url_falls_back_to_the_admin_route(
    storage: ObjectStorage, fake_request: FakeRequest
) -> None:
    url = await storage.url(fake_request, "media/a/b.txt")  # type: ignore[arg-type]

    assert url == "/admin/_files/media/media/a/b.txt"


async def test_url_uses_base_url_and_signs_only_on_demand(fake_request: FakeRequest) -> None:
    store = S3Store(
        "bucket",
        region="eu-central-1",
        access_key_id="AKIAEXAMPLE",  # noqa: S106
        secret_access_key="secret",  # noqa: S106
    )
    storage = ObjectStorage(name="cdn", store=store, base_url="https://cdn.example.com")

    plain = await storage.url(fake_request, "a b/файл.txt")  # type: ignore[arg-type]
    signed = await storage.url(fake_request, "a b/файл.txt", signed=True)  # type: ignore[arg-type]

    assert plain == "https://cdn.example.com/a%20b/%D1%84%D0%B0%D0%B9%D0%BB.txt"
    assert "X-Amz-Signature" in signed


async def test_s3_signs_by_default_and_honours_expires(fake_request: FakeRequest) -> None:
    store = S3Store(
        "bucket",
        region="eu-central-1",
        access_key_id="AKIAEXAMPLE",  # noqa: S106
        secret_access_key="secret",  # noqa: S106
    )
    storage = ObjectStorage(name="s3", store=store)

    url = await storage.url(fake_request, "k", expires=60)  # type: ignore[arg-type]

    assert "X-Amz-Signature" in url
    assert "X-Amz-Expires=60" in url


async def test_serve_streams_the_file(
    storage: ObjectStorage, fake_request: FakeRequest, asgi_body: Any
) -> None:
    info = await storage.save(upload(png_bytes(), "cat.png", "image/png"), "cat.png")

    response = await storage.serve(fake_request, info.key)  # type: ignore[arg-type]
    body = await asgi_body(response)

    assert body == await storage.read(info.key)
    assert response.headers["content-length"] == str(info.size)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("inline")


async def test_serve_downgrades_inline_for_dangerous_types(
    storage: ObjectStorage, fake_request: FakeRequest
) -> None:
    info = await storage.save(upload(b"<svg/>", "x.svg", "image/svg+xml"), "x.svg")

    response = await storage.serve(fake_request, info.key)  # type: ignore[arg-type]

    assert response.headers["content-disposition"].startswith("attachment")


@pytest.mark.parametrize(
    "header",
    [
        "inline",
        'inline; filename="x.html"',
        'INLINE; filename="x.html"',
        'inline ; filename="x.html"',
    ],
)
async def test_serve_downgrades_a_disposition_written_elsewhere(
    fake_request: FakeRequest, header: str
) -> None:
    """The stored header is not necessarily one this package wrote."""
    storage = ObjectStorage(name=f"elsewhere-{header}", store=MemoryStore())
    await storage.store.put_async(
        path="x.html",
        file=io.BytesIO(b"<script>alert(1)</script>"),
        attributes={"Content-Type": "text/html", "Content-Disposition": header},
    )

    response = await storage.serve(fake_request, "x.html")  # type: ignore[arg-type]

    assert response.headers["content-disposition"].startswith("attachment")


async def test_serve_keeps_the_parameters_when_it_downgrades(
    fake_request: FakeRequest,
) -> None:
    storage = ObjectStorage(name="downgrade-params", store=MemoryStore(), disposition="inline")
    info = await storage.save(upload(b"<h1/>", RU_NAME, "text/html; charset=utf-8"), RU_NAME)

    response = await storage.serve(fake_request, info.key)  # type: ignore[arg-type]

    header = response.headers["content-disposition"]
    assert header.startswith("attachment;")
    assert 'filename="dogovor_No7.pdf"' in header
    assert "filename*=UTF-8''" in header


async def test_serve_leaves_an_inline_type_inline(
    storage: ObjectStorage, fake_request: FakeRequest
) -> None:
    info = await storage.save(upload(png_bytes(), "cat.png", "image/png"), "cat.png")

    response = await storage.serve(fake_request, info.key)  # type: ignore[arg-type]

    assert response.headers["content-disposition"].startswith("inline")


async def test_serve_404(storage: ObjectStorage, fake_request: FakeRequest) -> None:
    from starlette.exceptions import HTTPException

    with pytest.raises(HTTPException) as error:
        await storage.serve(fake_request, "media/nothing.txt")  # type: ignore[arg-type]

    assert error.value.status_code == 404


async def test_files_are_not_overwritten_by_thumbnail_names(storage: ObjectStorage) -> None:
    first = await storage.save(upload(), "covers/cat.png")
    second = await storage.save(upload(), f"{first.key.rsplit('.', 1)[0]}.thumb.png")

    assert await exists(storage, first.key)
    assert await exists(storage, second.key)


async def test_serve_redirects_for_signable_stores(fake_request: FakeRequest) -> None:
    store = S3Store(
        "bucket",
        region="eu-central-1",
        access_key_id="AKIAEXAMPLE",  # noqa: S106
        secret_access_key="secret",  # noqa: S106
    )
    storage = ObjectStorage(name="s3-serve", store=store)

    response = await storage.serve(fake_request, "k")  # type: ignore[arg-type]

    assert response.status_code == 307
    assert "X-Amz-Signature" in response.headers["location"]


async def test_size_is_measured_when_the_upload_does_not_know_it(
    storage: ObjectStorage,
) -> None:
    from starlette.datastructures import UploadFile

    sizeless = UploadFile(io.BytesIO(b"12345"), filename="a.txt")

    info = await storage.save(sizeless, "a.txt")

    assert info.size == 5


def test_repr_of_a_storage(storage: ObjectStorage) -> None:
    assert storage.name in repr(storage) or "ObjectStorage" in type(storage).__name__
