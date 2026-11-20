"""Tests for item_permissions_internal.py (opt-in, unsupported portal sharing API)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import item_permissions_internal as ipi

SP = "5ca1ab1e-0000-4000-8000-000000000001"
GRP = "0f0f0f0f-0000-0000-0000-000000000001"
USR = "0e0e0e0e-0000-0000-0000-000000000002"


def payload(numeric_id, *entries):
    return {"id": numeric_id, "objectId": "x", "detail": list(entries)}


def sp_entry(perm=1, art=0):
    # Shape observed live on 2026-11-20 for a service principal direct share.
    return {"id": 1000001, "userId": 42, "permissions": perm, "artifactPermissions": art,
            "aadAppId": "a99a99a9-0000-4000-8000-000000000002", "objectId": SP}


def inherited():
    return {"objectId": USR, "userPrincipalName": "a@x.com", "permissions": 7,
            "accessSource": {"folderRoleId": 1}}


class TestParse(unittest.TestCase):
    def test_kinds_inherited_and_numeric_id(self):
        p = ipi.parse(payload(42, sp_entry(1, 1), inherited(),
                              {"objectId": GRP, "groupId": 77, "permissions": 1},
                              {"objectId": USR, "userType": 0, "permissions": 3, "userPrincipalName": "u@x.com"},
                              {"objectId": "zzz", "permissions": 1}))
        self.assertEqual(p["numeric_id"], 42)
        self.assertEqual(p["inherited"], 1)
        self.assertEqual(p["unparsed"], 1)
        self.assertEqual(sorted((d["kind"], d["permissions"], d["artifact_permissions"]) for d in p["direct"]),
                         [("Group", 1, 0), ("ServicePrincipal", 1, 1), ("User", 3, 0)])


ITEM = {"id": "p-lh", "type": "Lakehouse", "displayName": "bronze"}
S_ITEM = {"id": "s-lh", "type": "Lakehouse", "displayName": "bronze"}


class TestPlan(unittest.TestCase):
    def test_grant_present_and_union_upgrade(self):
        prim = ipi.parse(payload(1, sp_entry(1, 1), {"objectId": GRP, "groupId": 7, "permissions": 1}))
        sec = ipi.parse(payload(9, sp_entry(1, 1), {"objectId": GRP, "groupId": 7, "permissions": 2}))
        rows = {r["principal_kind"]: r for r in ipi.plan_item(ITEM, S_ITEM, prim, sec)}
        self.assertEqual(rows["ServicePrincipal"]["action"], ipi.ACTION_PRESENT)
        self.assertEqual(rows["Group"]["action"], ipi.ACTION_UPGRADE)
        self.assertEqual(rows["Group"]["permissions"], 3)  # never downgrades what secondary has
        self.assertEqual(rows["Group"]["secondary_numeric_id"], 9)

    def test_grant_body_matches_captured_portal_request(self):
        row = ipi.plan_item(ITEM, S_ITEM, ipi.parse(payload(1, sp_entry(1, 1))), ipi.parse(payload(9)))[0]
        self.assertEqual(ipi.grant_body(row), {
            "dashboards": [], "reports": [], "workbooks": [], "models": [], "datamarts": [],
            "artifacts": [{"id": 9, "artifactObjectId": "s-lh", "userId": None, "groupId": None,
                           "permissions": 1, "artifactPermissions": 1, "userObjectId": SP,
                           "groupObjectId": None, "isServicePrincipal": True}]})

    def test_group_uses_group_object_id(self):
        row = ipi.plan_item(ITEM, S_ITEM, ipi.parse(payload(1, {"objectId": GRP, "groupId": 7, "permissions": 1})),
                            ipi.parse(payload(9)))[0]
        art = ipi.grant_body(row)["artifacts"][0]
        self.assertEqual((art["groupObjectId"], art["userObjectId"], art["isServicePrincipal"]), (GRP, None, False))

    def test_build_plan_reports_unserved_types_and_unmatched(self):
        client = mock.Mock()
        client.read.side_effect = lambda iid: None if iid == "p-rep" else payload(1, sp_entry()) if iid == "p-lh" else payload(2)
        plan = ipi.build_plan(client,
                              [ITEM, {"id": "p-rep", "type": "Report", "displayName": "r"},
                               {"id": "p-x", "type": "Notebook", "displayName": "only_primary"}],
                              [S_ITEM, {"id": "s-rep", "type": "Report", "displayName": "r"}])
        self.assertEqual(plan["coverage"]["not_served"], {"Report": 1})
        self.assertEqual(plan["coverage"]["unmatched"], 1)
        self.assertEqual(plan["counts"], {ipi.ACTION_GRANT: 1})


class TestApply(unittest.TestCase):
    def setUp(self):
        self.rows = ipi.plan_item(ITEM, S_ITEM, ipi.parse(payload(1, sp_entry(1, 1))), ipi.parse(payload(9)))

    def test_refuses_without_optin_or_ack(self):
        with mock.patch.dict(os.environ, {ipi.ENABLE_ENV: ""}):
            with self.assertRaises(PermissionError):
                ipi.apply_rows(mock.Mock(), self.rows, ipi.ACK_PHRASE)
        with self.assertRaises(PermissionError):
            ipi.apply_rows(mock.Mock(), self.rows, "yes", settings={"internal_sharing_api_enabled": True})

    def test_grant_verified_by_read_back(self):
        client = mock.Mock()
        client.read.return_value = payload(9, sp_entry(1, 1))
        res = ipi.apply_rows(client, self.rows, ipi.ACK_PHRASE, settings={"internal_sharing_api_enabled": True})
        self.assertEqual((res["applied"], res["verified"], res["failed"]), (1, 1, 0))
        client.grant.assert_called_once()

    def test_accepted_but_invisible_grant_is_a_failure(self):
        client = mock.Mock()
        client.read.return_value = payload(9)
        res = ipi.apply_rows(client, self.rows, ipi.ACK_PHRASE, settings={"internal_sharing_api_enabled": True})
        self.assertEqual((res["applied"], res["verified"], res["failed"]), (1, 0, 1))
        self.assertIn("not visible", res["failures"][0]["error"])

    def test_auth_failure_stops_the_run(self):
        err = ipi.InternalApiError(403, "u", "denied")
        client = mock.Mock()
        client.grant.side_effect = err
        res = ipi.apply_rows(client, self.rows * 3, ipi.ACK_PHRASE, settings={"internal_sharing_api_enabled": True})
        self.assertEqual(client.grant.call_count, 1)
        self.assertEqual(res["failed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
