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
            "https://c", {"verified": False, "path": "/p"}, self.plan, "ws", self.logger
        )
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(mock_grant.call_count, 1)

    @mock.patch("item_permissions_accelerator.grant_item_access")
    def test_stops_on_auth_failure(self, mock_grant):
        mock_grant.side_effect = acc.InternalApiError(401, "u", "denied")
        result = acc.apply_plan(
            "https://c", {"verified": True, "path": "/p"}, self.plan, "ws", self.logger
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
            "https://c", {"verified": True, "path": "/p"}, self.plan, "ws", self.logger
        )
        self.assertEqual(result["applied"], 3)
        self.assertEqual(result["failed"], 1)

    @mock.patch("item_permissions_accelerator.grant_item_access", return_value=None)
    def test_all_success(self, _g):
        result = acc.apply_plan(
            "https://c", {"verified": True, "path": "/p"}, self.plan, "ws", self.logger
        )
        self.assertEqual(result["applied"], 4)
        self.assertEqual(result["failed"], 0)


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
