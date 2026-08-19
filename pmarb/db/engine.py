"""Connection plumbing. One place that knows how to reach the database."""

from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://pmarb:pmarb@localhost:5433/pmarb"


def dsn() -> str:
    """Connection string, overridable per environment."""
    return os.environ.get("PMARB_DSN", DEFAULT_DSN)


@contextmanager
def connect(*, autocommit: bool = False):
    """A connection yielding dict rows.

    dict_row is deliberate: the backtest already consumes plain dicts parsed
    from JSONL, so a cursor is a drop-in replacement for the file reader and
    neither has to know about the other.
    """
    with psycopg.connect(dsn(), autocommit=autocommit, row_factory=dict_row) as conn:
        yield conn
