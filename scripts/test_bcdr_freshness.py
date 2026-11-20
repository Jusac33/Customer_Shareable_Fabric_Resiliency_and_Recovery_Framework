"""Tests for bcdr_freshness.py (measured RPO via OneLake data plane)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bcdr_freshness as bf


def c(version, ts):
    return {"version": version, "last_commit": ts}


T0 = "Mon, 01 Sep 2026 10:00:00 GMT"
T_PLUS_2H = "Mon, 01 Sep 2026 12:00:00 GMT"
T_PLUS_3MIN = "Mon, 01 Sep 2026 10:03:00 GMT"


class TestLatestCommit(unittest.TestCase):
    def test_picks_highest_version_not_newest_listing_order(self):
        entries = [
            {"name": "lh/Tables/t/_delta_log/00000000000000000002.json", "lastModified": T_PLUS_2H},
            {"name": "lh/Tables/t/_delta_log/00000000000000000010.json", "lastModified": T0},
            {"name": "lh/Tables/t/_delta_log/00000000000000000010.checkpoint.parquet", "lastModified": T0},
            {"name": "lh/Tables/t/_delta_log/_last_checkpoint", "lastModified": T0},
        ]
        self.assertEqual(bf.latest_commit(entries), {"version": 10, "last_commit": T0})

    def test_empty(self):
        self.assertIsNone(bf.latest_commit([]))
        self.assertIsNone(bf.latest_commit(None))


class TestCompare(unittest.TestCase):
    def test_states(self):
        rows = {r["table"]: r for r in bf.compare(
            {"dbo/a": c(5, T_PLUS_2H), "dbo/b": c(3, T0), "dbo/c": c(1, T0), "dbo/d": c(9, T_PLUS_3MIN)},
            {"dbo/a": c(4, T0), "dbo/b": c(3, T0), "dbo/x": c(1, T0), "dbo/d": c(9, T0)})}
        self.assertEqual(rows["dbo/a"]["state"], "behind")
        self.assertEqual(rows["dbo/a"]["behind_hours"], 2.0)
        self.assertEqual(rows["dbo/b"]["state"], "in_sync")
        self.assertEqual(rows["dbo/c"]["state"], "missing_in_secondary")
        self.assertEqual(rows["dbo/x"]["state"], "extra_in_secondary")
        self.assertEqual(rows["dbo/d"]["state"], "in_sync")  # within tolerance

    def test_secondary_newer_is_not_negative_lag(self):
        rows = bf.compare({"t": c(1, T0)}, {"t": c(2, T_PLUS_2H)})
        self.assertEqual(rows[0]["behind_hours"], 0.0)
        self.assertEqual(rows[0]["state"], "in_sync")

    def test_unparseable_timestamp_is_unknown(self):
        rows = bf.compare({"t": c(1, "garbage")}, {"t": c(1, T0)})
        self.assertEqual(rows[0]["state"], "unknown")


class TestSummarize(unittest.TestCase):
    def test_worst_lag_is_measured_rpo_and_errors_mark_incomplete(self):
        lhs = [{"lakehouse": "a", "tables": bf.compare({"t": c(2, T_PLUS_2H)}, {"t": c(1, T0)})},
               {"lakehouse": "b", "tables": [], "error": "b: HTTP 403"}]
        s = bf.summarize(lhs)
        self.assertEqual(s["measured_rpo_hours"], 2.0)
        self.assertFalse(s["complete"])
        self.assertEqual(s["errors"], ["b: HTTP 403"])

    def test_no_tables(self):
        s = bf.summarize([])
        self.assertIsNone(s["measured_rpo_hours"])
        self.assertTrue(s["complete"])


class FakeSession:
    """Minimal OneLake DFS fake keyed by directory."""
    def __init__(self, tree):
        self.tree = tree

    def get(self, url, headers=None, params=None, timeout=None):
        r = mock.Mock()
        d = params["directory"]
        if d not in self.tree:
            r.status_code = 404
            return r
        val = self.tree[d]
        if isinstance(val, int):
            r.status_code, r.text = val, "denied"
            return r
        r.status_code = 200
        r.headers = {}
        r.json.return_value = {"paths": val}
        return r


def d(name):
    return {"name": name, "isDirectory": "true"}


def f(name, ts):
    return {"name": name, "lastModified": ts}


class TestTableCommits(unittest.TestCase):
    def test_schema_enabled_lakehouse_descends_into_schema(self):
        tree = {
            "lh/Tables": [d("lh/Tables/dbo")],
            "lh/Tables/dbo": [d("lh/Tables/dbo/claims")],
            "lh/Tables/dbo/claims/_delta_log": [f("lh/Tables/dbo/claims/_delta_log/00000000000000000007.json", T0)],
        }
        out = bf.table_commits(FakeSession(tree), "tok", "ws", "lh")
        self.assertEqual(out, {"dbo/claims": {"version": 7, "last_commit": T0}})

    def test_flat_lakehouse(self):
        tree = {
            "lh/Tables": [d("lh/Tables/sales")],
            "lh/Tables/sales/_delta_log": [f("lh/Tables/sales/_delta_log/00000000000000000001.json", T0)],
        }
        self.assertEqual(list(bf.table_commits(FakeSession(tree), "tok", "ws", "lh")), ["sales"])

    def test_http_error_raises_not_empty(self):
        with self.assertRaises(bf.FreshnessError):
            bf.table_commits(FakeSession({"lh/Tables": 403}), "tok", "ws", "lh")


class TestMeasurePair(unittest.TestCase):
    def test_missing_secondary_lakehouse_is_an_error(self):
        res = bf.measure_pair("tok", "pw", "sw", {"gold": "p1"}, {}, session_factory=lambda: FakeSession({}))
        self.assertFalse(res["summary"]["complete"])
        self.assertIn("no lakehouse", res["lakehouses"][0]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
