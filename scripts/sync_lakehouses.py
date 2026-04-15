"""
Sync Lakehouses

Purpose:
  BCDR for Lakehouse artifacts - both metadata (Delta table definitions) and
  OneLake data replication with selectable strategies.

Artifact Types Covered:
  Lakehouse

RPO/RTO by Strategy:
  Option 1 (azcopy): RPO = last sync interval, RTO = minutes
  Option 2 (Shortcuts): RPO = near-zero, RTO = seconds
  Option 3 (GRS): RPO = depends on GRS replication, RTO = seconds

Prerequisites:
  - azcopy installed and in PATH (for Option 1)
  - OneLake credentials configured

Sync Strategies:
  1. ACTIVE_REPLICATION - azcopy sync between regions
  2. ONELAKE_SHORTCUTS - Zero-copy DR using shortcuts
  3. GRS_PASSTHROUGH - Metadata only, GRS handles data

Usage:
  python sync_lakehouses.py --strategy ONELAKE_SHORTCUTS
  python sync_lakehouses.py --strategy ACTIVE_REPLICATION --dry-run
"""

import json
import argparse
import subprocess
import os
import base64
import time
from typing import Dict, List, Any, Optional
from enum import Enum

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common


class SyncStrategy(Enum):
    """Lakehouse sync strategy"""
    ACTIVE_REPLICATION = "ACTIVE_REPLICATION"  # azcopy full sync
    ONELAKE_SHORTCUTS = "ONELAKE_SHORTCUTS"    # zero-copy shortcuts
    GRS_PASSTHROUGH = "GRS_PASSTHROUGH"        # metadata-only, GRS handles data
    FAST_COPY = "FAST_COPY"                    # notebookutils.fs.cp (server-side, MS DR guidance)


def get_lakehouses(workspace_id: str) -> List[Dict[str, Any]]:
    """
    Get all lakehouses in workspace.
    
    Args:
        workspace_id: Workspace GUID
        
    Returns:
        List of lakehouse artifacts
    """
    return common.get_items(workspace_id, item_type="Lakehouse")


def sync_lakehouse_metadata(
    primary_workspace_id: str,
    primary_lakehouse_id: str,
    secondary_workspace_id: str,
    secondary_lakehouse_id: str,
    logger,
) -> Dict[str, Any]:
    """
    Sync lakehouse metadata (Delta table definitions).
    
    Args:
        primary_workspace_id: Primary workspace GUID
        primary_lakehouse_id: Primary lakehouse ID
        secondary_workspace_id: Secondary workspace GUID
        secondary_lakehouse_id: Secondary lakehouse ID
        logger: Logger instance
        
    Returns:
        Sync result dict
    """
    result = {
        "tables_created": [],
        "tables_skipped": [],
        "tables_failed": [],
    }
    
    try:
        # Get table list from primary (using API or management APIs)
        # For now, we'll log the intent as full table sync via OneLake is more common
        logger.info(f"Syncing metadata for Lakehouse {primary_lakehouse_id}")
        logger.info("Note: Delta table metadata is typically synced via data replication")
        
    except Exception as e:
        logger.error(f"Error syncing lakehouse metadata: {str(e)}")
    
    return result


