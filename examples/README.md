# Examples

| File | What it shows |
| --- | --- |
| [`quickstart.py`](quickstart.py) | The smallest setup: a storage, a model with one image column, an admin view. |
| [`fastapi_app.py`](fastapi_app.py) | An admin and REST routes over the same columns, with cleanup wired into the session dependency and the admin hooks. |
| [`generated_documents.py`](generated_documents.py) | Storing a file the backend produced itself: bytes, streams, dicts. |
| [`multiple_files.py`](multiple_files.py) | List columns: append, replace, drop by index, read back. |
| [`cleanup_unit_of_work.py`](cleanup_unit_of_work.py) | Who deletes files and when: orphans after a commit, uploads after a rollback. |
| [`storages.py`](storages.py) | Backends (memory, local, S3, CDN), signed vs public URLs, file-name handling. |

Run the web apps with uvicorn:

```sh
uv run uvicorn examples.quickstart:app --reload
uv run uvicorn examples.fastapi_app:app --reload
```

The rest are scripts:

```sh
uv run python -m examples.generated_documents
uv run python -m examples.multiple_files
uv run python -m examples.cleanup_unit_of_work
uv run python -m examples.storages
```
