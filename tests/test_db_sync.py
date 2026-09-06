"""What upsert_match_pairs actually writes, without needing a database.

The column list, the placeholder count and the row tuple have to agree, and
that agreement is invisible until an insert fails at 3am mid-collection. A fake
cursor captures the rows and lets the mapping be asserted directly.
"""

import pytest

from pmarb.db.sync import upsert_match_pairs


class _Cursor:
    """Captures executemany rows; answers the market-id query with everything."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.store["execute"].append((sql, params))

    def executemany(self, sql, rows):
        self.store["sql"] = sql
        self.store["rows"] = list(rows)

    def fetchall(self):
        # First fetch: the known-market ids. Later: live pairs (none).
        if not self.store["fetched"]:
            self.store["fetched"] = True
            return [{"id": i} for i in self.store["known"]]
        return []


class _Conn:
    def __init__(self, known):
        self.store = {"known": known, "rows": [], "sql": "",
                      "execute": [], "fetched": False}

    def cursor(self):
        return _Cursor(self.store)


def _match(**over):
    m = {"kalshi_id": "kalshi:A", "polymarket_id": "polymarket_us:B",
         "match_method": "structured", "similarity_score": 1.0,
         "resolution_date_delta_days": 0}
    m.update(over)
    return m


def _run(matches, reviews=None):
    ids = {m["kalshi_id"] for m in matches} | {m["polymarket_id"] for m in matches}
    conn = _Conn(ids)
    stats = upsert_match_pairs(conn, matches, reviews)
    return conn.store, stats


class TestHazardsReachTheDatabase:
    def test_hazards_are_written_as_a_list(self):
        store, _ = _run([_match(settlement_hazards=["tie_possible"])])
        assert ["tie_possible"] in store["rows"][0]

    def test_an_unflagged_pair_writes_an_empty_list_not_null(self):
        # The column is NOT NULL; a None here fails the insert outright.
        store, _ = _run([_match()])
        row = store["rows"][0]
        assert [] in row
        assert None not in row[:3]

    def test_the_column_list_and_the_row_agree(self):
        # The failure this guards: a column added to the INSERT but not to the
        # tuple (or vice versa) is only caught by a live insert.
        store, _ = _run([_match(settlement_hazards=["tie_possible"])])
        sql = store["sql"]
        columns = sql.split("INSERT INTO match_pair (", 1)[1].split(")", 1)[0]
        n_columns = len([c for c in columns.split(",") if c.strip()])
        n_placeholders = sql.split("VALUES (", 1)[1].split(")", 1)[0].count("%s")
        assert n_columns == n_placeholders == len(store["rows"][0])

    def test_hazards_are_refreshed_on_conflict(self):
        # A pair already in the table must pick up a hazard added later; without
        # this line in the DO UPDATE the flag would only ever apply to pairs
        # seen for the first time.
        store, _ = _run([_match()])
        assert "settlement_hazards = EXCLUDED.settlement_hazards" in store["sql"]


class TestReviewFieldsAreStillVerbatim:
    def test_an_explicit_none_verdict_is_preserved(self):
        # A withdrawn verdict is a decision, not an absence — 21 labels came
        # back once already by treating it as missing.
        store, _ = _run(
            [_match()],
            [{"kalshi_id": "kalshi:A", "polymarket_id": "polymarket_us:B",
              "resolution_match": None, "cohort": "unbiased", "standard": "rules",
              "notes": "", "reviewer": "cyril"}])
        row = store["rows"][0]
        assert "unbiased" in row and "cyril" in row
        assert row[5] is None            # resolution_match, explicitly


@pytest.mark.parametrize("hazards", [None, [], ["tie_possible", "x"]])
def test_any_hazard_shape_produces_a_list(hazards):
    store, _ = _run([_match(settlement_hazards=hazards)])
    written = [c for c in store["rows"][0] if isinstance(c, list)]
    assert written == [list(hazards or [])]