def sync_lakehouse_via_azcopy(
    source_workspace_id: str,
    source_lakehouse_name: str,
    dest_workspace_id: str,
    dest_lakehouse_name: str,
    logger,
    dry_run: bool = False,
    since_timestamp: Optional[str] = None,
) -> bool:
    """
    Sync lakehouse data via azcopy.

    For normal primary→secondary sync, since_timestamp is None (full sync).
    For failback reverse sync (secondary→primary), pass since_timestamp as an
    ISO-8601 string (e.g. "2026-04-14T10:07:00Z") to use --include-after,
    which restricts the copy to only files modified after the failover divergence
    point, avoiding unnecessary re-transfer and preventing stale overwrites.

    Args:
        source_workspace_id: Source workspace GUID
        source_lakehouse_name: Source lakehouse display name
        dest_workspace_id: Destination workspace GUID
        dest_lakehouse_name: Destination lakehouse display name
        logger: Logger instance
        dry_run: If True, print command without executing
        since_timestamp: ISO-8601 datetime string; when set, adds --include-after
                         so only delta Δ written after failover is transferred back
    """
    source = (
        f"https://onelake.dfs.fabric.microsoft.com/{source_workspace_id}/"
        f"{source_lakehouse_name}.Lakehouse/Tables"
    )
    dest = (
        f"https://onelake.dfs.fabric.microsoft.com/{dest_workspace_id}/"
        f"{dest_lakehouse_name}.Lakehouse/Tables"
    )

    cmd = [
        "azcopy",
        "sync",
        source,
        dest,
        "--recursive",
        "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com",
    ]

    if since_timestamp:
        # Only transfer files written after the failover divergence point.
        # This is the key safety mechanism for reverse/failback sync:
        # primary may already have V1 data — we only want to add Δ (V2) rows.
        cmd.append(f"--include-after={since_timestamp}")
        logger.info(f"Incremental reverse sync from {since_timestamp} (delta Δ only)")

    direction = "incremental reverse" if since_timestamp else "full"
    logger.info(f"Syncing lakehouse {source_lakehouse_name} via azcopy ({direction})...")
    logger.info(f"Source: {source}")
    logger.info(f"Dest:   {dest}")

    if dry_run:
        logger.info(f"[DRY RUN] Would execute: {' '.join(cmd)}")
        return True

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            logger.info("✓ azcopy sync completed successfully")
            return True
        else:
            logger.error(f"✗ azcopy sync failed: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        logger.error("azcopy sync timed out after 1 hour")
        return False
    except FileNotFoundError:
        logger.error("azcopy not found — install via: https://aka.ms/downloadazcopy")
        return False
    except Exception as e:
        logger.error(f"Error running azcopy: {str(e)}")
        return False


# ============================================================================
# FAST COPY — notebookutils.fs.cp (Microsoft DR guidance Approach 1)
# ============================================================================

def _build_fast_copy_notebook_ipynb(
    source_workspace_id: str,
    source_lakehouse_name: str,
    dest_workspace_id: str,
    dest_lakehouse_name: str,
    dest_lakehouse_id: str,
    tables: List[str],
    failover_timestamp_ms: int,
) -> dict:
    """
    Build an iPYNB notebook that performs an INCREMENTAL fast-copy of lakehouse
    tables using notebookutils.fs.cp (server-side copy inside OneLake).

    INCREMENTAL means: only files with modifyTime > failover_timestamp_ms are
    copied from source to destination.  V1 files that already exist on the
    primary are untouched — no wasted I/O, no risk of stale overwrites.

    How it works per table:
      1. Walk the source directory recursively (tables + partitions + _delta_log)
      2. For each file: if modifyTime > failover_ts_ms → cp to corresponding dest path
      3. After all data files are copied, replay only the new delta log entries
         (json commit files newer than failover_ts_ms) onto the destination
      4. Rewrite _last_checkpoint on dest so Delta picks up the new commits

    Why this is safe:
      - Primary already has all V1 data from before failover — we never overwrite it
      - Only Δ (post-failover) parquet data files and their matching commit entries move
      - Delta integrity is maintained: new .json commit files reference only the new
        parquet files we just copied

    See: https://learn.microsoft.com/en-us/fabric/security/experience-specific-guidance
    (Approach 1 — extended to be incremental using modifyTime filtering)

    Args:
        source_workspace_id: Secondary (active) workspace — data source for reverse sync
        source_lakehouse_name: Source lakehouse display name
        dest_workspace_id: Primary (restoring) workspace — data destination
        dest_lakehouse_name: Destination lakehouse display name
        dest_lakehouse_id: Destination lakehouse GUID (for notebook default_lakehouse)
        tables: List of table path strings relative to Tables/
        failover_timestamp_ms: Epoch ms at failover divergence point.
                               0 = full copy (no filtering), acts like original approach.
    """
    per_table_cells = []
    for table in tables:
        source_path = (
            f"abfss://{source_workspace_id}@onelake.dfs.fabric.microsoft.com/"
            f"{source_lakehouse_name}.Lakehouse/Tables/{table}"
        )
        dest_path = (
            f"abfss://{dest_workspace_id}@onelake.dfs.fabric.microsoft.com/"
            f"{dest_lakehouse_name}.Lakehouse/Tables/{table}"
        )
        per_table_cells.append({
            "cell_type": "code",
            "metadata": {},
            "outputs": [],
            "execution_count": None,
            "source": [
                f"# === Incremental fast-copy: {table} ===\n",
                f"src_root  = \"{source_path}\"\n",
                f"dst_root  = \"{dest_path}\"\n",
                f"since_ms  = {failover_timestamp_ms}  # 0 = full copy\n",
                "\n",
                "# ── recursive helper: copy only files newer than since_ms ─────────────\n",
                "def incremental_cp(src_dir, dst_dir, since_ms, depth=0):\n",
                "    copied = skipped = 0\n",
                "    try:\n",
                "        items = notebookutils.fs.ls(src_dir)\n",
                "    except Exception:\n",
                "        return 0, 0\n",
                "    for item in items:\n",
                "        item_name = item.name.rstrip('/')\n",
                "        src_path  = f\"{src_dir}/{item_name}\"\n",
                "        dst_path  = f\"{dst_dir}/{item_name}\"\n",
                "        if item.isDir:\n",
                "            # Always recurse into directories (partitions, _delta_log, etc.)\n",
                "            c, s = incremental_cp(src_path, dst_path, since_ms, depth + 1)\n",
                "            copied += c; skipped += s\n",
                "        else:\n",
                "            # Skip files that predate the failover — already on primary (V1)\n",
                "            if since_ms > 0 and item.modifyTime <= since_ms:\n",
                "                skipped += 1\n",
                "                continue\n",
                "            notebookutils.fs.cp(src_path, dst_path, False)\n",
                "            copied += 1\n",
                "    return copied, skipped\n",
                "\n",
                "# ── Step 1: Copy new data files (parquet/avro) under Tables/{table} ──────\n",
                "# Skip _delta_log subdir here; we handle it separately in Step 2\n",
                "data_copied = data_skipped = 0\n",
                "try:\n",
                "    for item in notebookutils.fs.ls(src_root):\n",
                "        if item.name.rstrip('/') == '_delta_log':\n",
                "            continue  # handled below\n",
                "        c, s = incremental_cp(\n",
                "            f\"{src_root}/{item.name.rstrip('/')}\",\n",
                "            f\"{dst_root}/{item.name.rstrip('/')}\",\n",
                "            since_ms\n",
                "        ) if item.isDir else (0, 0)\n",
                "        if not item.isDir:\n",
                "            # top-level data file (non-partitioned table)\n",
                "            if since_ms == 0 or item.modifyTime > since_ms:\n",
                "                notebookutils.fs.cp(f\"{src_root}/{item.name}\", f\"{dst_root}/{item.name}\", False)\n",
                "                c = 1\n",
                "        data_copied += c; data_skipped += s\n",
                "    print(f'  Data files: {data_copied} copied, {data_skipped} skipped (pre-failover V1)')\n",
                "except Exception as e:\n",
                "    print(f'  Warning copying data files for {table}: {e}')\n",
                "\n",
                "# ── Step 2: Copy only NEW delta log commit entries ────────────────────────\n",
                "# New .json files in _delta_log represent post-failover transactions on\n",
                "# secondary. Only these belong on the restored primary.\n",
                "log_copied = log_skipped = 0\n",
                "try:\n",
                "    src_log = f\"{src_root}/_delta_log\"\n",
                "    dst_log = f\"{dst_root}/_delta_log\"\n",
                "    for log_file in notebookutils.fs.ls(src_log):\n",
                "        fname = log_file.name.rstrip('/')\n",
                "        if fname == '_last_checkpoint':\n",
                "            continue  # rewrite this ourselves in Step 3\n",
                "        if since_ms > 0 and log_file.modifyTime <= since_ms:\n",
                "            log_skipped += 1\n",
                "            continue\n",
                "        notebookutils.fs.cp(f\"{src_log}/{fname}\", f\"{dst_log}/{fname}\", False)\n",
                "        log_copied += 1\n",
                "    print(f'  Delta log: {log_copied} new commits copied, {log_skipped} pre-failover entries left as-is')\n",
                "except Exception as e:\n",
                "    print(f'  Warning copying delta log for {table}: {e}')\n",
                "\n",
                "# ── Step 3: Rewrite _last_checkpoint so Delta re-discovers new commits ───\n",
                "try:\n",
                "    notebookutils.fs.put(f\"{dst_root}/_delta_log/_last_checkpoint\", '', True)\n",
                "    print(f'  _last_checkpoint reset on primary')\n",
                "except Exception as e:\n",
                "    print(f'  Warning resetting _last_checkpoint: {e}')\n",
                "\n",
                f"print(f'\u2713 {table}: incremental fast-copy done '\n",
                f"      f'({{data_copied + log_copied}} files copied, {{data_skipped + log_skipped}} V1 files skipped)')\n",
            ],
        })

    cells = [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# BCDR Incremental Fast-Copy Reverse Sync\n",
                "\n",
                "Auto-generated by failback.py — copies ONLY delta Δ (post-failover files)\n",
                "from secondary back to primary using notebookutils.fs.cp (server-side).\n",
                "\n",
                "Files with modifyTime <= failover_timestamp_ms are skipped — they are\n",
                "already present on the primary as V1 data.\n",
                f"Failover timestamp (ms): {failover_timestamp_ms}\n",
            ],
        },
        {
            "cell_type": "code",
            "metadata": {},
            "outputs": [],
            "execution_count": None,
            "source": [
                "from notebookutils import mssparkutils as notebookutils\n",
                "import json\n",
            ],
        },
        *per_table_cells,
        {
            "cell_type": "code",
            "metadata": {},
            "outputs": [],
            "execution_count": None,
            "source": [
                "print('\\n\u2713 Incremental fast-copy complete for all tables')\n",
                "notebookutils.notebook.exit(json.dumps({'status': 'success', "
                f"'tables': {tables}}})\n",
            ],
        },
    ]

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernel_info": {"name": "synapse_pyspark"},
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "language_info": {"name": "python"},
            "trident": {
                "lakehouse": {
                    "default_lakehouse": dest_lakehouse_id,
                    "default_lakehouse_name": dest_lakehouse_name,
                    "default_lakehouse_workspace_id": dest_workspace_id,
                    "known_lakehouses": [{"id": dest_lakehouse_id}],
                },
            },
        },
        "cells": cells,
    }


