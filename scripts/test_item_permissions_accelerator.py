"""
Tests for scripts/item_permissions_accelerator.py

Covers the logic that can be tested without touching the internal API:
payload construction, tolerant response parsing, contract loading, safety
gates, and the loud-failure behaviour that the removed dead code lacked.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import item_permissions_accelerator as acc


class TestBuildGrantPayload(unittest.TestCase):
    def test_substitutes_all_placeholders(self):
        payload = acc.build_grant_payload(
            acc.DEFAULT_WRITE_CONTRACT,
            item_id="item-1",
            principal_id="prin-1",
            principal_type="User",
            role="Read",
        )
        self.assertEqual(payload["artifactObjectIds"], ["item-1"])
        perm = payload["permissions"][0]
        self.assertEqual(perm["principalId"], "prin-1")
        self.assertEqual(perm["principalType"], "User")
        self.assertEqual(perm["permissionType"], "Read")
        self.assertIs(perm["grant"], True)

    def test_role_map_is_applied(self):
        contract = {
            "path": "/metadata/access",
            "body_template": {"role": "{role}"},
            "role_map": {"Read": "CanView"},
        }
        payload = acc.build_grant_payload(contract, "i", "p", "User", "Read")
        self.assertEqual(payload["role"], "CanView")

    def test_unmapped_role_passes_through(self):
        contract = {
            "path": "/metadata/access",
            "body_template": {"role": "{role}"},
            "role_map": {"Read": "CanView"},
        }
        payload = acc.build_grant_payload(contract, "i", "p", "User", "Admin")
        self.assertEqual(payload["role"], "Admin")

    def test_template_is_not_mutated(self):
        before = json.dumps(acc.DEFAULT_WRITE_CONTRACT["body_template"], sort_keys=True)
        acc.build_grant_payload(acc.DEFAULT_WRITE_CONTRACT, "i", "p", "User", "Read")
        after = json.dumps(acc.DEFAULT_WRITE_CONTRACT["body_template"], sort_keys=True)
        self.assertEqual(before, after)

    def test_non_string_values_survive_substitution(self):
        contract = {
            "path": "/x",
            "body_template": {"grant": True, "count": 3, "nested": {"id": "{item_id}"}},
            "role_map": {},
        }
        payload = acc.build_grant_payload(contract, "i", "p", "User", "Read")
        self.assertIs(payload["grant"], True)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["nested"]["id"], "i")


class TestExtractPrincipals(unittest.TestCase):
    def test_empty_and_none(self):
        self.assertEqual(acc.extract_principals(None), [])
        self.assertEqual(acc.extract_principals({}), [])
        self.assertEqual(acc.extract_principals([]), [])

    def test_bare_list_shape(self):
        out = acc.extract_principals(
            [{"principalId": "a", "principalType": "User", "permissionType": "Read"}]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["principal_id"], "a")
        self.assertEqual(out[0]["role"], "Read")

    def test_permissions_wrapper(self):
        out = acc.extract_principals(
            {"permissions": [{"objectId": "b", "type": "Group", "role": "Admin"}]}
        )
        self.assertEqual(out[0]["principal_id"], "b")
        self.assertEqual(out[0]["principal_type"], "Group")
        self.assertEqual(out[0]["role"], "Admin")

    def test_nested_principal_object(self):
        out = acc.extract_principals(
            {
                "accessDetails": [
                    {
                        "principal": {"id": "c", "type": "User"},
                        "accessRight": "ReadWrite",
                    }
                ]
            }
        )
        self.assertEqual(out[0]["principal_id"], "c")
        self.assertEqual(out[0]["role"], "ReadWrite")

    def test_skips_entries_missing_id_or_role(self):
        out = acc.extract_principals(
            {
                "value": [
                    {"principalId": "d"},
                    {"permissionType": "Read"},
                    {"principalId": "e", "permissionType": "Read"},
                ]
            }
        )
        self.assertEqual([p["principal_id"] for p in out], ["e"])

    def test_ignores_non_dict_entries(self):
        out = acc.extract_principals({"value": ["junk", 42, None]})
        self.assertEqual(out, [])

    def test_unrecognized_shape_returns_empty_not_crash(self):
        self.assertEqual(acc.extract_principals({"somethingElse": {"a": 1}}), [])


class TestLoadContract(unittest.TestCase):
    def setUp(self):
        self.logger = mock.MagicMock()

    def test_default_is_flagged_unverified(self):
        contract = acc.load_contract(None, self.logger)
        self.assertFalse(contract.get("verified"))
        self.assertTrue(self.logger.warning.called)

    def test_file_contract_defaults_to_verified(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"body_template": {"a": "{item_id}"}}, f)
            path = f.name
        try:
            contract = acc.load_contract(path, self.logger)
            self.assertTrue(contract["verified"])
            self.assertEqual(contract["path"], "/metadata/access")
        finally:
            os.unlink(path)

    def test_file_without_body_template_rejected(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"path": "/x"}, f)
            path = f.name
        try:
            with self.assertRaises(ValueError):
                acc.load_contract(path, self.logger)
        finally:
            os.unlink(path)


class TestInternalCallFailsLoudly(unittest.TestCase):
    """The removed dead code swallowed 404s at debug level. This must not."""

    def setUp(self):
        self.logger = mock.MagicMock()

    def _resp(self, status, body='{"error":"nope"}'):
        r = mock.MagicMock()
        r.status_code = status
        r.text = body
        r.content = body.encode()
        r.json.return_value = json.loads(body)
        return r

    @mock.patch("item_permissions_accelerator.common.get_powerbi_headers", return_value={})
    @mock.patch("item_permissions_accelerator.requests.get")
    def test_raises_with_status_and_body(self, mock_get, _h):
        mock_get.return_value = self._resp(400)
        with self.assertRaises(acc.InternalApiError) as ctx:
            acc.internal_call("GET", "https://c", "/p", self.logger)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("nope", str(ctx.exception))

    @mock.patch("item_permissions_accelerator.common.get_powerbi_headers", return_value={})
    @mock.patch("item_permissions_accelerator.requests.get")
    def test_empty_body_is_success_not_crash(self, mock_get, _h):
        r = mock.MagicMock()
        r.status_code = 200
        r.text = ""
        r.content = b""
        mock_get.return_value = r
        self.assertEqual(acc.internal_call("GET", "https://c", "/p", self.logger), {})

    @mock.patch("item_permissions_accelerator.common.get_powerbi_headers", return_value={})
    @mock.patch("item_permissions_accelerator.requests.get")
    def test_non_json_body_preserved(self, mock_get, _h):
        r = mock.MagicMock()
        r.status_code = 200
        r.text = "<html>"
        r.content = b"<html>"
        r.json.side_effect = ValueError
        mock_get.return_value = r
        self.assertEqual(
            acc.internal_call("GET", "https://c", "/p", self.logger), {"_raw": "<html>"}
        )

    def test_unsupported_method_rejected(self):
        with self.assertRaises(ValueError):
            acc.internal_call("DELETE", "https://c", "/p", self.logger)


class TestReadItemAccess(unittest.TestCase):
    def setUp(self):
        self.logger = mock.MagicMock()

    @mock.patch("item_permissions_accelerator.internal_call")
    def test_falls_back_to_second_path_variant(self, mock_call):
        mock_call.side_effect = [
            acc.InternalApiError(404, "u", "no"),
            {"permissions": []},
        ]
        payload, path = acc.read_item_access("https://c", "i", self.logger)
        self.assertEqual(payload, {"permissions": []})
        self.assertEqual(path, acc.READ_PATH_VARIANTS[1].format(item_id="i"))

    @mock.patch("item_permissions_accelerator.internal_call")
    def test_auth_error_surfaces_immediately(self, mock_call):
        mock_call.side_effect = acc.InternalApiError(403, "u", "denied")
        with self.assertRaises(acc.InternalApiError):
            acc.read_item_access("https://c", "i", self.logger)
        self.assertEqual(mock_call.call_count, 1)

    @mock.patch("item_permissions_accelerator.internal_call")
    def test_all_variants_fail_returns_none(self, mock_call):
        mock_call.side_effect = acc.InternalApiError(404, "u", "no")
        payload, path = acc.read_item_access("https://c", "i", self.logger)
        self.assertIsNone(payload)
        self.assertIsNone(path)
        self.assertTrue(self.logger.warning.called)


class TestApplyPlanStopsOnSystemicFailure(unittest.TestCase):
    def setUp(self):
        self.logger = mock.MagicMock()
        self.plan = [
            {
                "item_name": f"item{i}",
                "secondary_item_id": f"s{i}",
                "principal_id": f"p{i}",
                "principal_type": "User",
                "role": "Read",
            }
            for i in range(4)
        ]

    @mock.patch("item_permissions_accelerator.grant_item_access")
    def test_stops_on_400_with_unverified_contract(self, mock_grant):
        mock_grant.side_effect = acc.InternalApiError(400, "u", "bad shape")
        result = acc.apply_plan(
            "https://c", {"verified": False, "path": "/p"}, self.plan, self.logger
        )
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(mock_grant.call_count, 1)

    @mock.patch("item_permissions_accelerator.grant_item_access")
    def test_stops_on_auth_failure(self, mock_grant):
        mock_grant.side_effect = acc.InternalApiError(401, "u", "denied")
        result = acc.apply_plan(
            "https://c", {"verified": True, "path": "/p"}, self.plan, self.logger
        )
        self.assertEqual(mock_grant.call_count, 1)
        self.assertEqual(result["failed"], 1)

    @mock.patch("item_permissions_accelerator.grant_item_access")
    def test_continues_past_isolated_404_when_verified(self, mock_grant):
        mock_grant.side_effect = [
            None,
            acc.InternalApiError(404, "u", "missing"),
            None,
            None,
        ]
        result = acc.apply_plan(
            "https://c", {"verified": True, "path": "/p"}, self.plan, self.logger
        )
        self.assertEqual(result["applied"], 3)
        self.assertEqual(result["failed"], 1)

    @mock.patch("item_permissions_accelerator.grant_item_access", return_value=None)
    def test_all_success(self, _g):
        result = acc.apply_plan(
            "https://c", {"verified": True, "path": "/p"}, self.plan, self.logger
        )
        self.assertEqual(result["applied"], 4)
        self.assertEqual(result["failed"], 0)


class TestExtractPrincipalsNoTypeGuess(unittest.TestCase):
    def test_missing_type_is_blank_not_user(self):
        out = acc.extract_principals([{"principalId": "a", "permissionType": "Read"}])
        self.assertEqual(out[0]["principal_type"], "")


class TestBuildPlan(unittest.TestCase):
    """Regression tests for planner correctness found on recheck."""

    def setUp(self):
        self.logger = mock.MagicMock()
        self.items = [{"id": "p1", "displayName": "LH", "type": "Lakehouse"}]
        self.mapping = {"p1": "s1"}
        patcher = mock.patch("item_permissions_accelerator.common.save_json")
        self.addCleanup(patcher.stop)
        patcher.start()

    def _run(self, reads):
        def fake_read(cluster, item_id, logger):
            return reads[item_id]

        with mock.patch(
            "item_permissions_accelerator.common.get_items", return_value=self.items
        ), mock.patch(
            "item_permissions_accelerator.read_item_access", side_effect=fake_read
        ):
            return acc.build_plan("https://c", "pw", "sw", self.mapping, self.logger)

    def test_unreadable_secondary_is_skipped_not_planned(self):
        plan, stats = self._run(
            {
                "p1": ([{"principalId": "u", "principalType": "User", "permissionType": "Read"}], "/x"),
                "s1": (None, None),
            }
        )
        self.assertEqual(plan, [])
        self.assertEqual(stats["secondary_unreadable"], 1)
        self.assertEqual(stats["grants_planned"], 0)

    def test_untyped_principal_is_skipped(self):
        plan, stats = self._run(
            {
                "p1": ([{"principalId": "u", "permissionType": "Read"}], "/x"),
                "s1": ([], "/x"),
            }
        )
        self.assertEqual(plan, [])
        self.assertEqual(stats["principals_untyped"], 1)

    def test_missing_grant_is_planned(self):
        plan, stats = self._run(
            {
                "p1": ([{"principalId": "u", "principalType": "Group", "permissionType": "Read"}], "/x"),
                "s1": ([], "/x"),
            }
        )
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["secondary_item_id"], "s1")
        self.assertEqual(plan[0]["principal_type"], "Group")

    def test_existing_grant_not_replanned(self):
        entry = {"principalId": "u", "principalType": "User", "permissionType": "Read"}
        plan, stats = self._run({"p1": ([entry], "/x"), "s1": ([entry], "/x")})
        self.assertEqual(plan, [])
        self.assertEqual(stats["already_present"], 1)


class TestPlanCsvRoundTrip(unittest.TestCase):
    def setUp(self):
        self.logger = mock.MagicMock()
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "plan.csv")

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        os.rmdir(self.dir)

    def test_round_trip(self):
        rows = [
            {
                "item_name": "LH",
                "item_type": "Lakehouse",
                "primary_item_id": "p1",
                "secondary_item_id": "s1",
                "principal_id": "u",
                "principal_type": "User",
                "role": "Read",
                "artifact_permissions": "9",
            }
        ]
        acc.write_plan_csv(rows, self.path, self.logger)
        self.assertEqual(acc.load_plan_csv(self.path), rows)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            acc.load_plan_csv(self.path)

    def test_missing_column_raises(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("item_name,principal_id\nLH,u\n")
        with self.assertRaises(ValueError):
            acc.load_plan_csv(self.path)

    def test_blank_required_field_raises(self):
        acc.write_plan_csv(
            [
                {
                    "item_name": "LH",
                    "item_type": "",
                    "primary_item_id": "p1",
                    "secondary_item_id": "s1",
                    "principal_id": "u",
                    "principal_type": "",
                    "role": "Read",
                }
            ],
            self.path,
            self.logger,
        )
        with self.assertRaises(ValueError):
            acc.load_plan_csv(self.path)


class TestMainApplyUsesReviewedPlan(unittest.TestCase):
    """The core recheck finding: --apply must not re-plan from live state."""

    def _argv(self, *extra):
        return ["item_permissions_accelerator.py", *extra]

    @mock.patch("item_permissions_accelerator.resolve_cluster", return_value="https://c")
    @mock.patch("item_permissions_accelerator.build_plan")
    @mock.patch("item_permissions_accelerator.apply_plan")
    def test_apply_reads_csv_and_never_calls_build_plan(self, mock_apply, mock_build, _rc):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "plan.csv")
        reviewed = [
            {
                "item_name": "Kept",
                "item_type": "Lakehouse",
                "primary_item_id": "p1",
                "secondary_item_id": "s1",
                "principal_id": "u",
                "principal_type": "User",
                "role": "Read",
            }
        ]
        acc.write_plan_csv(reviewed, path, mock.MagicMock())
        mock_apply.return_value = {"applied": 1, "failed": 0, "failures": []}
        try:
            with mock.patch.object(
                sys,
                "argv",
                self._argv(
                    "--apply",
                    "--i-understand-this-is-unsupported",
                    "--plan-file",
                    path,
                ),
            ):
                rc = acc.main()
        finally:
            os.unlink(path)
            os.rmdir(d)

        self.assertEqual(rc, 0)
        mock_build.assert_not_called()
        applied_rows = mock_apply.call_args[0][2]
        self.assertEqual([r["item_name"] for r in applied_rows], ["Kept"])

    @mock.patch("item_permissions_accelerator.resolve_cluster", return_value="https://c")
    def test_apply_without_plan_file_fails_cleanly(self, _rc):
        with mock.patch.object(
            sys,
            "argv",
            self._argv(
                "--apply",
                "--i-understand-this-is-unsupported",
                "--plan-file",
                os.path.join(tempfile.gettempdir(), "does-not-exist-xyz.csv"),
            ),
        ):
            self.assertEqual(acc.main(), 1)

    def test_modes_are_mutually_exclusive(self):
        with mock.patch.object(sys, "argv", self._argv("--plan", "--apply", "--i-understand-this-is-unsupported")):
            with self.assertRaises(SystemExit) as ctx:
                acc.main()
        self.assertEqual(ctx.exception.code, 2)

    @mock.patch("item_permissions_accelerator.resolve_cluster", return_value="https://c")
    @mock.patch(
        "item_permissions_accelerator.verify_contract",
        side_effect=acc.InternalApiError(403, "u", "denied"),
    )
    def test_auth_error_exits_cleanly_not_traceback(self, _vc, _rc):
        with mock.patch.object(sys, "argv", self._argv("--verify-contract")):
            self.assertEqual(acc.main(), 1)


# Sanitized copy of the shape returned live by /metadata/access/artifacts/{id}
# on 2026-09-26 (fake IDs, PII fields removed). Entries with accessSource are
# inherited from workspace roles; entries without it are direct shares.
LIVE_SHAPE = {
    "id": 243287,
    "displayName": "LakeHouse01",
    "objectId": "aaaaaaaa-0000-0000-0000-000000000000",
    "permissions": 327,
    "artifactPermissions": 15,
    "sharedWithCount": 0,
    "detail": [
        {"id": 243287, "userId": 1, "permissions": 327, "artifactPermissions": 15,
         "objectId": "11111111-0000-0000-0000-000000000001", "userType": 0,
         "accessSource": {"id": 9, "artifactLinkId": None, "folderRoleId": 1,
                          "folderRole": {"id": 1, "name": "Administrator"}}},
        {"id": 243287, "userId": 2, "permissions": 1,
         "objectId": "22222222-0000-0000-0000-000000000002", "groupId": 77},
        {"id": 243287, "userId": 3, "permissions": 1,
         "objectId": "33333333-0000-0000-0000-000000000003", "userType": 0},
        {"id": 243287, "userId": 4, "permissions": 1, "artifactPermissions": 9,
         "objectId": "44444444-0000-0000-0000-000000000004", "userType": 0},
        {"id": 243287, "userId": 5, "permissions": 71, "artifactPermissions": 11,
         "objectId": "55555555-0000-0000-0000-000000000005", "userType": 0,
         "accessSource": {"id": 9, "folderRoleId": 2,
                          "folderRole": {"id": 2, "name": "Member"}}},
    ],
}


class TestLiveResponseShape(unittest.TestCase):
    """Regression: the shipped parser recognised 0 of 12 live entries."""

    def test_counts(self):
        r = acc.parse_access(LIVE_SHAPE)
        self.assertEqual(len(r["direct"]), 3)
        self.assertEqual(r["inherited"], 2)
        self.assertEqual(r["unparsed"], 0)

    def test_inherited_workspace_roles_are_not_direct(self):
        ids = {p["principal_id"] for p in acc.extract_principals(LIVE_SHAPE)}
        self.assertNotIn("11111111-0000-0000-0000-000000000001", ids)
        self.assertNotIn("55555555-0000-0000-0000-000000000005", ids)

    def test_group_and_user_types(self):
        by_id = {p["principal_id"]: p for p in acc.extract_principals(LIVE_SHAPE)}
        self.assertEqual(by_id["22222222-0000-0000-0000-000000000002"]["principal_type"], "Group")
        self.assertEqual(by_id["33333333-0000-0000-0000-000000000003"]["principal_type"], "User")

    def test_bitmasks_captured(self):
        by_id = {p["principal_id"]: p for p in acc.extract_principals(LIVE_SHAPE)}
        p = by_id["44444444-0000-0000-0000-000000000004"]
        self.assertEqual(p["role"], "1")
        self.assertEqual(p["artifact_permissions"], "9")
        self.assertEqual(by_id["33333333-0000-0000-0000-000000000003"]["artifact_permissions"], "")

    def test_numeric_artifact_id_never_used_as_principal(self):
        entry = {"detail": [{"id": 243287, "permissions": 1, "userType": 0}]}
        r = acc.parse_access(entry)
        self.assertEqual(r["direct"], [])
        self.assertEqual(r["unparsed"], 1)

    def test_untyped_live_entry_left_blank(self):
        entry = {"detail": [{"id": 1, "permissions": 1, "objectId": "x"}]}
        self.assertEqual(acc.extract_principals(entry)[0]["principal_type"], "")

    def test_zero_permission_bitmask_is_parsed_not_dropped(self):
        entry = {"detail": [{"id": 1, "permissions": 0, "objectId": "x", "userType": 0}]}
        self.assertEqual(acc.extract_principals(entry)[0]["role"], "0")


class TestTypedSubstitution(unittest.TestCase):
    def test_bitmask_sent_as_json_number(self):
        body = acc.build_grant_payload(
            acc.DEFAULT_WRITE_CONTRACT, "i", "p", "User", "1", artifact_permissions="9"
        )
        perm = body["permissions"][0]
        self.assertEqual(perm["permissionType"], 1)
        self.assertEqual(perm["artifactPermissions"], 9)

    def test_blank_artifact_permissions_becomes_null(self):
        body = acc.build_grant_payload(acc.DEFAULT_WRITE_CONTRACT, "i", "p", "User", "1")
        self.assertIsNone(body["permissions"][0]["artifactPermissions"])

    def test_guid_stays_string(self):
        body = acc.build_grant_payload(
            acc.DEFAULT_WRITE_CONTRACT, "i", "12345678-aaaa-bbbb-cccc-000000000000", "User", "1"
        )
        self.assertEqual(body["permissions"][0]["principalId"], "12345678-aaaa-bbbb-cccc-000000000000")

    def test_embedded_placeholder_is_textual(self):
        contract = {"path": "/x", "body_template": {"note": "grant {role} on {item_id}"}, "role_map": {}}
        body = acc.build_grant_payload(contract, "abc", "p", "User", "1")
        self.assertEqual(body["note"], "grant 1 on abc")


class TestBuildPlanLiveShape(unittest.TestCase):
    def setUp(self):
        self.logger = mock.MagicMock()
        patcher = mock.patch("item_permissions_accelerator.common.save_json")
        self.addCleanup(patcher.stop)
        patcher.start()

    def _run(self, p_payload, s_payload):
        reads = {"p1": (p_payload, "/x"), "s1": (s_payload, "/x")}
        with mock.patch(
            "item_permissions_accelerator.common.get_items",
            return_value=[{"id": "p1", "displayName": "LH", "type": "Lakehouse"}],
        ), mock.patch(
            "item_permissions_accelerator.read_item_access",
            side_effect=lambda c, i, l: reads[i],
        ):
            return acc.build_plan("https://c", "pw", "sw", {"p1": "s1"}, self.logger)

    def test_plans_only_direct_shares(self):
        plan, stats = self._run(LIVE_SHAPE, {"detail": []})
        self.assertEqual(stats["grants_planned"], 3)
        self.assertEqual(stats["inherited_skipped"], 2)
        self.assertEqual({r["principal_type"] for r in plan}, {"User", "Group"})

    def test_different_artifact_permissions_is_not_already_present(self):
        s = {"detail": [{"id": 9, "permissions": 1, "objectId": "44444444-0000-0000-0000-000000000004", "userType": 0}]}
        plan, stats = self._run(LIVE_SHAPE, s)
        ids = [r["principal_id"] for r in plan]
        self.assertIn("44444444-0000-0000-0000-000000000004", ids)

    def test_unparsed_entries_are_counted(self):
        bad = {"detail": [{"id": 1, "permissions": 1}]}
        plan, stats = self._run(bad, {"detail": []})
        self.assertEqual(plan, [])
        self.assertEqual(stats["entries_unparsed"], 1)


class TestNotWiredIntoAutomatedPaths(unittest.TestCase):
    """Guard: the accelerator must stay out of the automated DR path."""

    def _src(self, name):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(p, "r", encoding="utf-8") as f:
            return f.read()

    def test_sync_permissions_does_not_import_accelerator(self):
        self.assertNotIn("item_permissions_accelerator", self._src("sync_permissions.py"))

    def test_failover_does_not_import_accelerator(self):
        self.assertNotIn("item_permissions_accelerator", self._src("failover.py"))

    def test_failback_does_not_import_accelerator(self):
        self.assertNotIn("item_permissions_accelerator", self._src("failback.py"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
