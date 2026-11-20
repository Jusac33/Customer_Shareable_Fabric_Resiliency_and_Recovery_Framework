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
        self.assertEqual(bf.latest_commit(entries),
                         {"version": 10, "last_commit": T0, "commit_times": {2: T_PLUS_2H, 10: T0}})

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

    def test_secondary_ahead_of_primary_is_diverged_not_in_sync(self):
        rows = bf.compare({"t": c(1, T0)}, {"t": c(2, T_PLUS_2H)})
        self.assertEqual(rows[0]["state"], "diverged")
        self.assertIsNone(rows[0]["behind_hours"])

    def test_stale_replica_copied_late_is_behind(self):
        # Secondary holds v3 but its commit file was copied AFTER primary's v6:
        # file times would say "in sync"; the primary's history says 2h behind.
        p = {"version": 6, "last_commit": T_PLUS_2H, "commit_times": {3: T0, 6: T_PLUS_2H}}
        s = {"version": 3, "last_commit": "Mon, 01 Sep 2026 13:00:00 GMT"}
        row = bf.compare({"t": p}, {"t": s})[0]
        self.assertEqual((row["state"], row["behind_hours"], row["lag_is_lower_bound"]), ("behind", 2.0, False))

    def test_secondary_older_than_retained_log_is_lower_bound(self):
        p = {"version": 60, "last_commit": T_PLUS_2H, "commit_times": {50: T0, 60: T_PLUS_2H}}
        row = bf.compare({"t": p}, {"t": c(10, T0)})[0]
        self.assertEqual((row["state"], row["behind_hours"], row["lag_is_lower_bound"]), ("behind", 2.0, True))
        self.assertTrue(bf.summarize([{"tables": [row]}])["rpo_is_lower_bound"])

    def test_unparseable_timestamp_is_unknown(self):
        rows = bf.compare({"t": c(2, "garbage")}, {"t": c(1, T0)})
        self.assertEqual(rows[0]["state"], "unknown")

    def test_equal_versions_are_in_sync_regardless_of_file_times(self):
        rows = bf.compare({"t": c(1, "garbage")}, {"t": c(1, T0)})
        self.assertEqual((rows[0]["state"], rows[0]["behind_hours"]), ("in_sync", 0.0))


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
    def __init__(self, tree, files=None):
        self.tree = tree
        self.files = files or {}

    def get(self, url, headers=None, params=None, timeout=None):
        r = mock.Mock()
        if params is None:  # file read
            path = url.split("/ws/", 1)[1]
            r.status_code = 200 if path in self.files else 404
            r.text = self.files.get(path, "")
            return r
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
        self.assertEqual(out, {"dbo/claims": {"version": 7, "last_commit": T0, "commit_times": {7: T0}}})

    def test_in_commit_timestamp_detection(self):
        tree = {
            "lh/Tables": [d("lh/Tables/a"), d("lh/Tables/b")],
            "lh/Tables/a/_delta_log": [f("lh/Tables/a/_delta_log/00000000000000000001.json", T0)],
            "lh/Tables/b/_delta_log": [f("lh/Tables/b/_delta_log/00000000000000000004.json", T0)],
        }
        files = {
            "lh/Tables/a/_delta_log/00000000000000000001.json":
                '{"commitInfo":{"inCommitTimestamp":1756720800000,"operation":"WRITE"}}\n{"add":{}}',
            "lh/Tables/b/_delta_log/00000000000000000004.json":
                '{"add":{}}\n{"commitInfo":{"timestamp":1756720800000,"operation":"WRITE"}}',
        }
        out = bf.table_commits(FakeSession(tree, files), "tok", "ws", "lh", check_ict=True)
        self.assertEqual((out["a"]["ict"], out["b"]["ict"]), (True, False))
        rows = bf.compare(out, out)
        self.assertEqual(bf.summarize([{"tables": rows}])["tables_without_in_commit_timestamps"], 1)

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
