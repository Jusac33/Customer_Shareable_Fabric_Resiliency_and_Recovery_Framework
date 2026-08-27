"""
Test Suite: DR Item Ownership & Connection Role Assignments

Covers the two out-of-definition DR gaps:

  Gap 1 — Semantic model ownership takeover
      common.FabricAuthenticator per-scope token cache, common.powerbi_api_call(),
      and failover.takeover_semantic_models() (Power BI Default.TakeOver).
      https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/take-over-in-group

  Gap 2 — Connection role assignments for the DR executing principal
      sync_permissions.sync_connection_role_assignments() — deduplicated
      connection discovery + skip-if-already-granted delta.
      https://learn.microsoft.com/en-us/rest/api/fabric/core/connections

All HTTP is mocked; this suite never touches a live API.

Run:
    python -m pytest scripts/test_dr_ownership_and_connections.py -v
    # or directly:
    python scripts/test_dr_ownership_and_connections.py
"""

import logging
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # repo root -> common
sys.path.insert(0, _HERE)                   # scripts   -> failover, sync_permissions

import common  # noqa: E402
import failover as fo  # noqa: E402
import sync_permissions as sp  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_logger(name: str = "test-dr") -> logging.Logger:
    """Return a silent logger so test output stays clean."""
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


SECONDARY_WS = "secondary-ws-guid-5678"
PRINCIPAL_ID = "sp-object-id-0001"

FAKE_MODELS = [
    {"id": "sm-id-001", "displayName": "Claims_Model", "type": "SemanticModel"},
    {"id": "sm-id-002", "displayName": "Fraud_Model", "type": "SemanticModel"},
]

FAKE_ITEMS = [
    {"id": "pipe-id-001", "displayName": "Ingest_Claims", "type": "DataPipeline"},
    {"id": "pipe-id-002", "displayName": "Ingest_Fraud", "type": "DataPipeline"},
    {"id": "flow-id-003", "displayName": "Curate_Flow", "type": "DataflowsGen2"},
]

CONN_A = "conn-guid-aaaa"
CONN_B = "conn-guid-bbbb"


def _mock_response(status_code=200, body=None, raw=None, headers=None):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.content = raw if raw is not None else (b"{}" if body is None else b'{"mocked":true}')
    resp.text = resp.content.decode("utf-8", errors="ignore")
    resp.json.return_value = body if body is not None else {}
    return resp


def _fake_fabric_api(item_connections, connection_roles, recorder):
    """
    Build a stand-in for common.api_call that serves:
      GET  /workspaces/{ws}/items/{id}/connections
      GET  /connections/{id}/roleAssignments
      POST /connections/{id}/roleAssignments
    and records every call as (method, endpoint, payload).
    """
    def _api_call(method, endpoint, payload=None, *args, **kwargs):
        recorder.append((method, endpoint, payload))

        if method == "GET" and "/items/" in endpoint and endpoint.endswith("/connections"):
            item_id = endpoint.split("/items/")[1].split("/")[0]
            value = item_connections.get(item_id)
            if isinstance(value, Exception):
                raise value
            return {"value": value or []}

        if method == "GET" and endpoint.startswith("/connections/") and endpoint.endswith("/roleAssignments"):
            conn_id = endpoint.split("/")[2]
            value = connection_roles.get(conn_id)
            if isinstance(value, Exception):
                raise value
            return {"value": value or []}

        if method == "POST" and endpoint.startswith("/connections/") and endpoint.endswith("/roleAssignments"):
            return {}

        raise AssertionError(f"Unexpected API call: {method} {endpoint}")

    return _api_call


def _mutating_calls(recorder):
    """Filter a call recorder down to state-changing calls."""
    return [c for c in recorder if c[0].upper() in ("POST", "PUT", "PATCH", "DELETE")]


# ─────────────────────────────────────────────────────────────────────────────
# Gap 1 — per-scope token caching
# ─────────────────────────────────────────────────────────────────────────────

