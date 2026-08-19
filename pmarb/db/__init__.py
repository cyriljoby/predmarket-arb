"""Postgres persistence for detected opportunities."""

from pmarb.db.engine import connect, dsn

__all__ = ["connect", "dsn"]
