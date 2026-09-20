"""The smallest useful setup: one storage, one model, one admin view.

uv run uvicorn examples.quickstart:app --reload  ->  http://127.0.0.1:8000/admin
"""

from obstore.store import LocalStore
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from starlette.applications import Starlette
from starlette_admin import ImageField
from starlette_admin.contrib.sqla import Admin, ModelView
from starlette_admin_files import ImageColumn, ObjectStorage

# Files land under ./uploads; swap LocalStore for S3Store to go to a bucket.
storage = ObjectStorage(name="media", store=LocalStore("uploads", mkdir=True))


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "product"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(default="")

    # One line per file. The file column declares the mapped column that
    # holds the metadata dict — "photo", mapped as Product._photo — and turns
    # it into an Image on the way out.
    photo = ImageColumn(storage=storage, upload_folder="photos")


class ProductView(ModelView):
    # field_params carries the column name, the storage and the limits, so the
    # form and the backend can never disagree.
    fields = ["id", "name", ImageField(**Product.photo.field_params, label="Photo")]


engine = create_engine("sqlite:///quickstart.db")
Base.metadata.create_all(engine)

app = Starlette()
admin = Admin(engine, title="Quickstart", secret_key="change-me")  # noqa: S106
admin.add_view(ProductView(Product))
admin.mount_to(app)
