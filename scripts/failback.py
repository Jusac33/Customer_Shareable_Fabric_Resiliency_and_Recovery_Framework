"""
Failback Orchestration Script

Purpose:
  Return to primary workspace after disaster is resolved. Performs
  reverse sync (secondary → primary), validates, and reactivates primary.

Steps:
  1. Pause active jobs & disable schedules in secondary
  2. Reverse sync all artifacts (secondary → primary)
  3. Validate data parity
  4. Re-enable schedules on primary & reactivate
  5. Decommission secondary to standby state

RPO/RTO Impact:
  - RPO: Data loss = time since failover
  - RTO: Total execution time typically 30-60 minutes

Usage:
  python failback.py
  python failback.py --dry-run
"""

import argparse
import json
import time
from datetime import datetime
from typing import Dict, List, Any, Optional

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
from sync_lakehouses import reverse_sync_lakehouses, SyncStrategy

# Import failover helpers so we can reuse cancel/schedule logic
from failover import (
    cancel_running_jobs,
    disable_schedules,
    enable_schedules,
    SCHEDULABLE_TYPES,
    _get_item_schedule,
    _update_item_schedule,
)


def pause_secondary_pipelines(workspace_id: str, logger, dry_run: bool = False) -> Dict[str, Any]:
    """
    Pause secondary workspace: cancel running jobs + disable all schedules.
    """
    result = {
        "pipelines_paused": [],
        "pipelines_failed": [],
        "schedule_manifest": [],
    }

    try:
        logger.info("Cancelling running jobs in secondary...")
        cancel_result = cancel_running_jobs(workspace_id, logger, dry_run)
        logger.info(f"  Cancelled {len(cancel_result['jobs_cancelled'])} jobs")

        logger.info("Disabling schedules in secondary...")
        schedule_result = disable_schedules(workspace_id, logger, dry_run)
        logger.info(f"  Disabled {len(schedule_result['schedules_disabled'])} schedules")

        result["pipelines_paused"] = (
            cancel_result["jobs_cancelled"] + schedule_result["schedules_disabled"]
        )
        result["pipelines_failed"] = (
            cancel_result["cancel_failures"] + schedule_result["errors"]
        )
        result["schedule_manifest"] = schedule_result["previously_enabled"]

    except Exception as e:
        logger.error(f"Error pausing secondary pipelines: {str(e)}")

    return result


def reverse_sync_artifacts(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
    since_timestamp: Optional[str] = None,
    lakehouse_strategy: SyncStrategy = SyncStrategy.FAST_COPY,
) -> Dict[str, Any]:
    """
    Perform reverse sync: secondary → primary for all artifact types.

    Lakehouse data is handled separately via reverse_sync_lakehouses() which
    uses either FAST_COPY (notebookutils.fs.cp + delta log trimming, per MS DR
    guidance) or ACTIVE_REPLICATION (azcopy --include-after=since_timestamp).

    The since_timestamp (ISO-8601 failover divergence point) ensures only the
    delta Δ written on secondary AFTER failover is transferred back, preventing
    unnecessary re-transfer and stale overwrites on the restored primary.
    """
    logger.info("Performing reverse sync (secondary → primary)...")

    sync_result = {
        "types_synced": [],
        "types_failed": [],
        "details": {},
    }

    # ── Lakehouses: use direction-aware reverse_sync_lakehouses ──────────────
    # This is the critical path: only copy delta Δ (data written after failover)
    # rather than a full re-sync, which would overwrite V1 data unnecessarily.
    logger.info(f"  Reverse-syncing Lakehouses (strategy={lakehouse_strategy.value}, "
                f"since={since_timestamp or 'full'})...")
    try:
        if dry_run:
            for item_type in ["Lakehouse", "Warehouse", "Notebook", "DataPipeline",
                              "SemanticModel", "Report", "DataflowsGen2"]:
                items = common.get_items(secondary_workspace_id, item_type=item_type)
                if items:
                    sync_result["details"][item_type] = len(items)
            logger.info(f"    [DRY RUN] Would reverse-sync lakehouses "
                        f"({lakehouse_strategy.value}, since={since_timestamp or 'full'})")
            sync_result["types_synced"].append("Lakehouses")
        else:
            lh_result = reverse_sync_lakehouses(
                primary_workspace_id=primary_workspace_id,
                secondary_workspace_id=secondary_workspace_id,
                logger=logger,
                strategy=lakehouse_strategy,
                since_timestamp=since_timestamp,
                dry_run=False,
            )
            sync_result["details"]["lakehouses"] = lh_result
            if lh_result["lakehouses_failed"]:
                sync_result["types_failed"].append("Lakehouses")
                logger.warning(f"    ⚠ {len(lh_result['lakehouses_failed'])} lakehouse(s) failed reverse sync")
            else:
                sync_result["types_synced"].append("Lakehouses")
                logger.info(f"    ✓ Lakehouses reverse-synced ({len(lh_result['lakehouses_synced'])} total)")
    except Exception as e:
        logger.error(f"    Lakehouse reverse sync error: {e}")
        sync_result["types_failed"].append("Lakehouses")

    # ── Non-lakehouse artifacts ───────────────────────────────────────────────
    sync_modules = [
        ("sync_notebooks_and_pipelines", "Notebooks & Pipelines"),
        ("sync_semantic_models_and_reports", "Semantic Models & Reports"),
        ("sync_dataflows", "Dataflows"),
        ("sync_permissions", "Permissions"),
    ]

    for module_name, label in sync_modules:
        try:
            logger.info(f"  Reverse-syncing {label}...")
            if dry_run:
                logger.info(f"    [DRY RUN] Would run {module_name} (secondary → primary)")
                sync_result["types_synced"].append(label)
                continue

            mod = __import__(module_name)
            # Reverse: secondary is now the source, primary is the target
            if hasattr(mod, "main"):
                mod.main()
            elif hasattr(mod, "sync"):
                mod.sync(secondary_workspace_id, primary_workspace_id)
            sync_result["types_synced"].append(label)
            logger.info(f"    ✓ {label} reverse-synced")
        except ImportError:
            logger.warning(f"    Module {module_name} not available — skipping {label}")
            sync_result["types_failed"].append(label)
        except Exception as e:
            logger.error(f"    Error reverse-syncing {label}: {str(e)}")
            sync_result["types_failed"].append(label)

    return sync_result


