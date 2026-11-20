"""Regression tests: SQL endpoint host remapping and type-aware item matching."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import common

P_HOST = "x6eps4xrq2xudenlfv6naeo3i4-gwveqpr27ywuvg3mjtocsayzoi.msit-datawarehouse.fabric.microsoft.com"
S_HOST = "x6eps4xrq2xudenlfv6naeo3i4-gegbb6uxbxuulnj4ycmnb5eoy4.msit-datawarehouse.fabric.microsoft.com"

P_ITEMS = [
    {"id": "p-lh", "type": "Lakehouse", "displayName": "gold"},
    {"id": "p-ep", "type": "SQLEndpoint", "displayName": "gold"},
    {"id": "p-wh", "type": "Warehouse", "displayName": "wh"},
]
S_ITEMS = [
    {"id": "s-ep", "type": "SQLEndpoint", "displayName": "gold"},  # listed first on purpose
    {"id": "s-lh", "type": "Lakehouse", "displayName": "gold"},
    {"id": "s-wh", "type": "Warehouse", "displayName": "wh"},
]


def fake_item_api(method, endpoint, *a, **k):
    if "/lakehouses/p-lh" in endpoint:
        return {"properties": {"sqlEndpointProperties": {"connectionString": P_HOST}}}
    if "/lakehouses/s-lh" in endpoint:
        return {"properties": {"sqlEndpointProperties": {"connectionString": S_HOST}}}
    if "/warehouses/p-wh" in endpoint:
        return {"properties": {"connectionString": "Data Source=" + P_HOST.replace("x6", "wa") + ";"}}
    if "/warehouses/s-wh" in endpoint:
        return {"properties": {"connectionString": "Data Source=" + S_HOST.replace("x6", "wb") + ";"}}
    raise AssertionError(endpoint)


class TestSqlHostFrom(unittest.TestCase):
    def test_extracts_host_from_connection_string(self):
        self.assertEqual(common.sql_host_from(f"Data Source={P_HOST};Initial Catalog=gold"), P_HOST)

    def test_plain_host_and_case(self):
        self.assertEqual(common.sql_host_from(P_HOST.upper()), P_HOST)

    def test_non_fabric_host_ignored(self):
        self.assertEqual(common.sql_host_from("server.database.windows.net"), "")


class TestCommonHostMapping(unittest.TestCase):
    def test_maps_lakehouse_and_warehouse_hosts(self):
        def items(ws):
            return P_ITEMS if ws == "pw" else S_ITEMS
        with mock.patch.object(common, "get_items", side_effect=items), \
             mock.patch.object(common, "api_call", side_effect=fake_item_api):
            m = common.build_sql_host_mapping("pw", "sw")
        self.assertEqual(m[P_HOST], S_HOST)
        self.assertEqual(m[P_HOST.replace("x6", "wa")], S_HOST.replace("x6", "wb"))

    def test_lookup_failure_is_skipped_not_fatal(self):
        def boom(*a, **k):
            raise RuntimeError("403")
        with mock.patch.object(common, "get_items", side_effect=lambda ws: P_ITEMS if ws == "pw" else S_ITEMS), \
             mock.patch.object(common, "api_call", side_effect=boom):
            self.assertEqual(common.build_sql_host_mapping("pw", "sw"), {})

    def test_combined_mapping_lets_csv_rows_win(self):
        with mock.patch.object(common, "build_sql_host_mapping", return_value={P_HOST: S_HOST, "k": "auto"}), \
             mock.patch.object(common, "load_reference_mapping", return_value={"k": "csv"}), \
             mock.patch.object(common, "load_artifact_mapping", return_value={}), \
             mock.patch.object(common, "load_connection_mapping", return_value={}):
            m = common.build_combined_mapping("pw", "sw")
        self.assertEqual(m[P_HOST], S_HOST)
        self.assertEqual(m["k"], "csv")

    def test_combined_mapping_without_ids_does_no_discovery(self):
        with mock.patch.object(common, "build_sql_host_mapping") as disc, \
             mock.patch.object(common, "load_reference_mapping", return_value={}), \
             mock.patch.object(common, "load_artifact_mapping", return_value={}), \
             mock.patch.object(common, "load_connection_mapping", return_value={}):
            common.build_combined_mapping()
        disc.assert_not_called()


class TestAppConnectionMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            import app
        finally:
            os.chdir(cwd)
        cls.app = app

    def test_lakehouse_maps_to_lakehouse_not_sql_endpoint_and_hosts_mapped(self):
        a = self.app
        with mock.patch.object(a, "get_workspace_items", side_effect=lambda ws: P_ITEMS if ws == "pw" else S_ITEMS), \
             mock.patch.object(a, "fabric_api", side_effect=fake_item_api):
            m = a._build_connection_map("pw", "sw")
        self.assertEqual(m["pw"], "sw")
        self.assertEqual(m["p-lh"], "s-lh")
        self.assertEqual(m["p-ep"], "s-ep")
        self.assertEqual(m[P_HOST], S_HOST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
