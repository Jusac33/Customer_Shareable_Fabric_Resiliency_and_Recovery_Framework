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


class TestGroupExpansion(unittest.TestCase):
    """A user reaching the secondary item through a shared group is already replicated."""

    def _plan(self, secondary, resolver, primary=None):
        primary = primary or [share("u@x.com", {"Read"}, extra_keys=("uid",))]
        return ip.plan_for_item(ITEM_LH, "s-lh", primary,
                                ip.expand_group_shares(secondary, resolver), set())

    def test_member_of_shared_group_is_present(self):
        rows = self._plan([share("gid", {"Read"}, ptype="Group")], lambda g: {"uid"})
        self.assertEqual(rows[0]["action"], ip.ACTION_PRESENT)

    def test_unreadable_group_is_not_credited(self):
        rows = self._plan([share("gid", {"Read"}, ptype="Group")], lambda g: None)
        self.assertEqual(rows[0]["action"], ip.ACTION_MANUAL)

    def test_resolver_pages_and_caps_large_groups(self):
        pages = {"/p1": {"value": [{"id": "U1", "userPrincipalName": "A@x.com"}], "@odata.nextLink": "/p2"},
                 "/p2": {"value": [{"id": "U2"}]}}
        graph = mock.Mock(side_effect=lambda m, p: pages["/p1" if "transitiveMembers" in p else p])
        self.assertEqual(ip.make_group_member_resolver(graph)("G"), {"u1", "a@x.com", "u2"})
        self.assertIsNone(ip.make_group_member_resolver(graph, max_members=1)("G"))

    def test_additional_permissions_must_also_match(self):
        p = [{**share("u@x.com", {"Read"}), "additional": ["ReadAll"]}]
        rows = self._plan([share("gid", {"Read"}, ptype="Group")], lambda g: {"u@x.com"}, primary=p)
        self.assertEqual(rows[0]["action"], ip.ACTION_MANUAL)
        self.assertEqual(rows[0]["additional"], "ReadAll")
        s = [{**share("gid", {"Read"}, ptype="Group"), "additional": ["ReadAll", "ReadData"]}]
        rows = self._plan(s, lambda g: {"u@x.com"}, primary=p)
        self.assertEqual(rows[0]["action"], ip.ACTION_PRESENT)


def manual(item_id, item_type, name, principal, ptype="User", rights="Read", additional=""):
    return {"action": ip.ACTION_MANUAL, "item_type": item_type, "item_name": name,
            "primary_item_id": item_id, "secondary_item_id": "s-" + item_id,
            "principal": principal, "principal_type": ptype, "display_name": "",
            "rights": rights, "additional": additional}