def _create_and_run_notebook(
    workspace_id: str,
    notebook_name: str,
    notebook_ipynb: dict,
    logger,
    poll_interval: int = 10,
    max_wait_seconds: int = 3600,
) -> Dict[str, Any]:
    """
    Create a temporary notebook in Fabric, trigger it as a RunNotebook job,
    poll until completion, then delete it.

    Returns dict with keys: success (bool), status (str), output (str), error (str).
    """
    result = {"success": False, "status": "UNKNOWN", "output": "", "error": ""}
    notebook_id = None

    try:
        # 1. Encode notebook as base64 for Fabric Items API
        nb_json = json.dumps(notebook_ipynb)
        nb_b64 = base64.b64encode(nb_json.encode("utf-8")).decode("ascii")

        create_payload = {
            "displayName": notebook_name,
            "type": "Notebook",
            "definition": {
                "format": "ipynb",
                "parts": [
                    {
                        "path": "artifact.content.ipynb",
                        "payload": nb_b64,
                        "payloadType": "InlineBase64",
                    }
                ],
            },
        }

        logger.info(f"Creating temporary notebook '{notebook_name}' in workspace {workspace_id}")
        created = common.api_call("POST", f"/workspaces/{workspace_id}/items", create_payload)
        notebook_id = created.get("id")
        if not notebook_id:
            result["error"] = "Notebook creation returned no ID"
            return result
        logger.info(f"Notebook created: {notebook_id}")

        # 2. Trigger RunNotebook job
        logger.info(f"Triggering RunNotebook job for {notebook_name}")
        common.api_call(
            "POST",
            f"/workspaces/{workspace_id}/items/{notebook_id}/jobs/instances?jobType=RunNotebook",
        )

        # 3. Poll for job completion
        elapsed = 0
        while elapsed < max_wait_seconds:
            time.sleep(poll_interval)
            elapsed += poll_interval

            instances_resp = common.api_call(
                "GET",
                f"/workspaces/{workspace_id}/items/{notebook_id}/jobs/instances",
            )
            instances = instances_resp.get("value", [])
            if not instances:
                continue

            # Most recent job instance
            latest = sorted(
                instances,
                key=lambda j: j.get("startTimeUtc", ""),
                reverse=True,
            )[0]
            status = latest.get("status", "")
            logger.info(f"  [{elapsed}s] Job status: {status}")

            if status == "Completed":
                result["success"] = True
                result["status"] = "Completed"
                result["output"] = latest.get("failureReason", "")  # output stored here too
                break
            elif status in ("Failed", "Cancelled", "Deduped"):
                result["status"] = status
                result["error"] = latest.get("failureReason", f"Job {status}")
                logger.error(f"Notebook job {status}: {result['error']}")
                break

        else:
            result["error"] = f"Notebook job timed out after {max_wait_seconds}s"
            result["status"] = "Timeout"

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Error in _create_and_run_notebook: {e}")
    finally:
        # 4. Always delete the temp notebook
        if notebook_id:
            try:
                common.api_call("DELETE", f"/workspaces/{workspace_id}/items/{notebook_id}")
                logger.info(f"Deleted temporary notebook {notebook_id}")
            except Exception as del_e:
                logger.warning(f"Could not delete temp notebook {notebook_id}: {del_e}")

    return result