class TestPerScopeTokenCache(unittest.TestCase):
    """FabricAuthenticator must cache one token per scope, not one globally."""

    def setUp(self):
        self.auth = common.FabricAuthenticator("tenant-id", "client-id", "client-secret")
        self.requested_scopes = []

    def _msal_patch(self):
        def _acquire(scopes=None, **kwargs):
            self.requested_scopes.append(scopes[0])
            return {"access_token": f"token-for-{scopes[0]}", "expires_in": 3600}

        mock_app = MagicMock()
        mock_app.acquire_token_for_client.side_effect = _acquire
        return patch.object(common.msal, "ConfidentialClientApplication", return_value=mock_app)

    def test_default_scope_is_fabric(self):
        with self._msal_patch():
            self.auth.get_token()
        self.assertEqual(self.requested_scopes, [common.FABRIC_API_SCOPE])

    def test_powerbi_scope_is_requested_verbatim(self):
        with self._msal_patch():
            self.auth.get_token(scope=common.POWERBI_API_SCOPE)
        self.assertEqual(self.requested_scopes, [common.POWERBI_API_SCOPE])

    def test_distinct_tokens_per_scope(self):
        with self._msal_patch():
            fabric_token = self.auth.get_token()
            powerbi_token = self.auth.get_token(scope=common.POWERBI_API_SCOPE)
        self.assertNotEqual(fabric_token, powerbi_token)

    def test_second_scope_does_not_evict_first(self):
        with self._msal_patch():
            fabric_first = self.auth.get_token()
            self.auth.get_token(scope=common.POWERBI_API_SCOPE)
            fabric_again = self.auth.get_token()
            powerbi_again = self.auth.get_token(scope=common.POWERBI_API_SCOPE)

        self.assertEqual(fabric_first, fabric_again)
        self.assertEqual(powerbi_again, f"token-for-{common.POWERBI_API_SCOPE}")
        # Exactly two acquisitions — one per scope, the rest served from cache
        self.assertEqual(
            self.requested_scopes,
            [common.FABRIC_API_SCOPE, common.POWERBI_API_SCOPE],
        )

    def test_cache_key_includes_scope(self):
        with self._msal_patch():
            self.auth.get_token()
            self.auth.get_token(scope=common.POWERBI_API_SCOPE)

        self.assertNotIn("fabric_token", self.auth.token_cache)
        self.assertEqual(len(self.auth.token_cache), 2)
        self.assertTrue(any(common.FABRIC_API_SCOPE in k for k in self.auth.token_cache))
        self.assertTrue(any(common.POWERBI_API_SCOPE in k for k in self.auth.token_cache))

    def test_force_refresh_reacquires_for_that_scope_only(self):
        with self._msal_patch():
            self.auth.get_token()
            self.auth.get_token(force_refresh=True)
            self.auth.get_token(scope=common.POWERBI_API_SCOPE)
            self.auth.get_token(scope=common.POWERBI_API_SCOPE)

        self.assertEqual(
            self.requested_scopes,
            [common.FABRIC_API_SCOPE, common.FABRIC_API_SCOPE, common.POWERBI_API_SCOPE],
        )

    def test_expiry_keeps_five_minute_buffer(self):
        with self._msal_patch():
            self.auth.get_token()

        key = next(iter(self.auth.token_expiry))
        self.assertAlmostEqual(
            self.auth.token_expiry[key], time.time() + 3600 - 300, delta=10
        )

    def test_expired_token_is_reacquired(self):
        with self._msal_patch():
            self.auth.get_token()
            for key in self.auth.token_expiry:
                self.auth.token_expiry[key] = time.time() - 1
            self.auth.get_token()

        self.assertEqual(len(self.requested_scopes), 2)


# ─────────────────────────────────────────────────────────────────────────────
# Gap 1 — powerbi_api_call semantics
# ─────────────────────────────────────────────────────────────────────────────

