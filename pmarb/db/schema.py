"""Partition maintenance for the observation table.

`observation` is range-partitioned monthly. Postgres does NOT create partitions
on demand — an insert whose timestamp falls outside every existing partition
fails outright. So partitions must exist BEFORE the month they cover, and the
collector calls this at startup and then daily.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from psycopg import sql

_MONTHS_AHEAD = 3  # keep a runway so a long unattended run never hits the edge


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return _month_start(d + timedelta(days=32))


def ensure_partitions(conn, *, now: datetime | None = None,
                      months_ahead: int = _MONTHS_AHEAD) -> list[str]:
    """Create observation partitions for this month and the next few.

    Idempotent (IF NOT EXISTS), so it is safe to call on every startup and on a
    timer. Returns the partition names it ensured, for logging.
    """
    start = _month_start((now or datetime.now()).date())
    created = []
    for _ in range(months_ahead + 1):
        end = _next_month(start)
        name = f"observation_{start:%Y_%m}"
        # Partition bounds are DDL literals — Postgres will not accept bind
        # parameters there, so they are composed with sql.Literal rather than
        # interpolated by hand.
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {name} PARTITION OF observation "
            "FOR VALUES FROM ({lo}) TO ({hi})"
        ).format(name=sql.Identifier(name),
                 lo=sql.Literal(start.isoformat()),
                 hi=sql.Literal(end.isoformat())))
        created.append(name)
        start = end
    return created
