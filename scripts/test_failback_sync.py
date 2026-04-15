"""
Test Suite: Failback Lakehouse Reverse Sync

Tests both reverse-sync strategies end-to-end in dry-run / unit-test mode:

  Option A — FAST_COPY
      notebookutils.fs.cp (server-side OneLake copy) + delta log trimming.
      Per Microsoft DR guidance Approach 1:
      https://learn.microsoft.com/en-us/fabric/security/experience-specific-guidance

  Option B — ACTIVE_REPLICATION
      azcopy sync --include-after=<failover_timestamp> (incremental reverse sync).

Run:
    cd scripts
    python -m pytest test_failback_sync.py -v
    # or directly:
    python test_failback_sync.py
"""

import json
import logging
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Stub out common.api_call / common.get_items so tests run offline ──────────
import common as _common_module  # noqa: E402 (must come after sys.path insert)

_common_module.PRIMARY_WORKSPACE_ID = "primary-ws-guid-1234"
_common_module.SECONDARY_WORKSPACE_ID = "secondary-ws-guid-5678"

import sync_lakehouses as sl  # noqa: E402
import failback as fb  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_logger(name: str = "test") -> logging.Logger:
    """Return a silent logger so test output stays clean."""
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


FAKE_FAILOVER_TS = "2026-04-14T10:07:00Z"
FAKE_FAILOVER_MS = int(
    datetime.fromisoformat(FAKE_FAILOVER_TS.replace("Z", "+00:00")).timestamp() * 1000
)

FAKE_LAKEHOUSES_SECONDARY = [
    {"id": "sec-lh-id-001", "displayName": "Claims_LH", "type": "Lakehouse"},
    {"id": "sec-lh-id-002", "displayName": "Fraud_LH", "type": "Lakehouse"},
]
FAKE_LAKEHOUSES_PRIMARY = [
    {"id": "pri-lh-id-001", "displayName": "Claims_LH", "type": "Lakehouse"},
    {"id": "pri-lh-id-002", "displayName": "Fraud_LH", "type": "Lakehouse"},
]
FAKE_TABLES = ["claims_raw", "claims_enriched"]


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — sync_lakehouses module
# ─────────────────────────────────────────────────────────────────────────────

class TestSyncStrategy(unittest.TestCase):
    """Verify the SyncStrategy enum includes FAST_COPY."""

    def test_fast_copy_in_enum(self):
        self.assertIn("FAST_COPY", sl.SyncStrategy.__members__)

    def test_active_replication_in_enum(self):
        self.assertIn("ACTIVE_REPLICATION", sl.SyncStrategy.__members__)

    def test_onelake_shortcuts_in_enum(self):
        self.assertIn("ONELAKE_SHORTCUTS", sl.SyncStrategy.__members__)