class TestPowerBIApiCall(unittest.TestCase):
    """powerbi_api_call mirrors api_call but targets the Power BI surface."""

    def test_posts_to_powerbi_base_url(self):
        resp = _mock_response(200, raw=b"")
        with patch.object(common, "get_powerbi_headers", return_value={}), \
             patch.object(common.requests, "post", return_value=resp) as mock_post:
            common.powerbi_api_call("POST", "/groups/ws/datasets/ds/Default.TakeOver")

        url = mock_post.call_args[0][0]
        self.assertEqual(
            url, f"{common.POWERBI_API_BASE}/groups/ws/datasets/ds/Default.TakeOver"
        )

    def test_empty_200_body_returns_empty_dict(self):
        # Default.TakeOver answers 200 with no body
        resp = _mock_response(200, raw=b"")
        with patch.object(common, "get_powerbi_headers", return_value={}), \
             patch.object(common.requests, "post", return_value=resp):
            self.assertEqual(common.powerbi_api_call("POST", "/x"), {})

    def test_204_returns_empty_dict(self):
        resp = _mock_response(204, raw=b"")
        with patch.object(common, "get_powerbi_headers", return_value={}), \
             patch.object(common.requests, "delete", return_value=resp):
            self.assertEqual(common.powerbi_api_call("DELETE", "/x"), {})

    def test_json_body_is_returned(self):
        resp = _mock_response(200, body={"value": [1, 2]}, raw=b'{"value":[1,2]}')
        with patch.object(common, "get_powerbi_headers", return_value={}), \
             patch.object(common.requests, "get", return_value=resp):
            self.assertEqual(common.powerbi_api_call("GET", "/x"), {"value": [1, 2]})

    def test_429_retries_with_backoff(self):
        throttled = _mock_response(429, raw=b"")
        ok = _mock_response(200, body={"ok": True}, raw=b'{"ok":true}')
        with patch.object(common, "get_powerbi_headers", return_value={}), \
             patch.object(common.requests, "post", side_effect=[throttled, ok]), \
             patch.object(common.time, "sleep") as mock_sleep:
            out = common.powerbi_api_call("POST", "/x")

        self.assertEqual(out, {"ok": True})
        mock_sleep.assert_called_once()

    def test_503_retries_with_backoff(self):
        unavailable = _mock_response(503, raw=b"")
        ok = _mock_response(200, body={"ok": True}, raw=b'{"ok":true}')
        with patch.object(common, "get_powerbi_headers", return_value={}), \
             patch.object(common.requests, "post", side_effect=[unavailable, ok]), \
             patch.object(common.time, "sleep") as mock_sleep:
            out = common.powerbi_api_call("POST", "/x")

        self.assertEqual(out, {"ok": True})
        mock_sleep.assert_called_once()

    def test_error_raises_with_response_body(self):
        resp = _mock_response(403, body={"error": "PowerBINotAuthorizedException"},
                              raw=b'{"error":"PowerBINotAuthorizedException"}')
        with patch.object(common, "get_powerbi_headers", return_value={}), \
             patch.object(common.requests, "post", return_value=resp):
            with self.assertRaises(Exception) as ctx:
                common.powerbi_api_call("POST", "/x")

        self.assertIn("403", str(ctx.exception))
        self.assertIn("PowerBINotAuthorizedException", str(ctx.exception))

    def test_202_polls_long_running_operation(self):
        accepted = _mock_response(202, raw=b"", headers={"Operation-Location": "https://op/1"})
        with patch.object(common, "get_powerbi_headers", return_value={}), \
             patch.object(common.requests, "post", return_value=accepted), \
             patch.object(common, "poll_long_running_operation", return_value={"done": True}) as poll:
            out = common.powerbi_api_call("POST", "/x")

        self.assertEqual(out, {"done": True})
        self.assertEqual(poll.call_args[0][0], "https://op/1")

    def test_fabric_api_call_still_uses_fabric_base(self):
        """Non-regression: the shared executor must not change api_call's target."""
        resp = _mock_response(200, body={"value": []}, raw=b'{"value":[]}')
        with patch.object(common, "get_headers", return_value={}), \
             patch.object(common.requests, "get", return_value=resp) as mock_get:
            common.api_call("GET", "/workspaces/abc/items")

        self.assertEqual(
            mock_get.call_args[0][0], f"{common.FABRIC_API_BASE}/workspaces/abc/items"
        )

    def test_unsupported_method_raises(self):
        with patch.object(common, "get_powerbi_headers", return_value={}):
            with self.assertRaises(ValueError):
                common.powerbi_api_call("OPTIONS", "/x")


