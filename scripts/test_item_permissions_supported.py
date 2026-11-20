"""Tests for item_permissions.py (supported-API item permission replication)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import item_permissions as ip


def share(key, rights, ptype="User", extra_keys=()):
    return {"principal_key": key, "identifier": key, "principal_type": ptype,
            "rights": set(rights), "keys": {key, *extra_keys}}


ITEM_SM = {"id": "p-sm", "type": "SemanticModel", "displayName": "Sales"}
ITEM_LH = {"id": "p-lh", "type": "Lakehouse", "displayName": "gold"}


class TestRights(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(ip.rights_from_dataset_right("ReadWriteReshareExplore"),
                         {"Read", "Write", "Reshare", "Explore"})
        self.assertEqual(ip.dataset_right_from_rights({"Explore", "Read"}), "ReadExplore")

    def test_none(self):
        self.assertEqual(ip.rights_from_dataset_right("None"), set())
        self.assertEqual(ip.dataset_right_from_rights(set()), "None")

    def test_read_is_implied(self):
        self.assertEqual(ip.dataset_right_from_rights({"Reshare"}), "ReadReshare")


class TestPrincipalKeys(unittest.TestCase):
    def test_object_id_and_upn_lowercased(self):
        keys = ip.principal_keys({"id": "ABC", "userDetails": {"userPrincipalName": "A@Contoso.com"}})
        self.assertEqual(keys, {"abc", "a@contoso.com"})

    def test_workspace_role_keys(self):
        ras = [{"principal": {"id": "G1", "type": "Group"}},
               {"principal": {"id": "U1", "userDetails": {"userPrincipalName": "u@x.com"}}}]
        self.assertEqual(ip.workspace_role_keys(ras), {"g1", "u1", "u@x.com"})


class TestReaders(unittest.TestCase):
    def test_dataset_shares_exclude_workspace_role_holders(self):
        pbi = mock.Mock(return_value={"value": [
            {"identifier": "member@x.com", "principalType": "User", "datasetUserAccessRight": "ReadWriteReshareExplore"},
            {"identifier": "viewer@x.com", "principalType": "User", "datasetUserAccessRight": "Read"},
            {"identifier": "gone@x.com", "principalType": "User", "datasetUserAccessRight": "None"},
        ]})
        out = ip.read_dataset_shares(pbi, "ws", "ds", {"member@x.com"})
        self.assertEqual([s["identifier"] for s in out], ["viewer@x.com"])

    def test_admin_403_raises_admin_access_required(self):
        err = Exception("API error 403")
        err.status_code = 403
        with self.assertRaises(ip.AdminAccessRequired):
            ip.read_admin_item_shares(mock.Mock(side_effect=err), "ws", "i", "Lakehouse", set())

    def test_admin_other_errors_propagate(self):
        err = Exception("boom")
        err.status_code = 500
        with self.assertRaises(Exception) as ctx:
            ip.read_admin_item_shares(mock.Mock(side_effect=err), "ws", "i", "Lakehouse", set())
        self.assertNotIsInstance(ctx.exception, ip.AdminAccessRequired)

    def test_admin_type_param_only_for_pbi_types(self):
        call = mock.Mock(return_value={"accessDetails": []})
        ip.read_admin_item_shares(call, "ws", "i", "Report", set())
        self.assertTrue(call.call_args[0][1].endswith("?type=Report"))
        ip.read_admin_item_shares(call, "ws", "i", "Lakehouse", set())
        self.assertFalse("?type=" in call.call_args[0][1])

    def test_admin_maps_service_principal_to_app(self):
        call = mock.Mock(return_value={"accessDetails": [
            {"principal": {"id": "SP1", "type": "ServicePrincipal"},
             "itemAccessDetails": {"permissions": ["Read"], "additionalPermissions": []}}]})
        out = ip.read_admin_item_shares(call, "ws", "i", "Lakehouse", set())
        self.assertEqual(out[0]["principal_type"], "App")


class TestPlanForItem(unittest.TestCase):
    def test_missing_semantic_model_share_is_granted(self):
        rows = ip.plan_for_item(ITEM_SM, "s-sm", [share("v@x.com", {"Read", "Explore"})], [], set())
        self.assertEqual(rows[0]["action"], ip.ACTION_GRANT)
        self.assertEqual(rows[0]["rights"], "ReadExplore")

    def test_existing_equal_share_is_present(self):
        rows = ip.plan_for_item(ITEM_SM, "s-sm", [share("v@x.com", {"Read"})],
                                [share("v@x.com", {"Read"})], set())
        self.assertEqual(rows[0]["action"], ip.ACTION_PRESENT)

    def test_weaker_secondary_share_is_upgraded(self):
        rows = ip.plan_for_item(ITEM_SM, "s-sm", [share("v@x.com", {"Read", "Reshare"})],
                                [share("v@x.com", {"Read"})], set())
        self.assertEqual(rows[0]["action"], ip.ACTION_UPGRADE)
        self.assertEqual(rows[0]["rights"], "ReadReshare")

    def test_write_is_stripped_and_flagged(self):
        rows = ip.plan_for_item(ITEM_SM, "s-sm", [share("v@x.com", {"Read", "Write"})], [], set())
        self.assertEqual(rows[0]["rights"], "Read")
        self.assertIn("Write cannot be granted", rows[0]["note"])

    def test_service_principal_is_manual(self):
        rows = ip.plan_for_item(ITEM_SM, "s-sm", [share("sp1", {"Read"}, ptype="App")], [], set())
        self.assertEqual(rows[0]["action"], ip.ACTION_MANUAL)

    def test_non_semantic_model_is_manual_not_dropped(self):
        rows = ip.plan_for_item(ITEM_LH, "s-lh", [share("v@x.com", {"Read"})], [], set())
        self.assertEqual(rows[0]["action"], ip.ACTION_MANUAL)
        self.assertIn("no public API", rows[0]["note"])

    def test_secondary_workspace_role_counts_as_present(self):
        rows = ip.plan_for_item(ITEM_LH, "s-lh", [share("v@x.com", {"Read"})], [], {"v@x.com"})
        self.assertEqual(rows[0]["action"], ip.ACTION_PRESENT)

    def test_match_across_upn_and_object_id(self):
        p = share("oid-1", {"Read"}, extra_keys=("v@x.com",))
        s = share("v@x.com", {"Read"})
        rows = ip.plan_for_item(ITEM_SM, "s-sm", [p], [s], set())
        self.assertEqual(rows[0]["action"], ip.ACTION_PRESENT)


class TestBuildPlanCoverage(unittest.TestCase):
    def _fabric(self, admin_status=None):
        def call(method, endpoint, *a, **k):
            if endpoint.endswith("/roleAssignments"):
                return {"value": []}
            if "/admin/" in endpoint:
                if admin_status:
                    e = Exception(f"API error {admin_status}")
                    e.status_code = admin_status
                    raise e
                return {"accessDetails": []}
            raise AssertionError(endpoint)
        return call

    def test_admin_denied_reports_not_scanned_instead_of_zero(self):
        p = [ITEM_LH, {"id": "p-nb", "type": "Notebook", "displayName": "nb"}]
        s = [{"id": "s-lh", "type": "Lakehouse", "displayName": "gold"},
             {"id": "s-nb", "type": "Notebook", "displayName": "nb"}]
        plan = ip.build_plan(self._fabric(403), mock.Mock(), "pw", "sw", p, s)
        self.assertFalse(plan["complete"])
        self.assertEqual(plan["coverage"]["not_scanned"], 2)
        self.assertTrue(plan["coverage"]["admin_api_denied"])
        self.assertEqual(plan["coverage"]["not_scanned_by_type"], {"Lakehouse": 1, "Notebook": 1})

    def test_semantic_models_scanned_without_admin(self):
        pbi = mock.Mock(return_value={"value": [
            {"identifier": "v@x.com", "principalType": "User", "datasetUserAccessRight": "Read"}]})
        plan = ip.build_plan(self._fabric(403), pbi, "pw", "sw",
                             [ITEM_SM], [{"id": "s-sm", "type": "SemanticModel", "displayName": "Sales"}])
        self.assertEqual(plan["coverage"]["scanned_dataset_api"], 1)
        self.assertEqual(plan["counts"], {ip.ACTION_PRESENT: 1})

    def test_system_and_unmatched_items(self):
        p = [{"id": "x", "type": "Notebook", "displayName": "BCDR_Sync"},
             {"id": "y", "type": "Notebook", "displayName": "only-primary"}]
        plan = ip.build_plan(self._fabric(), mock.Mock(), "pw", "sw", p, [])
        self.assertEqual(plan["coverage"]["unmatched_in_secondary"], 1)
        self.assertEqual(plan["rows"], [])


class TestApply(unittest.TestCase):
    def test_only_grantable_rows_are_sent(self):
        pbi = mock.Mock(return_value={})
        rows = [
            {"action": ip.ACTION_GRANT, "item_name": "A", "principal": "a@x.com", "principal_type": "User",
             "rights": "Read", "secondary_item_id": "s1"},
            {"action": ip.ACTION_UPGRADE, "item_name": "B", "principal": "b@x.com", "principal_type": "Group",
             "rights": "ReadReshare", "secondary_item_id": "s2"},
            {"action": ip.ACTION_MANUAL, "item_name": "C", "principal": "c@x.com", "principal_type": "User",
             "rights": "Read", "secondary_item_id": "s3"},
            {"action": ip.ACTION_PRESENT, "item_name": "D", "principal": "d@x.com", "principal_type": "User",
             "rights": "Read", "secondary_item_id": "s4"},
        ]
        res = ip.apply_rows(pbi, "sw", rows)
        self.assertEqual(res["applied"], 2)
        self.assertEqual(res["skipped_manual"], 1)
        self.assertEqual([c[0][0] for c in pbi.call_args_list], ["POST", "PUT"])
        self.assertEqual(pbi.call_args_list[0][0][2],
                         {"identifier": "a@x.com", "principalType": "User", "datasetUserAccessRight": "Read"})

    def test_failures_are_counted_not_swallowed(self):
        pbi = mock.Mock(side_effect=Exception("400 bad"))
        res = ip.apply_rows(pbi, "sw", [{"action": ip.ACTION_GRANT, "item_name": "A", "principal": "a",
                                         "principal_type": "User", "rights": "Read", "secondary_item_id": "s"}])
        self.assertEqual(res["failed"], 1)
        self.assertIn("400 bad", res["failures"][0]["error"])


class TestNoUnsupportedEndpoints(unittest.TestCase):
    def test_module_uses_only_documented_hosts(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "item_permissions.py"), encoding="utf-8").read()
        self.assertNotIn("analysis.windows.net/metadata", src)
        self.assertNotIn("/metadata/access", src)
        self.assertNotIn("clusterdetails", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