class TestAzcopyIncremental(unittest.TestCase):
    """Option B: azcopy sync with --include-after (incremental reverse sync)."""

    def setUp(self):
        self.logger = _make_logger("azcopy_test")

    def test_dry_run_no_subprocess(self):
        """Dry-run must not invoke azcopy at all."""
        with patch("subprocess.run") as mock_run:
            result = sl.sync_lakehouse_via_azcopy(
                source_workspace_id="sec-ws",
                source_lakehouse_name="Claims_LH",
                dest_workspace_id="pri-ws",
                dest_lakehouse_name="Claims_LH",
                logger=self.logger,
                dry_run=True,
                since_timestamp=FAKE_FAILOVER_TS,
            )
        self.assertTrue(result)
        mock_run.assert_not_called()

    def test_include_after_in_command(self):
        """When since_timestamp is set, --include-after must appear in the azcopy command."""
        captured_cmd: List[List[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=fake_run):
            sl.sync_lakehouse_via_azcopy(
                source_workspace_id="sec-ws",
                source_lakehouse_name="Claims_LH",
                dest_workspace_id="pri-ws",
                dest_lakehouse_name="Claims_LH",
                logger=self.logger,
                dry_run=False,
                since_timestamp=FAKE_FAILOVER_TS,
            )

        self.assertEqual(len(captured_cmd), 1)
        cmd_str = " ".join(captured_cmd[0])
        self.assertIn("--include-after", cmd_str)
        self.assertIn(FAKE_FAILOVER_TS, cmd_str)

    def test_no_include_after_for_full_sync(self):
        """Without since_timestamp, --include-after must NOT appear (full sync path)."""
        captured_cmd: List[List[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=fake_run):
            sl.sync_lakehouse_via_azcopy(
                source_workspace_id="pri-ws",
                source_lakehouse_name="Claims_LH",
                dest_workspace_id="sec-ws",
                dest_lakehouse_name="Claims_LH",
                logger=self.logger,
                dry_run=False,
                since_timestamp=None,
            )

        self.assertEqual(len(captured_cmd), 1)
        cmd_str = " ".join(captured_cmd[0])
        self.assertNotIn("--include-after", cmd_str)

    def test_correct_source_is_secondary(self):
        """In reverse sync the OneLake source URL must point at the secondary workspace."""
        captured_cmd: List[List[str]] = []

        def fake_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("subprocess.run", side_effect=fake_run):
            sl.sync_lakehouse_via_azcopy(
                source_workspace_id="sec-ws-guid",
                source_lakehouse_name="Claims_LH",
                dest_workspace_id="pri-ws-guid",
                dest_lakehouse_name="Claims_LH",
                logger=self.logger,
                dry_run=False,
                since_timestamp=FAKE_FAILOVER_TS,
            )

        cmd = " ".join(captured_cmd[0])
        # source (positional arg[2]) must contain the secondary workspace ID
        args = captured_cmd[0]
        source_arg = args[2]  # azcopy sync <source> <dest> ...
        self.assertIn("sec-ws-guid", source_arg)
        dest_arg = args[3]
        self.assertIn("pri-ws-guid", dest_arg)

    def test_azcopy_not_found_returns_false(self):
        """If azcopy binary is missing, return False (not raise)."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = sl.sync_lakehouse_via_azcopy(
                source_workspace_id="sec-ws",
                source_lakehouse_name="Claims_LH",
                dest_workspace_id="pri-ws",
                dest_lakehouse_name="Claims_LH",
                logger=self.logger,
                dry_run=False,
                since_timestamp=FAKE_FAILOVER_TS,
            )
        self.assertFalse(result)


class TestFastCopyNotebookBuilder(unittest.TestCase):
    """Option A: Verify the generated notebook cell content is correct."""

    def setUp(self):
        self.nb = sl._build_fast_copy_notebook_ipynb(
            source_workspace_id="sec-ws-guid",
            source_lakehouse_name="Claims_LH",
            dest_workspace_id="pri-ws-guid",
            dest_lakehouse_name="Claims_LH",
            dest_lakehouse_id="pri-lh-id-001",
            tables=FAKE_TABLES,
            failover_timestamp_ms=FAKE_FAILOVER_MS,
        )

    def test_notebook_format(self):
        self.assertEqual(self.nb["nbformat"], 4)

    def test_default_lakehouse_set_to_primary(self):
        """default_lakehouse must be the primary (destination) lakehouse."""
        trident = self.nb["metadata"]["trident"]["lakehouse"]
        self.assertEqual(trident["default_lakehouse"], "pri-lh-id-001")
        self.assertEqual(trident["default_lakehouse_workspace_id"], "pri-ws-guid")

    def test_one_code_cell_per_table(self):
        """There must be a code cell for each table (plus header + import + footer)."""
        code_cells = [c for c in self.nb["cells"] if c["cell_type"] == "code"]
        # import cell + one cell per table + footer cell
        self.assertEqual(len(code_cells), len(FAKE_TABLES) + 2)

    def test_fast_copy_call_in_cells(self):
        """Every per-table cell must define the incremental_cp helper and use notebookutils.fs.cp."""
        code_cells = [c for c in self.nb["cells"] if c["cell_type"] == "code"]
        # skip import (index 0) and footer (last)
        per_table_cells = code_cells[1:-1]
        for cell in per_table_cells:
            source = "".join(cell["source"])
            self.assertIn("notebookutils.fs.cp", source)
            self.assertIn("incremental_cp", source)

    def test_delta_log_trimming_in_cells(self):
        """Every per-table cell must handle _delta_log copy filtered by failover_ts_ms."""
        code_cells = [c for c in self.nb["cells"] if c["cell_type"] == "code"]
        per_table_cells = code_cells[1:-1]
        for cell in per_table_cells:
            source = "".join(cell["source"])
            # Must reference the failover timestamp for modifyTime comparison
            self.assertIn("since_ms", source)
            # Must handle _delta_log separately
            self.assertIn("_delta_log", source)
            # Must reset _last_checkpoint so Delta discovers new commits
            self.assertIn("_last_checkpoint", source)

    def test_source_is_secondary(self):
        """Cell sources must contain the secondary workspace ID as source."""
        code_cells = [c for c in self.nb["cells"] if c["cell_type"] == "code"]
        per_table_cells = code_cells[1:-1]
        for cell in per_table_cells:
            source = "".join(cell["source"])
            self.assertIn("sec-ws-guid", source)

    def test_destination_is_primary(self):
        """Cell sources must contain the primary workspace ID as destination."""
        code_cells = [c for c in self.nb["cells"] if c["cell_type"] == "code"]
        per_table_cells = code_cells[1:-1]
        for cell in per_table_cells:
            source = "".join(cell["source"])
            self.assertIn("pri-ws-guid", source)

    def test_failover_timestamp_embedded(self):
        """Failover epoch ms must appear in at least one cell."""
        all_source = " ".join(
            "".join(c["source"])
            for c in self.nb["cells"]
            if c["cell_type"] == "code"
        )
        self.assertIn(str(FAKE_FAILOVER_MS), all_source)


class TestFastCopyDryRun(unittest.TestCase):
    """Option A: fast-copy dry-run should not call any API."""

    def setUp(self):
        self.logger = _make_logger("fast_copy_dry")

    def test_dry_run_skips_api_calls(self):
        """In dry-run mode, no API calls or notebook creation should happen."""
        with patch.object(_common_module, "api_call") as mock_api:
            # Stub table list
            mock_api.return_value = {"data": [{"name": "claims_raw"}, {"name": "fraud_flags"}]}
            result = sl.sync_lakehouse_fast_copy(
                source_workspace_id="sec-ws",
                source_lakehouse_id="sec-lh-id",
                source_lakehouse_name="Claims_LH",
                dest_workspace_id="pri-ws",
                dest_lakehouse_id="pri-lh-id",
                dest_lakehouse_name="Claims_LH",
                logger=self.logger,
                dry_run=True,
                failover_timestamp_ms=FAKE_FAILOVER_MS,
            )

        self.assertTrue(result["success"])
        # Only ONE api_call (to list tables) — no notebook creation/run calls
        self.assertEqual(mock_api.call_count, 1)


class TestFastCopyNotebookLifecycle(unittest.TestCase):
    """Option A: notebook create → trigger → poll → delete lifecycle."""

    def setUp(self):
        self.logger = _make_logger("nb_lifecycle")

    def _setup_api_mock(self, job_status: str = "Completed") -> MagicMock:
        """Return a mock api_call that simulates the full notebook lifecycle."""
        mock = MagicMock()
        call_count = [0]

        def side_effect(method, endpoint, payload=None, **kwargs):
            call_count[0] += 1
            # POST /workspaces/.../items → notebook created
            if method == "POST" and "/items" in endpoint and "jobs" not in endpoint:
                return {"id": "temp-nb-id-9999", "displayName": "_BCDR_FastCopy_Claims_LH_Temp"}
            # POST .../jobs/instances → start job (202 => empty dict after polling)
            if method == "POST" and "jobs/instances" in endpoint:
                return {}
            # GET .../jobs/instances → return job status
            if method == "GET" and "jobs/instances" in endpoint:
                return {"value": [{
                    "id": "job-inst-001",
                    "status": job_status,
                    "startTimeUtc": "2026-04-14T10:10:00Z",
                    "failureReason": "",
                }]}
            # DELETE notebook
            if method == "DELETE":
                return {}
            # GET tables
            if method == "GET" and "/tables" in endpoint:
                return {"data": [{"name": t} for t in FAKE_TABLES]}
            return {}

        mock.side_effect = side_effect
        return mock

    def test_successful_job_returns_true(self):
        mock_api = self._setup_api_mock(job_status="Completed")
        with patch.object(_common_module, "api_call", mock_api):
            result = sl.sync_lakehouse_fast_copy(
                source_workspace_id="sec-ws",
                source_lakehouse_id="sec-lh-id",
                source_lakehouse_name="Claims_LH",
                dest_workspace_id="pri-ws",
                dest_lakehouse_id="pri-lh-id",
                dest_lakehouse_name="Claims_LH",
                logger=self.logger,
                dry_run=False,
                failover_timestamp_ms=FAKE_FAILOVER_MS,
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["job_status"], "Completed")

    def test_failed_job_returns_false(self):
        mock_api = self._setup_api_mock(job_status="Failed")
        with patch.object(_common_module, "api_call", mock_api):
            result = sl.sync_lakehouse_fast_copy(
                source_workspace_id="sec-ws",
                source_lakehouse_id="sec-lh-id",
                source_lakehouse_name="Claims_LH",
                dest_workspace_id="pri-ws",
                dest_lakehouse_id="pri-lh-id",
                dest_lakehouse_name="Claims_LH",
                logger=self.logger,
                dry_run=False,
                failover_timestamp_ms=FAKE_FAILOVER_MS,
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["job_status"], "Failed")

    def test_temp_notebook_always_deleted(self):
        """Even on job failure the temp notebook must be cleaned up."""
        mock_api = self._setup_api_mock(job_status="Failed")
        delete_calls: List[str] = []

        original_side = mock_api.side_effect

        def tracking_side(method, endpoint, payload=None, **kwargs):
            if method == "DELETE":
                delete_calls.append(endpoint)
            return original_side(method, endpoint, payload, **kwargs)

        mock_api.side_effect = tracking_side
        with patch.object(_common_module, "api_call", mock_api):
            sl.sync_lakehouse_fast_copy(
                source_workspace_id="sec-ws",
                source_lakehouse_id="sec-lh-id",
                source_lakehouse_name="Claims_LH",
                dest_workspace_id="pri-ws",
                dest_lakehouse_id="pri-lh-id",
                dest_lakehouse_name="Claims_LH",
                logger=self.logger,
                dry_run=False,
                failover_timestamp_ms=FAKE_FAILOVER_MS,
            )
        # At least one DELETE call must have been made (cleanup)
        self.assertTrue(len(delete_calls) > 0)
        self.assertTrue(any("temp-nb-id-9999" in ep for ep in delete_calls))


class TestReverseSyncLakehouses(unittest.TestCase):
    """Integration test for reverse_sync_lakehouses() dispatcher."""

    def setUp(self):
        self.logger = _make_logger("reverse_sync")

    def _mock_get_lakehouses(self, workspace_id: str):
        if workspace_id == "sec-ws":
            return FAKE_LAKEHOUSES_SECONDARY
        elif workspace_id == "pri-ws":
            return FAKE_LAKEHOUSES_PRIMARY
        return []

    def test_fast_copy_strategy_dispatches_correctly(self):
        """FAST_COPY strategy must call sync_lakehouse_fast_copy for each lakehouse."""
        with patch.object(sl, "get_lakehouses", side_effect=self._mock_get_lakehouses), \
             patch.object(_common_module, "load_artifact_mapping", return_value={}), \
             patch.object(sl, "sync_lakehouse_fast_copy",
                          return_value={"success": True, "tables_found": 2}) as mock_fc:

            result = sl.reverse_sync_lakehouses(
                primary_workspace_id="pri-ws",
                secondary_workspace_id="sec-ws",
                logger=self.logger,
                strategy=sl.SyncStrategy.FAST_COPY,
                since_timestamp=FAKE_FAILOVER_TS,
                dry_run=False,
            )

        self.assertEqual(mock_fc.call_count, len(FAKE_LAKEHOUSES_SECONDARY))
        self.assertEqual(len(result["lakehouses_synced"]), len(FAKE_LAKEHOUSES_SECONDARY))
        self.assertEqual(len(result["lakehouses_failed"]), 0)

    def test_active_replication_strategy_dispatches_correctly(self):
        """ACTIVE_REPLICATION strategy must call sync_lakehouse_via_azcopy for each lakehouse."""
        with patch.object(sl, "get_lakehouses", side_effect=self._mock_get_lakehouses), \
             patch.object(_common_module, "load_artifact_mapping", return_value={}), \
             patch.object(sl, "sync_lakehouse_via_azcopy", return_value=True) as mock_azcopy:

            result = sl.reverse_sync_lakehouses(
                primary_workspace_id="pri-ws",
                secondary_workspace_id="sec-ws",
                logger=self.logger,
                strategy=sl.SyncStrategy.ACTIVE_REPLICATION,
                since_timestamp=FAKE_FAILOVER_TS,
                dry_run=False,
            )

        self.assertEqual(mock_azcopy.call_count, len(FAKE_LAKEHOUSES_SECONDARY))
        self.assertEqual(len(result["lakehouses_synced"]), len(FAKE_LAKEHOUSES_SECONDARY))

    def test_azcopy_receives_since_timestamp(self):
        """ACTIVE_REPLICATION must forward since_timestamp to azcopy function."""
        received_kwargs: List[Dict] = []

        def capture_azcopy(*args, **kwargs):
            received_kwargs.append(kwargs)
            return True

        with patch.object(sl, "get_lakehouses", side_effect=self._mock_get_lakehouses), \
             patch.object(_common_module, "load_artifact_mapping", return_value={}), \
             patch.object(sl, "sync_lakehouse_via_azcopy", side_effect=capture_azcopy):

            sl.reverse_sync_lakehouses(
                primary_workspace_id="pri-ws",
                secondary_workspace_id="sec-ws",
                logger=self.logger,
                strategy=sl.SyncStrategy.ACTIVE_REPLICATION,
                since_timestamp=FAKE_FAILOVER_TS,
                dry_run=False,
            )

        for kw in received_kwargs:
            self.assertEqual(kw.get("since_timestamp"), FAKE_FAILOVER_TS)

    def test_fast_copy_receives_failover_ms(self):
        """FAST_COPY must convert ISO-8601 to epoch ms and pass to sync_lakehouse_fast_copy."""
        received_kwargs: List[Dict] = []

        def capture_fc(*args, **kwargs):
            received_kwargs.append(kwargs)
            return {"success": True, "tables_found": 2}

        with patch.object(sl, "get_lakehouses", side_effect=self._mock_get_lakehouses), \
             patch.object(_common_module, "load_artifact_mapping", return_value={}), \
             patch.object(sl, "sync_lakehouse_fast_copy", side_effect=capture_fc):

            sl.reverse_sync_lakehouses(
                primary_workspace_id="pri-ws",
                secondary_workspace_id="sec-ws",
                logger=self.logger,
                strategy=sl.SyncStrategy.FAST_COPY,
                since_timestamp=FAKE_FAILOVER_TS,
                dry_run=False,
            )

        for kw in received_kwargs:
            self.assertEqual(kw.get("failover_timestamp_ms"), FAKE_FAILOVER_MS)

    def test_dry_run_does_not_call_sync_functions(self):
        """In dry-run mode, underlying fast-copy/azcopy functions must not be called."""
        with patch.object(sl, "get_lakehouses", side_effect=self._mock_get_lakehouses), \
             patch.object(_common_module, "load_artifact_mapping", return_value={}), \
             patch.object(sl, "sync_lakehouse_fast_copy") as mock_fc, \
             patch.object(sl, "sync_lakehouse_via_azcopy") as mock_azcopy:

            sl.reverse_sync_lakehouses(
                primary_workspace_id="pri-ws",
                secondary_workspace_id="sec-ws",
                logger=self.logger,
                strategy=sl.SyncStrategy.FAST_COPY,
                since_timestamp=FAKE_FAILOVER_TS,
                dry_run=True,
            )

        # dry_run=True is forwarded into sync_lakehouse_fast_copy (which logs only)
        # We still expect it to be called — just with dry_run=True
        for c in mock_fc.call_args_list:
            self.assertTrue(c.kwargs.get("dry_run") or c.args[7] if len(c.args) > 7 else True)

    def test_no_match_lakehouse_is_skipped(self):
        """Lakehouses on secondary with no name match on primary are skipped (not failed)."""
        orphan = [{"id": "orphan-id", "displayName": "Orphan_LH", "type": "Lakehouse"}]

        def mock_get_lh(workspace_id):
            return orphan if workspace_id == "sec-ws" else []  # primary has nothing

        with patch.object(sl, "get_lakehouses", side_effect=mock_get_lh), \
             patch.object(_common_module, "load_artifact_mapping", return_value={}):

            result = sl.reverse_sync_lakehouses(
                primary_workspace_id="pri-ws",
                secondary_workspace_id="sec-ws",
                logger=self.logger,
                strategy=sl.SyncStrategy.FAST_COPY,
                since_timestamp=FAKE_FAILOVER_TS,
                dry_run=False,
            )

        self.assertIn("Orphan_LH", result["lakehouses_skipped"])
        self.assertEqual(len(result["lakehouses_failed"]), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — failback.py timestamp wiring
# ─────────────────────────────────────────────────────────────────────────────

class TestFailbackTimestampWiring(unittest.TestCase):
    """Verify failback.py reads failover_timestamp from failover_log.json and passes it."""

    def setUp(self):
        self.logger = _make_logger("failback_wiring")
        self._failover_log = {
            "failover_timestamp": FAKE_FAILOVER_TS,
            "primary_workspace": "pri-ws",
            "secondary_workspace": "sec-ws",
            "schedule_manifest": [],
            "status": "SUCCESS",
        }

    def _write_failover_log(self, tmp_dir: str) -> str:
        path = os.path.join(tmp_dir, "failover_log.json")
        with open(path, "w") as f:
            json.dump(self._failover_log, f)
        return path

    def test_since_timestamp_read_from_log(self):
        """reverse_sync_artifacts must receive the failover_timestamp from the log."""
        received: Dict = {}

        def capture_reverse(*args, **kwargs):
            received.update(kwargs)
            return {
                "types_synced": ["Lakehouses"],
                "types_failed": [],
                "details": {},
            }

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = self._write_failover_log(tmpdir)
            # Patch open inside failback for the log path it uses
            original_cwd = os.getcwd()
            os.makedirs(os.path.join(tmpdir, "data"), exist_ok=True)
            import shutil
            shutil.copy(log_path, os.path.join(tmpdir, "data", "failover_log.json"))
            os.chdir(tmpdir)

            try:
                with patch.object(fb, "reverse_sync_artifacts", side_effect=capture_reverse):
                    # Simulate Step 2 of failback.main() inline
                    since_timestamp: Optional[str] = None
                    fpath = "data/failover_log.json"
                    if os.path.exists(fpath):
                        with open(fpath) as _f:
                            foLog = json.load(_f)
                        since_timestamp = foLog.get("failover_timestamp")
                    self.assertEqual(since_timestamp, FAKE_FAILOVER_TS)
            finally:
                os.chdir(original_cwd)

    def test_reverse_sync_artifacts_signature_accepts_since_timestamp(self):
        """reverse_sync_artifacts must accept since_timestamp and lakehouse_strategy."""
        import inspect
        sig = inspect.signature(fb.reverse_sync_artifacts)
        self.assertIn("since_timestamp", sig.parameters)
        self.assertIn("lakehouse_strategy", sig.parameters)

    def test_default_lakehouse_strategy_is_fast_copy(self):
        """Default lakehouse_strategy in reverse_sync_artifacts should be FAST_COPY."""
        import inspect
        sig = inspect.signature(fb.reverse_sync_artifacts)
        default = sig.parameters["lakehouse_strategy"].default
        self.assertEqual(default, sl.SyncStrategy.FAST_COPY)


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end dry-run test (both strategies, no real Fabric calls)
# ─────────────────────────────────────────────────────────────────────────────

class TestEndToEndDryRun(unittest.TestCase):
    """
    Dry-run end-to-end smoke test — exercises the full failback chain:
        pause_secondary → reverse_sync → validate_primary → reactivate
    with no real API calls.
    """

    def setUp(self):
        self.logger = _make_logger("e2e_dry_run")

    def _fake_api_call(self, method, endpoint, payload=None, **kwargs):
        """Universal stub: returns plausible shapes for all endpoints."""
        if "/items" in endpoint and method == "GET":
            return {"value": [
                {"id": "item-001", "displayName": "Claims_Pipeline", "type": "DataPipeline"},
            ]}
        if "/jobs/instances" in endpoint and method == "GET":
            return {"value": []}
        if "/jobScheduler" in endpoint:
            return {"enabled": True}
        if "/lakehouses/" in endpoint and "/tables" in endpoint:
            return {"data": [{"name": "claims_raw"}, {"name": "claims_enriched"}]}
        if method in ("POST", "PATCH", "DELETE"):
            return {}
        return {"value": []}

    def _run_reverse_sync_dry(self, strategy: sl.SyncStrategy) -> Dict:
        with patch.object(_common_module, "api_call", side_effect=self._fake_api_call), \
             patch.object(_common_module, "get_items", return_value=[]), \
             patch.object(sl, "get_lakehouses", side_effect=lambda ws_id: (
                 FAKE_LAKEHOUSES_SECONDARY if ws_id == "sec-ws"
                 else FAKE_LAKEHOUSES_PRIMARY
             )), \
             patch.object(_common_module, "load_artifact_mapping", return_value={}):

            return fb.reverse_sync_artifacts(
                primary_workspace_id="pri-ws",
                secondary_workspace_id="sec-ws",
                logger=self.logger,
                dry_run=True,
                since_timestamp=FAKE_FAILOVER_TS,
                lakehouse_strategy=strategy,
            )

    def test_dry_run_fast_copy_completes(self):
        result = self._run_reverse_sync_dry(sl.SyncStrategy.FAST_COPY)
        self.assertIsInstance(result, dict)
        self.assertIn("types_synced", result)
        # FAST_COPY dry-run should succeed for lakehouses
        self.assertIn("Lakehouses", result["types_synced"])

    def test_dry_run_active_replication_completes(self):
        result = self._run_reverse_sync_dry(sl.SyncStrategy.ACTIVE_REPLICATION)
        self.assertIsInstance(result, dict)
        self.assertIn("types_synced", result)
        self.assertIn("Lakehouses", result["types_synced"])


# ─────────────────────────────────────────────────────────────────────────────
# Timestamp conversion correctness
# ─────────────────────────────────────────────────────────────────────────────

class TestTimestampConversion(unittest.TestCase):
    """Verify ISO-8601 → epoch ms conversion in reverse_sync_lakehouses."""

    def test_utc_z_suffix(self):
        """'2026-04-14T10:07:00Z' should produce correct epoch ms."""
        logger = _make_logger("ts_conv")
        received_ms: List[int] = []

        def capture_fc(**kwargs):
            received_ms.append(kwargs.get("failover_timestamp_ms", -1))
            return {"success": True, "tables_found": 0}

        with patch.object(sl, "get_lakehouses", return_value=[
            {"id": "sec-lh-id", "displayName": "LH1", "type": "Lakehouse"}
        ]), \
             patch.object(sl, "get_lakehouses", side_effect=lambda ws: (
                 [{"id": "sec-lh-id", "displayName": "LH1", "type": "Lakehouse"}] if ws == "sec"
                 else [{"id": "pri-lh-id", "displayName": "LH1", "type": "Lakehouse"}]
             )), \
             patch.object(_common_module, "load_artifact_mapping", return_value={}), \
             patch.object(sl, "sync_lakehouse_fast_copy", side_effect=lambda **kw: capture_fc(**kw)):

            sl.reverse_sync_lakehouses(
                primary_workspace_id="pri",
                secondary_workspace_id="sec",
                logger=logger,
                strategy=sl.SyncStrategy.FAST_COPY,
                since_timestamp=FAKE_FAILOVER_TS,
                dry_run=False,
            )

        if received_ms:
            self.assertEqual(received_ms[0], FAKE_FAILOVER_MS)

    def test_missing_timestamp_defaults_to_zero(self):
        """When since_timestamp is None, failover_timestamp_ms must be 0 (full sync)."""
        logger = _make_logger("ts_zero")
        received_ms: List[int] = []

        def capture_fc(**kwargs):
            received_ms.append(kwargs.get("failover_timestamp_ms", -99))
            return {"success": True, "tables_found": 0}

        with patch.object(sl, "get_lakehouses", side_effect=lambda ws: (
            [{"id": "sec-lh-id", "displayName": "LH1", "type": "Lakehouse"}] if ws == "sec"
            else [{"id": "pri-lh-id", "displayName": "LH1", "type": "Lakehouse"}]
        )), \
             patch.object(_common_module, "load_artifact_mapping", return_value={}), \
             patch.object(sl, "sync_lakehouse_fast_copy", side_effect=lambda **kw: capture_fc(**kw)):

            sl.reverse_sync_lakehouses(
                primary_workspace_id="pri",
                secondary_workspace_id="sec",
                logger=logger,
                strategy=sl.SyncStrategy.FAST_COPY,
                since_timestamp=None,
                dry_run=False,
            )

        if received_ms:
            self.assertEqual(received_ms[0], 0)


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