# ─────────────────────────────────────────────────────────────────────────────
# Gap 1 — failover.takeover_semantic_models
# ─────────────────────────────────────────────────────────────────────────────

class TestTakeoverSemanticModels(unittest.TestCase):

    def setUp(self):
        self.logger = _make_logger()

    def test_takes_over_every_semantic_model(self):
        with patch.object(fo.common, "get_items", return_value=FAKE_MODELS), \
             patch.object(fo.common, "powerbi_api_call", return_value={}) as pbi:
            result = fo.takeover_semantic_models(SECONDARY_WS, self.logger)

        self.assertEqual(result["models_found"], 2)
        self.assertEqual(result["models_taken_over"], 2)
        self.assertEqual(result["models_failed"], 0)
        self.assertEqual(pbi.call_count, 2)

    def test_uses_default_takeover_endpoint(self):
        with patch.object(fo.common, "get_items", return_value=FAKE_MODELS), \
             patch.object(fo.common, "powerbi_api_call", return_value={}) as pbi:
            fo.takeover_semantic_models(SECONDARY_WS, self.logger)

        endpoints = [c[0][1] for c in pbi.call_args_list]
        self.assertIn(f"/groups/{SECONDARY_WS}/datasets/sm-id-001/Default.TakeOver", endpoints)
        self.assertIn(f"/groups/{SECONDARY_WS}/datasets/sm-id-002/Default.TakeOver", endpoints)
        self.assertTrue(all(c[0][0] == "POST" for c in pbi.call_args_list))

    def test_only_semantic_models_are_listed(self):
        with patch.object(fo.common, "get_items", return_value=FAKE_MODELS) as items, \
             patch.object(fo.common, "powerbi_api_call", return_value={}):
            fo.takeover_semantic_models(SECONDARY_WS, self.logger)

        self.assertEqual(items.call_args[1]["item_type"], "SemanticModel")

    def test_dry_run_makes_no_calls_but_counts(self):
        with patch.object(fo.common, "get_items", return_value=FAKE_MODELS), \
             patch.object(fo.common, "powerbi_api_call", return_value={}) as pbi:
            result = fo.takeover_semantic_models(SECONDARY_WS, self.logger, dry_run=True)

        pbi.assert_not_called()
        self.assertEqual(result["models_taken_over"], 2)
        self.assertEqual(result["models_failed"], 0)

    def test_per_model_failure_does_not_abort(self):
        def _side_effect(method, endpoint, *a, **kw):
            if "sm-id-001" in endpoint:
                raise Exception("PowerBINotAuthorizedException: owner is disabled")
            return {}

        with patch.object(fo.common, "get_items", return_value=FAKE_MODELS), \
             patch.object(fo.common, "powerbi_api_call", side_effect=_side_effect):
            result = fo.takeover_semantic_models(SECONDARY_WS, self.logger)

        self.assertEqual(result["models_taken_over"], 1)
        self.assertEqual(result["models_failed"], 1)
        self.assertEqual(result["taken_over"], ["Fraud_Model"])
        self.assertEqual(result["failures"][0]["model"], "Claims_Model")

    def test_no_models_is_not_an_error(self):
        with patch.object(fo.common, "get_items", return_value=[]), \
             patch.object(fo.common, "powerbi_api_call", return_value={}) as pbi:
            result = fo.takeover_semantic_models(SECONDARY_WS, self.logger)

        pbi.assert_not_called()
        self.assertEqual(result["models_found"], 0)
        self.assertEqual(result["models_taken_over"], 0)

    def test_listing_failure_returns_empty_result(self):
        with patch.object(fo.common, "get_items", side_effect=Exception("boom")), \
             patch.object(fo.common, "powerbi_api_call", return_value={}) as pbi:
            result = fo.takeover_semantic_models(SECONDARY_WS, self.logger)

        pbi.assert_not_called()
        self.assertEqual(result["models_found"], 0)


