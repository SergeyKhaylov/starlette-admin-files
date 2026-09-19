"""Checks on what this package assumes about starlette-admin.

Some of those assumptions reach past the public API — a private global, the
call shape of two validators, the exact keys of `FileInfo`. These tests exist so
that an upstream upgrade fails here, loudly and with a clear reason, instead of
silently changing behaviour somewhere in production.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import pytest
import starlette_admin.storage.base as sa_storage_base
from starlette_admin.fields import DEFAULT_MAX_UPLOAD_SIZE, BaseField, FileField, ImageField
from starlette_admin.storage import BaseStorage, FileInfo, get_storage, secure_filename
from starlette_admin.validators import file_size, file_type
from starlette_admin_files import ObjectStorage, allow_unicode_filenames

from conftest import upload


def test_the_private_sanitise_regex_still_exists() -> None:
    """`allow_unicode_filenames` swaps this global in place."""
    assert hasattr(sa_storage_base, "_FILENAME_SANITIZE_RE")
    assert sa_storage_base._FILENAME_SANITIZE_RE.sub("_", "a b") == "a_b"


def test_secure_filename_reads_the_global_at_call_time() -> None:
    """Patching the global would be pointless if the regex were bound early."""
    allow_unicode_filenames(True)
    assert secure_filename("отчёт.pdf") == "отчёт.pdf"

    allow_unicode_filenames(False)
    assert secure_filename("отчёт.pdf") == "pdf"


def test_validators_keep_their_call_shape() -> None:
    """They are called with a stand-in field and no request."""
    for validator in (file_size(10), file_type("image/*")):
        parameters = list(inspect.signature(validator).parameters)
        assert parameters == ["request", "field", "value", "form_values"]


def test_validators_only_touch_field_name() -> None:
    class Probe(BaseField):
        def __getattribute__(self, name: str) -> Any:
            if name not in {"name", "__class__", "__dict__"}:
                raise AssertionError(f"validator touched field.{name}")
            return object.__getattribute__(self, name)

    probe = Probe.__new__(Probe)
    object.__setattr__(probe, "name", "cover")

    file_size(1000)(None, probe, upload(b"x", "a.png", "image/png"), {})  # type: ignore[arg-type]
    file_type("image/*")(None, probe, upload(b"x", "a.png", "image/png"), {})  # type: ignore[arg-type]


def test_file_info_keeps_the_fields_the_column_stores() -> None:
    expected = {
        "filename",
        "content_type",
        "size",
        "storage",
        "key",
        "url",
        "uploaded_at",
        "width",
        "height",
        "thumbnail",
        "extra",
    }

    assert set(FileInfo.__dataclass_fields__) == expected


def test_base_storage_keeps_the_methods_this_package_implements() -> None:
    for name in ("save", "url", "delete", "read", "serve"):
        assert callable(getattr(BaseStorage, name, None)), name

    assert issubclass(ObjectStorage, BaseStorage)


def test_storages_are_registered_by_name() -> None:
    """`File.storage` resolves the name stored in the column."""
    from obstore.store import MemoryStore

    storage = ObjectStorage(name="contract-check", store=MemoryStore())

    assert get_storage("contract-check") is storage


@pytest.mark.parametrize("field_class", [FileField, ImageField])
def test_fields_accept_the_parameters_field_params_produces(field_class: type) -> None:
    # The fields are dataclasses whose annotations cannot be evaluated here,
    # so read the field names rather than the constructor signature.
    names = {field.name for field in dataclasses.fields(field_class)}

    assert {"name", "storage", "upload_folder", "multiple", "max_size", "accept"} <= names


def test_image_field_still_takes_thumbnail_size() -> None:
    names = {field.name for field in dataclasses.fields(ImageField)}

    assert "thumbnail_size" in names


def test_the_default_upload_limit_is_still_a_number() -> None:
    """`FileAttribute` uses it as its own default, so the two stay in step."""
    assert isinstance(DEFAULT_MAX_UPLOAD_SIZE, int)
    assert FileField("x").max_size == DEFAULT_MAX_UPLOAD_SIZE


def test_the_admin_still_processes_file_fields_the_way_the_tests_drive_it() -> None:
    from starlette_admin.base import BaseAdmin

    parameters = list(inspect.signature(BaseAdmin._process_file_fields).parameters)

    assert parameters == ["self", "request", "view", "data", "obj"]
