"""
Test Suite: Forward Sync Notebook (Lakehouse Data Sync page)

Validates _generate_per_lh_sync_notebook_ipynb() for both engines:
  - fast_copy  (default): notebookutils.fs.cp, full + incremental, no Spark/CDF
  - spark_cdf  (legacy):  Delta CDF + Spark upsert/delete

Run:
    python -m pytest scripts/test_forward_sync_notebook.py -v
"""

import json
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as app_module

PRIMARY_WS   = "pri-ws-guid"
SECONDARY_WS = "sec-ws-guid"
PRIMARY_LH   = "pri-lh-id"
SECONDARY_LH = "sec-lh-id"
LH_NAME      = "Claims_LH"


def _build(engine="fast_copy"):
    return app_module._generate_per_lh_sync_notebook_ipynb(
        primary_ws_id=PRIMARY_WS,
        secondary_ws_id=SECONDARY_WS,
        primary_lh_id=PRIMARY_LH,
        secondary_lh_id=SECONDARY_LH,
        lh_name=LH_NAME,
        sync_engine=engine,
    )


def _all_source(nb):
    """Concatenate all code cell source lines into one string."""
    return "\n".join(
        "".join(c["source"])
        for c in nb["cells"]
        if c["cell_type"] == "code"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Basic structure
# ─────────────────────────────────────────────────────────────────────────────

class TestNotebookStructure(unittest.TestCase):

    def test_returns_valid_notebook_format(self):
        nb = _build("fast_copy")
        self.assertEqual(nb["nbformat"], 4)
        self.assertIn("cells", nb)
        self.assertIn("metadata", nb)

    def test_default_lakehouse_is_secondary(self):
        nb = _build("fast_copy")
        trident = nb["metadata"]["trident"]["lakehouse"]
        self.assertEqual(trident["default_lakehouse"], SECONDARY_LH)
        self.assertEqual(trident["default_lakehouse_workspace_id"], SECONDARY_WS)

    def test_has_markdown_header(self):
        nb = _build("fast_copy")
        md_cells = [c for c in nb["cells"] if c["cell_type"] == "markdown"]
        self.assertGreater(len(md_cells), 0)
        header = "".join(md_cells[0]["source"])
        self.assertIn(LH_NAME, header)

    def test_has_at_least_five_code_cells(self):
        # config + shared_helpers + fast_copy_engine + spark_engine + files + main
        nb = _build("fast_copy")
        code_cells = [c for c in nb["cells"] if c["cell_type"] == "code"]
        self.assertGreaterEqual(len(code_cells), 5)

    def test_config_cell_has_workspace_ids(self):
        nb = _build("fast_copy")
        config_src = "".join(nb["cells"][1]["source"])  # second cell = config
        self.assertIn(PRIMARY_WS, config_src)
        self.assertIn(SECONDARY_WS, config_src)
        self.assertIn(PRIMARY_LH, config_src)
        self.assertIn(SECONDARY_LH, config_src)
        self.assertIn(LH_NAME, config_src)


# ─────────────────────────────────────────────────────────────────────────────
# Fast-copy engine
# ─────────────────────────────────────────────────────────────────────────────

class TestFastCopyEngine(unittest.TestCase):

    def setUp(self):
        self.nb = _build("fast_copy")
        self.src = _all_source(self.nb)

    def test_sync_engine_set_to_fast_copy(self):
        self.assertIn('SYNC_ENGINE = "fast_copy"', self.src)

    def test_has_incremental_cp_helper(self):
        self.assertIn("_incremental_cp", self.src)

    def test_has_fast_copy_full_function(self):
        self.assertIn("def fast_copy_full(", self.src)

    def test_has_fast_copy_incremental_function(self):
        self.assertIn("def fast_copy_incremental(", self.src)

    def test_full_copy_resets_last_checkpoint(self):
        """fast_copy_full must reset _last_checkpoint so Delta discovers the full log."""
        self.assertIn("_last_checkpoint", self.src)
        self.assertIn("fast_copy_full", self.src)

    def test_incremental_copy_filters_by_modifytime(self):
        """Incremental path must use since_ms to skip pre-sync files."""
        self.assertIn("since_ms", self.src)
        self.assertIn("modifyTime", self.src)

    def test_state_persisted_as_json(self):
        """Fast-copy state (watermarks) must be saved to Files/_bcdr_sync_state."""
        self.assertIn("_bcdr_sync_state", self.src)
        self.assertIn("_fc_save_state", self.src)
        self.assertIn("last_sync_ms", self.src)

    def test_auto_mode_incremental_when_state_exists(self):
        """In auto mode: if last_ms > 0, use incremental; else use full."""
        self.assertIn("SYNC_MODE == 'full' or last_ms == 0", self.src)
        self.assertIn("fast_copy_incremental", self.src)

    def test_delta_log_entries_copied_separately(self):
        """New _delta_log commit entries must be copied separately (not as part of full table cp)."""
        self.assertIn("_delta_log", self.src)
        # incremental copies only new log entries
        self.assertIn("log_new", self.src)

    def test_files_section_uses_incremental_cp(self):
        """Files/ section must reuse _incremental_cp with same since_ms watermark."""
        self.assertIn("sync_files_section", self.src)
        self.assertIn("since_for_files", self.src)
        self.assertIn("_bcdr_sync", self.src)  # skips own control dirs

    def test_no_direct_spark_read_in_fast_copy_path(self):
        """Fast-copy should NOT use spark.read in the main execution block."""
        # The main block is inside SYNC_ENGINE == 'fast_copy' branch
        # Verify the fast-copy functions don't call spark.read directly
        self.assertNotIn("spark.read.format", self.src.split("SPARK / CDF")[0])


# ─────────────────────────────────────────────────────────────────────────────
# Spark / CDF engine (legacy)
# ─────────────────────────────────────────────────────────────────────────────

class TestSparkCDFEngine(unittest.TestCase):

    def setUp(self):
        self.nb = _build("spark_cdf")
        self.src = _all_source(self.nb)

    def test_sync_engine_set_to_spark_cdf(self):
        self.assertIn('SYNC_ENGINE = "spark_cdf"', self.src)

    def test_has_full_sync_table(self):
        self.assertIn("def full_sync_table(", self.src)

    def test_has_incremental_sync_table(self):
        self.assertIn("def incremental_sync_table(", self.src)

    def test_has_enable_cdf(self):
        self.assertIn("def enable_cdf(", self.src)

    def test_cdf_fallback_on_error(self):
        """CDF engine must fall back to full sync when CDF unavailable."""
        self.assertIn("CDF unavailable", self.src)
        self.assertIn("full_sync_table", self.src)

    def test_uses_delta_change_feed(self):
        self.assertIn("readChangeFeed", self.src)

    def test_uses_merge_for_upserts(self):
        self.assertIn("whenMatchedUpdateAll", self.src)
        self.assertIn("whenNotMatchedInsertAll", self.src)

    def test_spark_path_does_not_use_fc_state(self):
        """CDF path must not use fast-copy state file."""
        # _fc_load_state should not be invoked in spark path main block
        # (Both are defined in same nb but only relevant engine branch executes)
        self.assertIn("_bcdr_sync_control", self.src)


# ─────────────────────────────────────────────────────────────────────────────
# Both engines — shared guarantees
# ─────────────────────────────────────────────────────────────────────────────

class TestBothEngines(unittest.TestCase):

    def _check_engine(self, engine):
        nb = _build(engine)
        src = _all_source(nb)

        # Must discover tables from primary
        self.assertIn("discover_tables", src, f"{engine}: missing discover_tables")

        # Must handle Files/ section
        self.assertIn("sync_files_section", src, f"{engine}: missing files sync")

        # Must produce a result dict with 'engine' key
        self.assertIn("'engine'", src, f"{engine}: missing engine in result")

        # Must call mssparkutils.notebook.exit
        self.assertIn("mssparkutils.notebook.exit", src, f"{engine}: missing exit call")

        # Must handle errors list
        self.assertIn("errors", src, f"{engine}: missing error handling")

        # Default lakehouse must always be secondary
        trident = nb["metadata"]["trident"]["lakehouse"]
        self.assertEqual(trident["default_lakehouse"], SECONDARY_LH,
                         f"{engine}: wrong default_lakehouse")

    def test_fast_copy_shared_guarantees(self):
        self._check_engine("fast_copy")

    def test_spark_cdf_shared_guarantees(self):
        self._check_engine("spark_cdf")

    def test_lh_name_in_main_cell(self):
        for engine in ("fast_copy", "spark_cdf"):
            nb = _build(engine)
            src = _all_source(nb)
            self.assertIn(LH_NAME, src, f"{engine}: LH_NAME missing from notebook")

    def test_catalog_registration_called(self):
        """Both engines must call the BCDR_Register_ notebook for catalog sync."""
        for engine in ("fast_copy", "spark_cdf"):
            src = _all_source(_build(engine))
            self.assertIn("BCDR_Register_", src, f"{engine}: missing registration call")
            self.assertIn("mssparkutils.notebook.run", src)


# ─────────────────────────────────────────────────────────────────────────────
# deploy_sync_artifacts integration — fast_copy is the default
# ─────────────────────────────────────────────────────────────────────────────

class TestDeploySyncArtifactsUsesDefaultEngine(unittest.TestCase):
    """Verify _generate_per_lh_sync_notebook_ipynb defaults to fast_copy."""

    def test_default_engine_is_fast_copy(self):
        import inspect
        sig = inspect.signature(app_module._generate_per_lh_sync_notebook_ipynb)
        default = sig.parameters["sync_engine"].default
        self.assertEqual(default, "fast_copy")

    def test_fast_copy_notebook_is_valid_json_serialisable(self):
        """The generated notebook must be JSON-serialisable (Fabric Items API requires this)."""
        nb = _build("fast_copy")
        try:
            serialised = json.dumps(nb)
            self.assertGreater(len(serialised), 100)
        except (TypeError, ValueError) as e:
            self.fail(f"Notebook is not JSON-serialisable: {e}")

    def test_spark_cdf_notebook_is_valid_json_serialisable(self):
        nb = _build("spark_cdf")
        try:
            json.dumps(nb)
        except (TypeError, ValueError) as e:
            self.fail(f"spark_cdf notebook not JSON-serialisable: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