# ─────────────────────────────────────────────────────────────────────────────
# Gap 2 — principal resolution
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveServicePrincipalObjectId(unittest.TestCase):

    def setUp(self):
        self.logger = _make_logger()

    def test_uses_object_id_when_set(self):
        with patch.object(sp.common, "SERVICE_PRINCIPAL_OBJECT_ID", PRINCIPAL_ID), \
             patch.object(sp.common, "CLIENT_ID", "client-id-not-this-one"):
            self.assertEqual(sp.resolve_service_principal_object_id(self.logger), PRINCIPAL_ID)

    def test_falls_back_to_client_id_with_warning(self):
        warn_logger = MagicMock()
        with patch.object(sp.common, "SERVICE_PRINCIPAL_OBJECT_ID", ""), \
             patch.object(sp.common, "CLIENT_ID", "client-id-fallback"):
            resolved = sp.resolve_service_principal_object_id(warn_logger)

        self.assertEqual(resolved, "client-id-fallback")
        warn_logger.warning.assert_called_once()
        message = warn_logger.warning.call_args[0][0]
        self.assertIn("SERVICE_PRINCIPAL_OBJECT_ID", message)
        self.assertIn("object ID", message)


# ─────────────────────────────────────────────────────────────────────────────
# Gap 2 — sync_connection_role_assignments
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectionRoleAssignments(unittest.TestCase):

    def setUp(self):
        self.logger = _make_logger()
        self.calls = []

    def _run(self, item_connections, connection_roles, dry_run=False, principal=PRINCIPAL_ID):
        fake = _fake_fabric_api(item_connections, connection_roles, self.calls)
        with patch.object(sp.common, "get_items", return_value=FAKE_ITEMS), \
             patch.object(sp.common, "api_call", side_effect=fake):
            return sp.sync_connection_role_assignments(
                SECONDARY_WS, principal, self.logger, dry_run=dry_run
            )

    def test_deduplicates_shared_connections(self):
        """Three items, two of them sharing CONN_A → two distinct connections."""
        result = self._run(
            item_connections={
                "pipe-id-001": [{"id": CONN_A, "displayName": "SQL_Conn"}],
                "pipe-id-002": [{"id": CONN_A, "displayName": "SQL_Conn"}],
                "flow-id-003": [{"id": CONN_A, "displayName": "SQL_Conn"},
                                {"id": CONN_B, "displayName": "Blob_Conn"}],
            },
            connection_roles={},
        )

        self.assertEqual(result["items_scanned"], 3)
        self.assertEqual(result["connections_discovered"], 2)
        self.assertEqual(sorted(result["connection_ids"]), sorted([CONN_A, CONN_B]))

    def test_shared_connection_granted_only_once(self):
        result = self._run(
            item_connections={
                "pipe-id-001": [{"id": CONN_A}],
                "pipe-id-002": [{"id": CONN_A}],
                "flow-id-003": [{"id": CONN_A}],
            },
            connection_roles={},
        )

        grants = [c for c in _mutating_calls(self.calls) if c[1].endswith("/roleAssignments")]
        self.assertEqual(len(grants), 1)
        self.assertEqual(result["roles_added"], 1)

    def test_grant_payload_shape(self):
        self._run(
            item_connections={"pipe-id-001": [{"id": CONN_A}]},
            connection_roles={},
        )

        grants = _mutating_calls(self.calls)
        self.assertEqual(len(grants), 1)
        method, endpoint, payload = grants[0]
        self.assertEqual(method, "POST")
        self.assertEqual(endpoint, f"/connections/{CONN_A}/roleAssignments")
        self.assertEqual(
            payload,
            {"principal": {"id": PRINCIPAL_ID, "type": "ServicePrincipal"}, "role": "User"},
        )

    def test_skips_when_principal_already_granted(self):
        result = self._run(
            item_connections={"pipe-id-001": [{"id": CONN_A}]},
            connection_roles={
                CONN_A: [{"principal": {"id": PRINCIPAL_ID, "type": "ServicePrincipal"},
                          "role": "User"}]
            },
        )

        self.assertEqual(result["roles_unchanged"], 1)
        self.assertEqual(result["roles_added"], 0)
        self.assertEqual(_mutating_calls(self.calls), [])
        self.assertEqual(result["delta_details"][0]["action"], "skip")

    def test_grants_when_only_other_principals_present(self):
        result = self._run(
            item_connections={"pipe-id-001": [{"id": CONN_A}]},
            connection_roles={
                CONN_A: [{"principal": {"id": "someone-else", "type": "User"}, "role": "Owner"}]
            },
        )

        self.assertEqual(result["roles_added"], 1)
        self.assertEqual(result["roles_unchanged"], 0)
        self.assertEqual(len(_mutating_calls(self.calls)), 1)

    def test_mixed_skip_and_add(self):
        result = self._run(
            item_connections={
                "pipe-id-001": [{"id": CONN_A}],
                "flow-id-003": [{"id": CONN_B}],
            },
            connection_roles={
                CONN_A: [{"principal": {"id": PRINCIPAL_ID}, "role": "UserWithReshare"}],
            },
        )

        self.assertEqual(result["roles_unchanged"], 1)
        self.assertEqual(result["roles_added"], 1)
        grants = _mutating_calls(self.calls)
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0][1], f"/connections/{CONN_B}/roleAssignments")

    def test_dry_run_makes_zero_mutating_calls(self):
        result = self._run(
            item_connections={
                "pipe-id-001": [{"id": CONN_A}],
                "flow-id-003": [{"id": CONN_B}],
            },
            connection_roles={},
            dry_run=True,
        )

        self.assertEqual(_mutating_calls(self.calls), [])
        self.assertEqual(result["roles_added"], 2)
        self.assertEqual(result["roles_failed"], 0)

    def test_item_connection_failure_is_tolerated(self):
        """A genuine error on one item must not abort the remaining items."""
        result = self._run(
            item_connections={
                "pipe-id-001": common.FabricApiError(500, "InternalServerError"),
                "pipe-id-002": [{"id": CONN_A}],
                "flow-id-003": [{"id": CONN_B}],
            },
            connection_roles={},
        )

        self.assertEqual(result["items_failed"], 1)
        self.assertEqual(result["items_scanned"], 2)
        self.assertEqual(result["connections_discovered"], 2)
        self.assertEqual(result["roles_added"], 2)

    def test_role_assignment_read_failure_is_tolerated(self):
        result = self._run(
            item_connections={
                "pipe-id-001": [{"id": CONN_A}],
                "flow-id-003": [{"id": CONN_B}],
            },
            connection_roles={CONN_A: Exception("Forbidden")},
        )

        self.assertEqual(result["roles_failed"], 1)
        self.assertEqual(result["roles_added"], 1)

    def test_grant_failure_is_counted_not_raised(self):
        def _fake(method, endpoint, payload=None, *a, **kw):
            self.calls.append((method, endpoint, payload))
            if method == "GET" and endpoint.endswith("/connections"):
                return {"value": [{"id": CONN_A}]}
            if method == "GET" and endpoint.endswith("/roleAssignments"):
                return {"value": []}
            raise Exception("PrincipalNotFound: object id is invalid")

        with patch.object(sp.common, "get_items", return_value=[FAKE_ITEMS[0]]), \
             patch.object(sp.common, "api_call", side_effect=_fake):
            result = sp.sync_connection_role_assignments(
                SECONDARY_WS, PRINCIPAL_ID, self.logger
            )

        self.assertEqual(result["roles_failed"], 1)
        self.assertEqual(result["roles_added"], 0)

    def test_missing_principal_short_circuits(self):
        with patch.object(sp.common, "get_items", return_value=FAKE_ITEMS) as items, \
             patch.object(sp.common, "api_call") as api:
            result = sp.sync_connection_role_assignments(SECONDARY_WS, "", self.logger)

        items.assert_not_called()
        api.assert_not_called()
        self.assertEqual(result["connections_discovered"], 0)
        self.assertEqual(result["roles_added"], 0)

    def test_items_without_connections_are_scanned_not_failed(self):
        result = self._run(item_connections={}, connection_roles={})

        self.assertEqual(result["items_scanned"], 3)
        self.assertEqual(result["items_failed"], 0)
        self.assertEqual(result["items_without_connections"], 3)
        self.assertEqual(result["connections_discovered"], 0)
        self.assertEqual(_mutating_calls(self.calls), [])

    def test_custom_role_is_honoured(self):
        fake = _fake_fabric_api({"pipe-id-001": [{"id": CONN_A}]}, {}, self.calls)
        with patch.object(sp.common, "get_items", return_value=[FAKE_ITEMS[0]]), \
             patch.object(sp.common, "api_call", side_effect=fake):
            sp.sync_connection_role_assignments(
                SECONDARY_WS, PRINCIPAL_ID, self.logger, role="Owner"
            )

        payload = _mutating_calls(self.calls)[0][2]
        self.assertEqual(payload["role"], "Owner")