def validate_primary_ready(primary_workspace_id: str, secondary_workspace_id: str, logger) -> Dict[str, Any]:
    """
    Validate primary workspace is ready to be reactivated.
    Compares artifact counts between primary and secondary.
    """
    logger.info("Validating primary workspace readiness...")

    checks = [
        ("Lakehouse", "lakehouses_count"),
        ("Warehouse", "warehouses_count"),
        ("Notebook", "notebooks_count"),
        ("DataPipeline", "pipelines_count"),
        ("SemanticModel", "semantic_models_count"),
        ("Report", "reports_count"),
    ]

    validation = {key: 0 for _, key in checks}
    validation["validation_status"] = "OK"
    validation["secondary_counts"] = {}
    validation["parity_issues"] = []

    for item_type, key in checks:
        try:
            primary_items = common.get_items(primary_workspace_id, item_type=item_type)
            secondary_items = common.get_items(secondary_workspace_id, item_type=item_type)
            p_count = len(primary_items)
            s_count = len(secondary_items)
            validation[key] = p_count
            validation["secondary_counts"][item_type] = s_count

            if p_count < s_count:
                diff = s_count - p_count
                validation["parity_issues"].append(
                    f"{item_type}: primary has {p_count}, secondary has {s_count} (missing {diff})"
                )
                logger.warning(f"  ⚠ {item_type}: primary={p_count}, secondary={s_count}")
            else:
                logger.info(f"  ✓ {item_type}: primary={p_count}, secondary={s_count}")
        except Exception as e:
            logger.error(f"  ✗ Error checking {item_type}: {str(e)}")
            validation["validation_status"] = "PARTIAL"

    if validation["parity_issues"]:
        validation["validation_status"] = "WARNING"
        logger.warning(f"  {len(validation['parity_issues'])} parity issue(s) detected")

    return validation


