"""API contract tests.

The queries themselves need a populated database, so those tests skip when one
is not reachable — same convention as the writer's integration tests. What does
NOT need a database is the part most worth pinning: the defaults that decide
whether an unreviewed pair can be reported as an opportunity.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from pmarb.api import queries as q
from pmarb.api.app import app


def _db_up() -> bool:
    try:
        from pmarb.db.engine import connect
        with connect() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_up(), reason="no database reachable")


class TestJsonable:
    def test_decimal_becomes_float_not_string(self):
        # Postgres numeric arrives as Decimal, which pydantic would render as a
        # quoted string. Every consumer compares edges numerically.
        row = q.jsonable({"spread_fee_adj": Decimal("0.00280"), "size": 47})
        assert row["spread_fee_adj"] == pytest.approx(0.0028)
        assert isinstance(row["spread_fee_adj"], float)
        assert row["size"] == 47

    def test_none_row_passes_through(self):
        assert q.jsonable(None) is None


class TestVerifiedOnlyDefaults:
    """The default is exploratory: unreviewed pairs are included, because only
    152 of 3,025 pairs carry a verdict and hiding the rest would misrepresent
    the catalog. The guarantee that replaces the strict default is that every
    row still carries `resolution_match`, so a caller can always tell a
    verified hedge from an unlabelled candidate."""

    def test_openapi_defaults_are_false(self):
        spec = app.openapi()
        for path in ("/opportunities", "/stats/frontier", "/stats/horizon"):
            params = {p["name"]: p for p in spec["paths"][path]["get"]["parameters"]}
            assert params["verified_only"]["schema"]["default"] is False, path

    def test_no_write_routes_exist(self):
        # Read-only by construction: this is a measurement system.
        methods = {m for r in app.routes for m in getattr(r, "methods", set())}
        assert methods <= {"GET", "HEAD"}


@needs_db
class TestEndpoints:
    client = TestClient(app)

    def test_health_reports_latest_observation(self):
        body = self.client.get("/health").json()
        assert body["observations"] >= 0
        assert "latest_observation" in body

    def test_unknown_pair_is_404(self):
        r = self.client.get("/pairs/999999999")
        assert r.status_code == 404

    def test_opportunities_are_sorted_by_edge_desc(self):
        rows = self.client.get("/opportunities?limit=20").json()
        edges = [r["spread_fee_adj"] for r in rows]
        assert edges == sorted(edges, reverse=True)

    def test_verified_only_never_widens_the_result(self):
        strict = self.client.get("/opportunities?limit=500&verified_only=true").json()
        loose = self.client.get("/opportunities?limit=500").json()
        assert len(strict) <= len(loose)
        assert all(r["resolution_match"] is True for r in strict)

    def test_every_row_carries_its_verdict(self):
        # This is what makes the permissive default safe: an unreviewed pair is
        # visibly unreviewed rather than silently indistinguishable.
        rows = self.client.get("/opportunities?limit=200").json()
        assert all("resolution_match" in r for r in rows)

    def test_frontier_edge_decays_from_p25_to_final(self):
        f = self.client.get("/stats/frontier").json()
        if not f["observations"]:
            pytest.skip("no frontier rows collected yet")
        assert f["avg_edge_p25"] > f["avg_edge_p50"] > f["avg_edge_p75"]
        assert f["avg_edge_p75"] > f["avg_edge_final"]

    def test_horizon_buckets_are_ordered_near_to_far(self):
        rows = self.client.get("/stats/horizon").json()
        order = [r["horizon"] for r in rows]
        expected = [h for h in ("under_90d", "90d_to_1y", "over_1y") if h in order]
        assert order == expected