class TestPlanAccessGroups(unittest.TestCase):
    def test_users_with_same_item_and_rights_share_one_group(self):
        gp = ip.plan_access_groups([
            manual("p-lh-0001", "Lakehouse", "gold", "a@x.com"),
            manual("p-lh-0001", "Lakehouse", "gold", "b@x.com"),
            manual("p-lh-0001", "Lakehouse", "gold", "c@x.com", rights="ReadReshare"),
            {**manual("p-lh-0001", "Lakehouse", "gold", "z@x.com"), "action": ip.ACTION_PRESENT},
        ])
        managed = [s for s in gp["specs"] if s["kind"] == "managed_group"]
        self.assertEqual(len(managed), 2)
        read = next(s for s in managed if s["rights"] == "Read")
        self.assertEqual([m["principal"] for m in read["members"]], ["a@x.com", "b@x.com"])
        self.assertEqual(read["share_mode"], ip.SHARE_MODE_PORTAL)

    def test_nickname_is_stable_safe_and_permission_specific(self):
        a = ip.access_group_nickname("1C2D3E4F-aaaa-bbbb-cccc-ddddeeeeffff", "Read", "")
        self.assertEqual(a, ip.access_group_nickname("1c2d3e4f-aaaa-bbbb-cccc-ddddeeeeffff", "Read", ""))
        self.assertNotEqual(a, ip.access_group_nickname("1c2d3e4f-aaaa-bbbb-cccc-ddddeeeeffff", "Read", "ReadAll"))
        self.assertLessEqual(len(a), 64)
        self.assertRegex(a, r"^[a-z0-9-]+$")

    def test_group_principal_needs_one_share_not_a_new_group(self):
        gp = ip.plan_access_groups([manual("p-nb", "Notebook", "nb", "GID-1", ptype="Group")])
        self.assertEqual(gp["specs"][0]["kind"], "existing_group")
        self.assertEqual(gp["specs"][0]["group_id"], "GID-1")

    def test_service_principal_on_semantic_model_is_automated_via_group(self):
        gp = ip.plan_access_groups([manual("p-sm", "SemanticModel", "Sales", "sp-oid", ptype="App")])
        self.assertEqual(gp["specs"][0]["share_mode"], ip.SHARE_MODE_API)
        self.assertEqual(gp["specs"][0]["members"], [{"principal": "sp-oid", "principal_type": "App"}])

    def test_entire_tenant_is_reported_unsupported(self):
        gp = ip.plan_access_groups([manual("p-r", "Report", "r", "tenant", ptype="None")])
        self.assertEqual(gp["specs"], [])
        self.assertEqual(len(gp["unsupported"]), 1)


GID = "11111111-2222-3333-4444-555555555555"
UID_A = "aaaaaaaa-0000-0000-0000-000000000001"
UID_B = "aaaaaaaa-0000-0000-0000-000000000002"
UID_X = "aaaaaaaa-0000-0000-0000-0000000000ff"


class FakeGraph:
    def __init__(self, existing_group=False, members=(), fail_add=None, not_found_first=0):
        self.groups = {GID: list(members)} if existing_group else {}
        self.calls = []
        self.fail_add = fail_add
        self.not_found_first = not_found_first

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/groups?$filter"):
            return {"value": [{"id": GID}] if GID in self.groups else []}
        if method == "POST" and path == "/groups":
            self.groups[GID] = []
            return {"id": GID}
        if method == "GET" and path.startswith(f"/groups/{GID}/members"):
            return {"value": [{"id": m} for m in self.groups.get(GID, [])]}
        if method == "GET" and path.startswith("/users/"):
            return {"id": {"a%40x.com": UID_A, "b%40x.com": UID_B}[path.split("/")[2].split("?")[0]]}
        if method == "POST" and path.endswith("/members/$ref"):
            if self.not_found_first:
                self.not_found_first -= 1
                e = Exception("API error 404")
                e.status_code = 404
                raise e
            if self.fail_add:
                raise self.fail_add
            self.groups[GID].append(body["@odata.id"].rsplit("/", 1)[1])
            return {}
        raise AssertionError((method, path))