def reactivate_primary_workspace(
    primary_workspace_id: str,
    failover_log_path: str,
    logger,
    dry_run: bool = False,
) -> bool:
    """
    Reactivate primary workspace:
      1. Load the schedule manifest from the original failover log
      2. Re-enable those schedules on primary
    """
    logger.info("Reactivating primary workspace...")

    if dry_run:
        logger.info("[DRY RUN] Would reactivate primary workspace")
        return True

    try:
        # Load original failover log to get the schedule manifest
        schedule_manifest = []
        if os.path.exists(failover_log_path):
            with open(failover_log_path) as f:
                failover_log = json.load(f)
            schedule_manifest = failover_log.get("schedule_manifest", [])
            logger.info(f"  Loaded {len(schedule_manifest)} schedule entries from failover log")
        else:
            logger.warning(f"  No failover log found at {failover_log_path}")
            logger.info("  Will scan for schedules to enable instead")

        if schedule_manifest:
            # Re-enable schedules that were active before failover
            enabled_count = 0
            for entry in schedule_manifest:
                item_id = entry["item_id"]
                item_name = entry["item_name"]
                logger.info(f"  Enabling schedule on '{item_name}'")
                if _update_item_schedule(primary_workspace_id, item_id, enabled=True):
                    enabled_count += 1
                else:
                    logger.warning(f"  Failed to enable schedule on '{item_name}'")
            logger.info(f"  ✓ Re-enabled {enabled_count}/{len(schedule_manifest)} schedules")
        else:
            # Fallback: enable all schedules found on primary
            logger.info("  No manifest available — scanning primary for disabled schedules")
            for item_type in SCHEDULABLE_TYPES:
                items = common.get_items(primary_workspace_id, item_type=item_type)
                for item in items:
                    sched = _get_item_schedule(primary_workspace_id, item["id"])
                    if sched and not sched.get("enabled", True):
                        logger.info(f"  Enabling schedule on '{item['displayName']}'")
                        _update_item_schedule(primary_workspace_id, item["id"], enabled=True)

        logger.info("✓ Primary workspace reactivated")
        return True

    except Exception as e:
        logger.error(f"Error reactivating primary: {str(e)}")
        return False