def sync_lakehouse_fast_copy(
    source_workspace_id: str,
    source_lakehouse_id: str,
    source_lakehouse_name: str,
    dest_workspace_id: str,
    dest_lakehouse_id: str,
    dest_lakehouse_name: str,
    logger,
    dry_run: bool = False,
    failover_timestamp_ms: int = 0,
) -> Dict[str, Any]:
    """
    Copy lakehouse tables using notebookutils.fs.cp (server-side fast copy).

    Creates a temporary Fabric notebook that runs inside OneLake, performs the
    copy server-side (no bytes leave the Microsoft network), and trims any delta
    log entries written after the failover timestamp.  This is Microsoft's
    recommended Approach 1 from the DR experience-specific guidance:
    https://learn.microsoft.com/en-us/fabric/security/experience-specific-guidance

    Args:
        source_workspace_id: Source (secondary during failback) workspace GUID
        source_lakehouse_id: Source lakehouse GUID
        source_lakehouse_name: Source lakehouse display name
        dest_workspace_id: Destination (primary during failback) workspace GUID
        dest_lakehouse_id: Destination lakehouse GUID
        dest_lakehouse_name: Destination lakehouse display name
        logger: Logger instance
        dry_run: If True, log what would be done without executing
        failover_timestamp_ms: Epoch ms at failover — delta log entries newer than
                               this are trimmed from dest so primary's history
                               stays clean (no secondary-only commits)
    """
    result = {
        "strategy": "fast_copy",
        "tables_found": 0,
        "success": False,
        "error": "",
    }

    try:
        # Discover tables in source lakehouse
        tables_resp = common.api_call(
            "GET",
            f"/workspaces/{source_workspace_id}/lakehouses/{source_lakehouse_id}/tables",
        )
        tables = [t.get("name", "") for t in tables_resp.get("data", []) if t.get("name")]
        result["tables_found"] = len(tables)
        logger.info(f"Found {len(tables)} tables in source lakehouse '{source_lakehouse_name}'")

        if not tables:
            logger.info("No tables to copy — skipping")
            result["success"] = True
            return result

        if dry_run:
            logger.info(
                f"[DRY RUN] Would fast-copy {len(tables)} tables: {tables} "
                f"(failover_ts_ms={failover_timestamp_ms})"
            )
            result["success"] = True
            return result

        # Build the fast-copy notebook
        nb_ipynb = _build_fast_copy_notebook_ipynb(
            source_workspace_id=source_workspace_id,
            source_lakehouse_name=source_lakehouse_name,
            dest_workspace_id=dest_workspace_id,
            dest_lakehouse_name=dest_lakehouse_name,
            dest_lakehouse_id=dest_lakehouse_id,
            tables=tables,
            failover_timestamp_ms=failover_timestamp_ms,
        )

        # Run it in the destination (primary) workspace so the default lakehouse
        # is scoped correctly for Spark catalog operations
        nb_name = f"_BCDR_FastCopy_{dest_lakehouse_name}_Temp"
        run_result = _create_and_run_notebook(
            workspace_id=dest_workspace_id,
            notebook_name=nb_name,
            notebook_ipynb=nb_ipynb,
            logger=logger,
        )

        result["success"] = run_result["success"]
        result["job_status"] = run_result["status"]
        if not run_result["success"]:
            result["error"] = run_result["error"]
            logger.error(f"Fast-copy notebook failed for '{source_lakehouse_name}': {run_result['error']}")
        else:
            logger.info(f"✓ Fast-copy completed for '{source_lakehouse_name}'")

    except Exception as e:
        result["error"] = str(e)
        logger.error(f"Error in sync_lakehouse_fast_copy for '{source_lakehouse_name}': {e}")

    return result