class TestApplyAccessGroups(unittest.TestCase):
    def _spec(self, item_type="Lakehouse", members=("a@x.com", "b@x.com"), rights="Read"):
        return ip.plan_access_groups([manual("p-1", item_type, "gold", m, rights=rights) for m in members])["specs"]

    def test_creates_group_adds_members_and_lists_one_time_share(self):
        g = FakeGraph()
        res = ip.apply_access_groups(g, mock.Mock(), "sw", self._spec(), sleep=lambda s: None)
        self.assertEqual(res["groups_created"], 1)
        self.assertEqual(res["members_added"], 2)
        self.assertEqual(g.groups[GID], [UID_A, UID_B])
        self.assertEqual(len(res["portal_steps"]), 1)
        self.assertEqual(res["portal_steps"][0]["group_id"], GID)
        self.assertEqual(res["failures"], [])

    def test_existing_members_kept_extras_reported_never_removed(self):
        g = FakeGraph(existing_group=True, members=[UID_A, UID_X])
        res = ip.apply_access_groups(g, mock.Mock(), "sw", self._spec(), sleep=lambda s: None)
        self.assertEqual((res["groups_existing"], res["members_present"], res["members_added"]), (1, 1, 1))
        self.assertEqual(res["extra_members"][0]["object_ids"], [UID_X])
        self.assertNotIn("DELETE", [c[0] for c in g.calls])

    def test_dry_run_writes_nothing(self):
        g = FakeGraph()
        res = ip.apply_access_groups(g, mock.Mock(), "sw", self._spec(), dry_run=True)
        self.assertEqual(res["groups_to_create"], [self._spec()[0]["group_display_name"]])
        self.assertEqual(res["members_to_add"], 2)
        self.assertEqual([c for c in g.calls if c[0] != "GET"], [])

    def test_new_group_replication_delay_is_retried(self):
        g = FakeGraph(not_found_first=2)
        sleeps = []
        res = ip.apply_access_groups(g, mock.Mock(), "sw", self._spec(members=("a@x.com",)), sleep=sleeps.append)
        self.assertEqual(res["members_added"], 1)
        self.assertEqual(sleeps, [5, 10])

    def test_member_failure_is_recorded(self):
        g = FakeGraph(fail_add=Exception("API error 403: Insufficient privileges"))
        res = ip.apply_access_groups(g, mock.Mock(), "sw", self._spec(members=("a@x.com",)), sleep=lambda s: None)
        self.assertEqual(res["members_added"], 0)
        self.assertIn("Insufficient privileges", res["failures"][0]["error"])

    def test_semantic_model_group_grant_is_automated(self):
        pbi = mock.Mock(side_effect=lambda m, ep, body=None: {"value": []} if m == "GET" else {})
        res = ip.apply_access_groups(FakeGraph(), pbi, "sw", self._spec("SemanticModel", rights="ReadReshare"),
                                     sleep=lambda s: None)
        self.assertEqual(res["dataset_group_grants"], 1)
        self.assertEqual(res["portal_steps"], [])
        post = [c for c in pbi.call_args_list if c[0][0] == "POST"][0]
        self.assertEqual(post[0][2], {"identifier": GID, "principalType": "Group",
                                      "datasetUserAccessRight": "ReadReshare"})

    def test_portal_step_dropped_once_group_is_shared(self):
        fabric = mock.Mock(return_value={"accessDetails": [
            {"principal": {"id": GID, "type": "Group"}, "itemAccessDetails": {"permissions": ["Read"]}}]})
        res = ip.apply_access_groups(FakeGraph(), mock.Mock(), "sw", self._spec(),
                                     sleep=lambda s: None, fabric_call=fabric)
        self.assertEqual(res["portal_steps"], [])


class TestGroupsCsvRoundTrip(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import sync_item_permissions as cli
        self.cli = cli

    def test_round_trip_and_reviewer_can_drop_a_member(self):
        specs = ip.plan_access_groups([manual("p-1", "Lakehouse", "gold", m) for m in ("a@x.com", "b@x.com")]
                                      + [manual("p-2", "Notebook", "nb", "GID-9", ptype="Group")])["specs"]
        rows = self.cli.specs_to_rows(specs)
        self.assertEqual(len(rows), 3)
        rows = [r for r in rows if r["member"] != "b@x.com"]
        back = self.cli.rows_to_specs([{k: str(v) for k, v in r.items()} for r in rows])
        managed = next(s for s in back if s["kind"] == "managed_group")
        self.assertEqual(managed["members"], [{"principal": "a@x.com", "principal_type": "User"}])
        self.assertTrue(any(s["kind"] == "existing_group" and s["group_id"] == "GID-9" for s in back))

    def test_malformed_rows_rejected(self):
        with self.assertRaises(ValueError):
            self.cli.rows_to_specs([{"kind": "managed_group", "group_nickname": "", "secondary_item_id": "s"}])
        with self.assertRaises(ValueError):
            self.cli.rows_to_specs([{"kind": "bogus"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