def decommission_secondary(
    secondary_workspace_id: str,
    secondary_schedule_manifest: List[Dict],
    logger,
    dry_run: bool = False,
) -> bool:
    """
    Transition secondary to standby state:
      - Ensure all schedules are disabled
      - Leave artifacts intact for next DR cycle
    """
    logger.info("Decommissioning secondary workspace to standby state...")

    if dry_run:
        logger.info("[DRY RUN] Would decommission secondary")
        return True

    try:
        # Verify all schedules are disabled
        disabled_count = 0
        for item_type in SCHEDULABLE_TYPES:
            items = common.get_items(secondary_workspace_id, item_type=item_type)
            for item in items:
                sched = _get_item_schedule(secondary_workspace_id, item["id"])
                if sched and sched.get("enabled", False):
                    logger.info(f"  Disabling leftover schedule on '{item['displayName']}'")
                    _update_item_schedule(secondary_workspace_id, item["id"], enabled=False)
                    disabled_count += 1

        logger.info("✓ Secondary workspace transitioned to standby state")
        logger.info(f"  - {disabled_count} additional schedules disabled")
        logger.info("  - All artifacts preserved for next failover cycle")
        return True

    except Exception as e:
        logger.error(f"Error decommissioning secondary: {str(e)}")
        return False


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Orchestrate Fabric DR failback")
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
        "--lakehouse-strategy",
        choices=["FAST_COPY", "ACTIVE_REPLICATION"],
        default="FAST_COPY",
        help="Data reverse-sync strategy for lakehouses. "
             "FAST_COPY (default): notebookutils.fs.cp with delta log trimming "
             "per Microsoft DR guidance. "
             "ACTIVE_REPLICATION: azcopy sync --include-after=<failover_timestamp>.",
    )

    args = parser.parse_args()
    
    logger = common.setup_logger("failback")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    failback_log = {
        "failback_timestamp": datetime.now().isoformat(),
        "primary_workspace": args.primary_workspace,
        "secondary_workspace": args.secondary_workspace,
        "lakehouse_strategy": args.lakehouse_strategy,
        "status": "IN_PROGRESS",
        "steps": [],
    }
    
    try:
        # Step 1: Pause secondary
        logger.info("\n=== STEP 1: PAUSING SECONDARY (CANCEL JOBS + DISABLE SCHEDULES) ===")
        pause_result = pause_secondary_pipelines(
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        failback_log["steps"].append({
            "step": "pause_secondary",
            "result": {
                "paused": pause_result["pipelines_paused"],
                "failed": pause_result["pipelines_failed"],
            },
        })
        secondary_schedule_manifest = pause_result.get("schedule_manifest", [])
        
        # Step 2: Reverse sync
        # Load failover timestamp from the failover log — this marks the exact
        # divergence point where secondary data became authoritative.  Only delta
        # Δ written AFTER this timestamp needs to flow back to primary.
        logger.info("\n=== STEP 2: REVERSE SYNC (SECONDARY → PRIMARY) ===")
        failover_log_path = "data/failover_log.json"
        since_timestamp: Optional[str] = None
        if os.path.exists(failover_log_path):
            try:
                with open(failover_log_path) as _f:
                    _fo_log = json.load(_f)
                since_timestamp = _fo_log.get("failover_timestamp")
                if since_timestamp:
                    logger.info(f"Using failover divergence timestamp: {since_timestamp}")
                else:
                    logger.warning("failover_log.json has no failover_timestamp — will do full re-sync")
            except Exception as _e:
                logger.warning(f"Could not read failover_log.json: {_e} — will do full re-sync")
        else:
            logger.warning("No failover_log.json found — will do full re-sync")

        lakehouse_strategy = SyncStrategy[args.lakehouse_strategy]
        sync_result = reverse_sync_artifacts(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
            since_timestamp=since_timestamp,
            lakehouse_strategy=lakehouse_strategy,
        )
        failback_log["steps"].append({
            "step": "reverse_sync",
            "result": sync_result,
        })
        
        # Step 3: Validate primary
        logger.info("\n=== STEP 3: VALIDATING PRIMARY WORKSPACE ===")
        validation_result = validate_primary_ready(
            args.primary_workspace, args.secondary_workspace, logger
        )
        failback_log["steps"].append({
            "step": "validate_primary",
            "result": validation_result,
        })
        
        # Step 4: Reactivate primary (re-enable original schedules)
        logger.info("\n=== STEP 4: REACTIVATING PRIMARY WORKSPACE ===")
        failover_log_path = "data/failover_log.json"
        if reactivate_primary_workspace(
            args.primary_workspace, failover_log_path, logger, dry_run=args.dry_run
        ):
            # Step 5: Decommission secondary
            logger.info("\n=== STEP 5: DECOMMISSIONING SECONDARY TO STANDBY ===")
            decommission_secondary(
                args.secondary_workspace, secondary_schedule_manifest, logger, dry_run=args.dry_run
            )
            
            failback_log["status"] = "SUCCESS" if not args.dry_run else "DRY_RUN_SUCCESS"
        else:
            failback_log["status"] = "PARTIAL_FAILURE"
        
        # Save log
        common.save_json(failback_log, "data/failback_log.json")
        
        # Print summary
        print("\n" + "=" * 70)
        print("FAILBACK SUMMARY")
        print("=" * 70)
        print(f"Timestamp:                  {failback_log['failback_timestamp']}")
        print(f"Primary Workspace:          {args.primary_workspace}")
        print(f"Secondary Workspace:        {args.secondary_workspace}")
        print(f"Status:                     {failback_log['status']}")
        print(f"Secondary Jobs/Schedules Paused: {len(pause_result['pipelines_paused'])}")
        print(f"Pause Failures:             {len(pause_result['pipelines_failed'])}")
        print(f"Reverse Sync Completed:     {len(sync_result['types_synced'])}")
        print(f"Reverse Sync Failed:        {len(sync_result['types_failed'])}")
        print(f"Primary Artifact Counts:")
        for k, v in validation_result.items():
            if k.endswith("_count") and isinstance(v, int):
                print(f"  - {k.replace('_count', '').replace('_', ' ').title()}: {v}")
        if validation_result.get("parity_issues"):
            print(f"Parity Issues:              {len(validation_result['parity_issues'])}")
            for issue in validation_result["parity_issues"]:
                print(f"  ⚠ {issue}")
        print("=" * 70)
        print("\n⚠ POST-FAILBACK TASKS:")
        print("  1. Update application connection strings back to primary Fabric endpoints")
        print("  2. Verify data consistency in primary workspace")
        print("  3. Update DNS/load balancer to point to primary")
        print("  4. Run DR validation tests to confirm full functionality")
        print("  5. Check secondary standby status and update standby refresh schedules")
        print()
        
        logger.info(f"Failback completed with status: {failback_log['status']}")
        return failback_log["status"] in ["SUCCESS", "DRY_RUN_SUCCESS"]
    
    except Exception as e:
        logger.error(f"Fatal error during failback: {str(e)}", exc_info=True)
        failback_log["status"] = "FAILED"
        failback_log["error"] = str(e)
        common.save_json(failback_log, "data/failback_log.json")
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