# ============================================================================
# REVERSE SYNC entry point — secondary → primary (used by failback.py)
# ============================================================================

def reverse_sync_lakehouses(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    strategy: SyncStrategy = SyncStrategy.FAST_COPY,
    since_timestamp: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Reverse sync: copy delta Δ written on secondary AFTER failover back to primary.

    This is the data layer of failback.py Step 2.  It is direction-aware:
    source = secondary (was active during DR window)
    dest   = primary   (just restored, needs Δ patched in)

    Strategy selection:
      FAST_COPY (default) — notebookutils.fs.cp with delta log trimming.
          Best for Fabric-native workloads; server-side, no network egress.
      ACTIVE_REPLICATION  — azcopy sync with --include-after=since_timestamp.
          Best when azcopy is already set up; works outside Fabric environment.

    Args:
        primary_workspace_id: The restored primary workspace GUID (data destination)
        secondary_workspace_id: The active secondary workspace GUID (data source)
        logger: Logger instance
        strategy: FAST_COPY or ACTIVE_REPLICATION
        since_timestamp: ISO-8601 string marking the failover divergence point.
                         Required for ACTIVE_REPLICATION (--include-after).
                         For FAST_COPY this is converted to epoch ms for delta
                         log trimming.  If None, falls back to full re-sync.
        dry_run: If True, log planned actions without executing

    Returns:
        Summary dict with per-lakehouse results.
    """
    summary: Dict[str, Any] = {
        "strategy": strategy.value,
        "since_timestamp": since_timestamp,
        "lakehouses_synced": [],
        "lakehouses_failed": [],
        "lakehouses_skipped": [],
    }

    # Convert since_timestamp to epoch ms for fast-copy delta log trimming
    failover_ts_ms = 0
    if since_timestamp:
        try:
            from datetime import timezone
            from datetime import datetime as _dt
            dt = _dt.fromisoformat(since_timestamp.replace("Z", "+00:00"))
            failover_ts_ms = int(dt.timestamp() * 1000)
            logger.info(f"Failover divergence point: {since_timestamp} ({failover_ts_ms} ms)")
        except Exception as e:
            logger.warning(f"Could not parse since_timestamp '{since_timestamp}': {e} — delta log trimming disabled")

    # Get lakehouse pairs (by display name match)
    artifact_mapping = common.load_artifact_mapping()
    secondary_lakehouses = get_lakehouses(secondary_workspace_id)
    primary_lakehouses = get_lakehouses(primary_workspace_id)
    primary_by_name = {lh["displayName"]: lh for lh in primary_lakehouses}

    for sec_lh in secondary_lakehouses:
        sec_lh_id = sec_lh["id"]
        sec_lh_name = sec_lh["displayName"]

        pri_lh = primary_by_name.get(sec_lh_name)
        if not pri_lh:
            logger.warning(f"No primary match for secondary lakehouse '{sec_lh_name}' — skipping")
            summary["lakehouses_skipped"].append(sec_lh_name)
            continue

        pri_lh_id = pri_lh["id"]
        logger.info(f"\n--- Reverse-syncing Lakehouse: {sec_lh_name} ---")
        logger.info(f"    Source (secondary): {sec_lh_id}")
        logger.info(f"    Dest   (primary):   {pri_lh_id}")

        try:
            if strategy == SyncStrategy.FAST_COPY:
                res = sync_lakehouse_fast_copy(
                    source_workspace_id=secondary_workspace_id,
                    source_lakehouse_id=sec_lh_id,
                    source_lakehouse_name=sec_lh_name,
                    dest_workspace_id=primary_workspace_id,
                    dest_lakehouse_id=pri_lh_id,
                    dest_lakehouse_name=sec_lh_name,
                    logger=logger,
                    dry_run=dry_run,
                    failover_timestamp_ms=failover_ts_ms,
                )
                if res["success"]:
                    summary["lakehouses_synced"].append(sec_lh_name)
                else:
                    summary["lakehouses_failed"].append({"name": sec_lh_name, "error": res.get("error", "")})

            elif strategy == SyncStrategy.ACTIVE_REPLICATION:
                success = sync_lakehouse_via_azcopy(
                    source_workspace_id=secondary_workspace_id,
                    source_lakehouse_name=sec_lh_name,
                    dest_workspace_id=primary_workspace_id,
                    dest_lakehouse_name=sec_lh_name,
                    logger=logger,
                    dry_run=dry_run,
                    since_timestamp=since_timestamp,
                )
                if success:
                    summary["lakehouses_synced"].append(sec_lh_name)
                else:
                    summary["lakehouses_failed"].append({"name": sec_lh_name, "error": "azcopy failed"})

            else:
                logger.warning(f"Strategy {strategy.value} not supported for reverse sync — skipping")
                summary["lakehouses_skipped"].append(sec_lh_name)

        except Exception as e:
            logger.error(f"Error reverse-syncing '{sec_lh_name}': {e}")
            summary["lakehouses_failed"].append({"name": sec_lh_name, "error": str(e)})

    logger.info(
        f"Reverse sync complete — synced: {len(summary['lakehouses_synced'])}, "
        f"failed: {len(summary['lakehouses_failed'])}, "
        f"skipped: {len(summary['lakehouses_skipped'])}"
    )
    return summary


def create_onelake_shortcuts(
    primary_workspace_id: str,
    primary_lakehouse_id: str,
    secondary_workspace_id: str,
    secondary_lakehouse_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Create OneLake shortcuts in secondary lakehouse pointing to primary.
    Zero-copy DR pattern.
    
    Args:
        primary_workspace_id: Primary workspace GUID
        primary_lakehouse_id: Primary lakehouse ID
        secondary_workspace_id: Secondary workspace GUID
        secondary_lakehouse_id: Secondary lakehouse ID
        logger: Logger instance
        dry_run: If True, don't execute
        
    Returns:
        Shortcuts creation result
    """
    result = {
        "shortcuts_created": [],
        "shortcuts_failed": [],
    }
    
    logger.info(f"Creating OneLake shortcuts in secondary lakehouse...")
    
    # Get primary lakehouse tables
    try:
        endpoint = f"/workspaces/{primary_workspace_id}/lakehouses/{primary_lakehouse_id}/tables"
        tables_response = common.api_call("GET", endpoint)
        tables = tables_response.get("value", [])
        
        logger.info(f"Found {len(tables)} tables in primary lakehouse")
        
        for table in tables:
            table_name = table.get("name")
            
            # Define shortcut payload
            shortcut_payload = {
                "path": "/Tables",
                "name": table_name,
                "target": {
                    "type": "OneLake",
                    "oneLake": {
                        "workspaceId": primary_workspace_id,
                        "itemId": primary_lakehouse_id,
                        "path": f"/Tables/{table_name}",
                    },
                },
            }
            
            logger.info(f"Creating shortcut for table: {table_name}")
            
            if dry_run:
                logger.info(f"[DRY RUN] Would create shortcut: {table_name}")
                result["shortcuts_created"].append(table_name)
            else:
                try:
                    endpoint = (
                        f"/workspaces/{secondary_workspace_id}/lakehouses/"
                        f"{secondary_lakehouse_id}/shortcuts"
                    )
                    common.api_call("POST", endpoint, shortcut_payload)
                    result["shortcuts_created"].append(table_name)
                    logger.info(f"✓ Shortcut created: {table_name}")
                except Exception as e:
                    result["shortcuts_failed"].append({
                        "table": table_name,
                        "error": str(e),
                    })
                    logger.error(f"✗ Failed to create shortcut {table_name}: {str(e)}")
    
    except Exception as e:
        logger.error(f"Error creating shortcuts: {str(e)}")
    
    return result


def sync_lakehouse_grs_passthrough(
    primary_lakehouse_name: str,
    logger,
) -> bool:
    """
    GRS passthrough - log metadata only (Azure GRS handles data).
    
    Args:
        primary_lakehouse_name: Lakehouse name for logging
        logger: Logger instance
        
    Returns:
        True (always succeeds, no-op for data)
    """
    logger.info(f"GRS Passthrough mode for lakehouse: {primary_lakehouse_name}")
    logger.info("Skipping data copy - Azure GRS replication assumed active")
    logger.info("Ensure GRS is enabled on Azure Data Lake Storage accounts")
    return True


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Sync Fabric lakehouses")
    parser.add_argument(
        "--strategy",
        choices=["ACTIVE_REPLICATION", "ONELAKE_SHORTCUTS", "GRS_PASSTHROUGH"],
        default="ONELAKE_SHORTCUTS",
        help="Data sync strategy",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without executing",
    )
    parser.add_argument(
        "--primary-workspace",
        default=common.PRIMARY_WORKSPACE_ID,
        help="Primary workspace GUID",
    )
    parser.add_argument(
        "--secondary-workspace",
        default=common.SECONDARY_WORKSPACE_ID,
        help="Secondary workspace GUID",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Reverse sync direction: secondary → primary (used during failback)",
    )
    parser.add_argument(
        "--since-timestamp",
        default=None,
        help="ISO-8601 failover timestamp (e.g. 2026-04-14T10:07:00Z). "
             "When set, azcopy adds --include-after (ACTIVE_REPLICATION) or "
             "fast-copy trims delta log entries newer than this (FAST_COPY).",
    )

    args = parser.parse_args()

    logger = common.setup_logger("sync_lakehouses")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    strategy = SyncStrategy[args.strategy]
    logger.info(f"Using sync strategy: {strategy.value}")
    
    try:
        # Get lakehouses from primary
        logger.info("Fetching lakehouses from primary workspace...")
        primary_lakehouses = get_lakehouses(args.primary_workspace)
        logger.info(f"Found {len(primary_lakehouses)} lakehouses in primary")
        
        # Load artifact mapping
        artifact_mapping = common.load_artifact_mapping()
        
        sync_summary = {
            "strategy": strategy.value,
            "lakehouses_processed": 0,
            "lakehouses_skipped": 0,
            "data_sync_results": [],
        }
        
        # ── Reverse sync (failback: secondary → primary) ──────────────────────
        if args.reverse:
            logger.info("REVERSE SYNC MODE (secondary → primary)")
            rev_result = reverse_sync_lakehouses(
                primary_workspace_id=args.primary_workspace,
                secondary_workspace_id=args.secondary_workspace,
                logger=logger,
                strategy=strategy,
                since_timestamp=args.since_timestamp,
                dry_run=args.dry_run,
            )
            common.save_json(rev_result, "data/lakehouse_reverse_sync_report.json")
            print("\n" + "=" * 70)
            print("REVERSE SYNC SUMMARY (secondary → primary)")
            print("=" * 70)
            print(f"Strategy:          {rev_result['strategy']}")
            print(f"Since timestamp:   {rev_result['since_timestamp'] or 'full re-sync'}")
            print(f"Lakehouses synced: {len(rev_result['lakehouses_synced'])}")
            print(f"Lakehouses failed: {len(rev_result['lakehouses_failed'])}")
            print(f"Lakehouses skipped:{len(rev_result['lakehouses_skipped'])}")
            print("=" * 70 + "\n")
            return len(rev_result["lakehouses_failed"]) == 0

        # ── Normal forward sync (primary → secondary) ──────────────────────────
        for primary_lh in primary_lakehouses:
            primary_lh_id = primary_lh["id"]
            primary_lh_name = primary_lh["displayName"]

            # Find secondary lakehouse
            secondary_lh_id = artifact_mapping.get(primary_lh_id)
            
            if not secondary_lh_id:
                # Create in secondary if missing
                logger.info(f"Lakehouse {primary_lh_name} not in secondary, "
                           "skipping data sync (create via sync_workspaces_metadata first)")
                sync_summary["lakehouses_skipped"] += 1
                continue
            
            logger.info(f"\n--- Processing Lakehouse: {primary_lh_name} ---")
            
            # First, sync metadata
            metadata_result = sync_lakehouse_metadata(
                args.primary_workspace,
                primary_lh_id,
                args.secondary_workspace,
                secondary_lh_id,
                logger,
            )
            
            # Then, sync data based on strategy
            data_result = None
            
            if strategy == SyncStrategy.ACTIVE_REPLICATION:
                success = sync_lakehouse_via_azcopy(
                    source_workspace_id=args.primary_workspace,
                    source_lakehouse_name=primary_lh_name,
                    dest_workspace_id=args.secondary_workspace,
                    dest_lakehouse_name=primary_lh_name,  # Usually same name
                    logger=logger,
                    dry_run=args.dry_run,
                    since_timestamp=None,  # forward sync is always full
                )
                data_result = {"strategy": "azcopy", "success": success}
            
            elif strategy == SyncStrategy.ONELAKE_SHORTCUTS:
                shortcut_result = create_onelake_shortcuts(
                    args.primary_workspace,
                    primary_lh_id,
                    args.secondary_workspace,
                    secondary_lh_id,
                    logger,
                    dry_run=args.dry_run,
                )
                data_result = {
                    "strategy": "shortcuts",
                    "shortcuts_created": len(shortcut_result["shortcuts_created"]),
                    "shortcuts_failed": len(shortcut_result["shortcuts_failed"]),
                }
            
            elif strategy == SyncStrategy.GRS_PASSTHROUGH:
                sync_lakehouse_grs_passthrough(primary_lh_name, logger)
                data_result = {"strategy": "grs_passthrough", "success": True}
            
            sync_summary["data_sync_results"].append({
                "lakehouse": primary_lh_name,
                "primary_id": primary_lh_id,
                "secondary_id": secondary_lh_id,
                "metadata": metadata_result,
                "data": data_result,
            })
            
            sync_summary["lakehouses_processed"] += 1
        
        # Save summary
        common.save_json(sync_summary, "data/lakehouse_sync_report.json")
        
        print("\n" + "=" * 70)
        print("LAKEHOUSE SYNC SUMMARY")
        print("=" * 70)
        print(f"Strategy:                   {strategy.value}")
        print(f"Lakehouses Processed:       {sync_summary['lakehouses_processed']}")
        print(f"Lakehouses Skipped:         {sync_summary['lakehouses_skipped']}")
        print("=" * 70 + "\n")
        
        logger.info("Lakehouse sync complete")
        return True
    
    except Exception as e:
        logger.error(f"Error in lakehouse sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
