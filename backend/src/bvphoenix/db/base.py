"""Declarative base for SQLAlchemy models. Models themselves will land in
`bvphoenix.db.models` as the schema is implemented phase by phase."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