class TestUnsupportedItemClassification(unittest.TestCase):
    """
    Item types with no connections endpoint are expected noise, not failures.
    A DR summary that reports them as failures would push an operator to abort
    a healthy failover.
    """

    def setUp(self):
        self.logger = _make_logger()
        self.calls = []

    def _run(self, item_connections, connection_roles=None):
        fake = _fake_fabric_api(item_connections, connection_roles or {}, self.calls)
        with patch.object(sp.common, "get_items", return_value=FAKE_ITEMS), \
             patch.object(sp.common, "api_call", side_effect=fake):
            return sp.sync_connection_role_assignments(
                SECONDARY_WS, PRINCIPAL_ID, self.logger
            )

    # ── classifier ───────────────────────────────────────────────────────

    def test_404_status_is_no_connections(self):
        self.assertTrue(sp._is_no_connections_error(common.FabricApiError(404, "NotFound")))

    def test_404_in_message_without_status_attr_is_no_connections(self):
        self.assertTrue(sp._is_no_connections_error(Exception("API error 404: EntityNotFound")))

    def test_unsupported_item_type_is_no_connections(self):
        for text in ("UnsupportedItemType", "ItemTypeNotSupported",
                     "Operation not supported for this item"):
            with self.subTest(text=text):
                self.assertTrue(sp._is_no_connections_error(Exception(text)))

    def test_unsupported_message_on_400_is_no_connections(self):
        self.assertTrue(
            sp._is_no_connections_error(common.FabricApiError(400, "UnsupportedItemType"))
        )

    def test_server_error_is_a_genuine_failure(self):
        self.assertFalse(sp._is_no_connections_error(common.FabricApiError(500, "InternalServerError")))

    def test_auth_error_is_a_genuine_failure(self):
        self.assertFalse(sp._is_no_connections_error(common.FabricApiError(401, "Unauthorized")))
        self.assertFalse(sp._is_no_connections_error(common.FabricApiError(403, "InsufficientPrivileges")))

    def test_retry_exhaustion_is_a_genuine_failure(self):
        self.assertFalse(sp._is_no_connections_error(Exception("Max retries exceeded for rate limit")))
        self.assertFalse(sp._is_no_connections_error(Exception("Service unavailable after retries")))

    # ── end-to-end counter behaviour ─────────────────────────────────────

    def test_404_item_does_not_inflate_items_failed(self):
        """404 on one of three items: no failures, others still processed."""
        result = self._run({
            "pipe-id-001": common.FabricApiError(404, "EntityNotFound"),
            "pipe-id-002": [{"id": CONN_A}],
            "flow-id-003": [{"id": CONN_B}],
        })

        self.assertEqual(result["items_failed"], 0)
        self.assertEqual(result["items_scanned"], 3)
        self.assertEqual(result["items_without_connections"], 1)
        # The remaining items' connections were still discovered and granted
        self.assertEqual(result["connections_discovered"], 2)
        self.assertEqual(sorted(result["connection_ids"]), sorted([CONN_A, CONN_B]))
        self.assertEqual(result["roles_added"], 2)
        self.assertEqual(len(_mutating_calls(self.calls)), 2)

    def test_all_items_unsupported_reports_zero_failures(self):
        result = self._run({
            "pipe-id-001": common.FabricApiError(404, "EntityNotFound"),
            "pipe-id-002": Exception("UnsupportedItemType"),
            "flow-id-003": common.FabricApiError(404, "EntityNotFound"),
        })

        self.assertEqual(result["items_failed"], 0)
        self.assertEqual(result["items_scanned"], 3)
        self.assertEqual(result["items_without_connections"], 3)
        self.assertEqual(result["connections_discovered"], 0)

    def test_genuine_and_expected_errors_are_counted_separately(self):
        result = self._run({
            "pipe-id-001": common.FabricApiError(404, "EntityNotFound"),
            "pipe-id-002": common.FabricApiError(503, "ServiceUnavailable"),
            "flow-id-003": [{"id": CONN_A}],
        })

        self.assertEqual(result["items_failed"], 1)
        self.assertEqual(result["items_without_connections"], 1)
        self.assertEqual(result["items_scanned"], 2)
        self.assertEqual(result["roles_added"], 1)

    def test_empty_connection_list_counts_as_without_connections(self):
        result = self._run({
            "pipe-id-001": [],
            "pipe-id-002": [{"id": CONN_A}],
            "flow-id-003": [],
        })

        self.assertEqual(result["items_failed"], 0)
        self.assertEqual(result["items_scanned"], 3)
        self.assertEqual(result["items_without_connections"], 2)
        self.assertEqual(result["connections_discovered"], 1)


