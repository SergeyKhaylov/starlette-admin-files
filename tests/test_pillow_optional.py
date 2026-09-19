"""Pillow is an extra: file columns must work without it, image ones must say so."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from obstore.store import MemoryStore
from sqlalchemy import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette_admin_files import FileAttribute, ImageAttribute, ObjectStorage

from conftest import upload


class _BlockPIL:
    """A meta path finder that makes `import PIL` fail."""

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> None:
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("No module named 'PIL'")
        return None


@pytest.fixture
def without_pillow() -> Iterator[None]:
    saved = {name: module for name, module in sys.modules.items() if name.split(".")[0] == "PIL"}
    for name in saved:
        del sys.modules[name]
    blocker = _BlockPIL()
    sys.meta_path.insert(0, blocker)
    try:
        yield
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(saved)


def test_pillow_is_really_blocked(without_pillow: None) -> None:
    with pytest.raises(ImportError):
        importlib.import_module("PIL")


def test_image_attribute_fails_at_declaration_time(without_pillow: None) -> None:
    storage = ObjectStorage(name="nopillow-image", store=MemoryStore())

    with pytest.raises(ImportError, match=r"requires Pillow"):
        ImageAttribute("_img", storage=storage)


async def test_plain_files_work_without_pillow(without_pillow: None) -> None:
    storage = ObjectStorage(name="nopillow-file", store=MemoryStore(), prefix="media")

    class Base(DeclarativeBase):
        pass

    class Doc(Base):
        __tablename__ = "doc_without_pillow"

        id: Mapped[int] = mapped_column(primary_key=True)
        _file: Mapped[dict | None] = mapped_column("file", JSON)
        file = FileAttribute("_file", storage=storage, upload_folder="docs")

    doc = Doc()
    saved = await doc.file.save(upload(b"hello", "отчёт.txt", "text/plain"))

    assert saved.filename == "отчёт.txt"
    assert saved.key.endswith("otchet.txt")
    assert await saved.read() == b"hello"


def test_require_pillow_message_points_at_the_extra(without_pillow: None) -> None:
    from starlette_admin_files.file import require_pillow

    with pytest.raises(ImportError, match=r"starlette-admin-files\[image\]"):
        require_pillow("Thumbnails")