class TestFabricApiError(unittest.TestCase):
    """FabricApiError must carry the status code without changing str(e)."""

    def test_is_an_exception_subclass(self):
        self.assertTrue(issubclass(common.FabricApiError, Exception))

    def test_message_format_unchanged(self):
        err = common.FabricApiError(403, {"error": "denied"})
        self.assertEqual(str(err), "API error 403: {'error': 'denied'}")

    def test_exposes_status_code_and_detail(self):
        err = common.FabricApiError(429, "TooManyRequests")
        self.assertEqual(err.status_code, 429)
        self.assertEqual(err.detail, "TooManyRequests")

    def test_api_call_raises_fabric_api_error_catchable_as_exception(self):
        resp = _mock_response(404, body={"error": "EntityNotFound"},
                              raw=b'{"error":"EntityNotFound"}')
        with patch.object(common, "get_headers", return_value={}), \
             patch.object(common.requests, "get", return_value=resp):
            with self.assertRaises(Exception) as ctx:
                common.api_call("GET", "/workspaces/x/items/y/connections")

        self.assertIsInstance(ctx.exception, common.FabricApiError)
        self.assertEqual(ctx.exception.status_code, 404)
        # Non-regression: existing substring matching on the message still works
        self.assertIn("API error 404", str(ctx.exception))
        self.assertIn("EntityNotFound", str(ctx.exception))


class TestConnectionPagination(unittest.TestCase):
    """_paged_get must follow continuation tokens."""

    def test_follows_continuation_token(self):
        pages = [
            {"value": [{"id": CONN_A}], "continuationToken": "tok-1"},
            {"value": [{"id": CONN_B}]},
        ]
        with patch.object(sp.common, "api_call", side_effect=pages) as api:
            out = sp._paged_get("/connections/x/roleAssignments")

        self.assertEqual(out, [{"id": CONN_A}, {"id": CONN_B}])
        self.assertIn("continuationToken=tok-1", api.call_args_list[1][0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
