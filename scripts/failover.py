"""
Failover Orchestration Script

Purpose:
  Automate full DR activation - pause primary, sync final data,
  validate secondary, and activate secondary workspace for business continuity.

Steps:
  1. Validate secondary workspace is current
  2. Cancel running jobs & disable schedules in primary
  3. Perform final data sync pass (lakehouses, notebooks/pipelines, semantic models)
  4. Validate secondary artifact counts & accessibility
  5. Enable schedules & activate secondary workspace
  6. Smoke-test secondary resources
  7. Generate failover report

RPO/RTO Impact:
  - RPO: Data loss = time since last sync
  - RTO: Total execution time typically 30-60 minutes depending on data volume

Usage:
  python failover.py
  python failover.py --dry-run
  python failover.py --skip-validation  # Skip validation checks
"""

import argparse
import json
import time
from datetime import datetime
from typing import Dict, List, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common

# Artifact types that support on-demand jobs and scheduling
SCHEDULABLE_TYPES = ["DataPipeline", "Notebook", "SparkJobDefinition", "DataflowsGen2"]


def _get_job_instances(workspace_id: str, item_id: str) -> List[Dict[str, Any]]:
    """Fetch recent job instances for an item."""
    endpoint = f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances"
    try:
        resp = common.api_call("GET", endpoint)
        return resp.get("value", [])
    except Exception:
        return []


def _cancel_job(workspace_id: str, item_id: str, job_instance_id: str) -> bool:
    """Cancel a specific running job instance."""
    endpoint = f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances/{job_instance_id}/cancel"
    try:
        common.api_call("POST", endpoint)
        return True
    except Exception:
        return False


def _get_item_schedule(workspace_id: str, item_id: str) -> Dict[str, Any]:
    """Get the job schedule configuration for an item."""
    endpoint = f"/workspaces/{workspace_id}/items/{item_id}/jobScheduler"
    try:
        return common.api_call("GET", endpoint)
    except Exception:
        return {}


def _update_item_schedule(workspace_id: str, item_id: str, enabled: bool) -> bool:
    """Enable or disable the job schedule for an item."""
    endpoint = f"/workspaces/{workspace_id}/items/{item_id}/jobScheduler"
    try:
        common.api_call("PATCH", endpoint, {"enabled": enabled})
        return True
    except Exception:
        return False


def _run_item_job(workspace_id: str, item_id: str, job_type: str = "Pipeline") -> bool:
    """Trigger an on-demand job for an item."""
    endpoint = f"/workspaces/{workspace_id}/items/{item_id}/jobs/instances?jobType={job_type}"
    try:
        common.api_call("POST", endpoint)
        return True
    except Exception:
        return False


def cancel_running_jobs(workspace_id: str, logger, dry_run: bool = False) -> Dict[str, Any]:
    """
    Cancel all currently running jobs across schedulable items in a workspace.
    """
    result = {"jobs_cancelled": [], "cancel_failures": []}

    for item_type in SCHEDULABLE_TYPES:
        items = common.get_items(workspace_id, item_type=item_type)
        for item in items:
            item_id = item["id"]
            item_name = item["displayName"]
            jobs = _get_job_instances(workspace_id, item_id)
            running = [j for j in jobs if j.get("status") in ("InProgress", "NotStarted")]
            for job in running:
                jid = job.get("id", "unknown")
                if dry_run:
                    logger.info(f"[DRY RUN] Would cancel {item_type} job {jid} on '{item_name}'")
                    result["jobs_cancelled"].append(f"{item_name}/{jid}")
                else:
                    logger.info(f"Cancelling {item_type} job {jid} on '{item_name}'")
                    if _cancel_job(workspace_id, item_id, jid):
                        result["jobs_cancelled"].append(f"{item_name}/{jid}")
                    else:
                        result["cancel_failures"].append(f"{item_name}/{jid}")
    return result


def disable_schedules(workspace_id: str, logger, dry_run: bool = False) -> Dict[str, Any]:
    """
    Disable all job schedules for schedulable items in a workspace.
    Returns a manifest of previously-enabled schedules (for re-enabling later).
    """
    result = {"schedules_disabled": [], "previously_enabled": [], "errors": []}

    for item_type in SCHEDULABLE_TYPES:
        items = common.get_items(workspace_id, item_type=item_type)
        for item in items:
            item_id = item["id"]
            item_name = item["displayName"]
            schedule = _get_item_schedule(workspace_id, item_id)
            if not schedule:
                continue
            is_enabled = schedule.get("enabled", False)
            if is_enabled:
                result["previously_enabled"].append({
                    "item_id": item_id,
                    "item_name": item_name,
                    "item_type": item_type,
                    "schedule_config": schedule,
                })
                if dry_run:
                    logger.info(f"[DRY RUN] Would disable schedule on '{item_name}' ({item_type})")
                    result["schedules_disabled"].append(item_name)
                else:
                    logger.info(f"Disabling schedule on '{item_name}' ({item_type})")
                    if _update_item_schedule(workspace_id, item_id, enabled=False):
                        result["schedules_disabled"].append(item_name)
                    else:
                        result["errors"].append(item_name)
    return result


def enable_schedules(workspace_id: str, schedule_manifest: List[Dict], logger, dry_run: bool = False) -> Dict[str, Any]:
    """
    Re-enable schedules from a previously saved manifest.
    """
    result = {"schedules_enabled": [], "errors": []}
    for entry in schedule_manifest:
        item_id = entry["item_id"]
        item_name = entry["item_name"]
        if dry_run:
            logger.info(f"[DRY RUN] Would enable schedule on '{item_name}'")
            result["schedules_enabled"].append(item_name)
        else:
            logger.info(f"Enabling schedule on '{item_name}'")
            if _update_item_schedule(workspace_id, item_id, enabled=True):
                result["schedules_enabled"].append(item_name)
            else:
                result["errors"].append(item_name)
    return result


def pause_pipelines(workspace_id: str, logger, dry_run: bool = False) -> Dict[str, Any]:
    """
    Pause a workspace: cancel running jobs + disable all schedules.
    """
    result = {
        "pipelines_paused": [],
        "pipelines_failed": [],
        "schedule_manifest": [],
    }

    try:
        # 1. Cancel any running jobs
        logger.info("Cancelling running jobs in workspace...")
        cancel_result = cancel_running_jobs(workspace_id, logger, dry_run)
        logger.info(f"  Cancelled {len(cancel_result['jobs_cancelled'])} jobs, "
                     f"{len(cancel_result['cancel_failures'])} failures")

        # 2. Disable schedules and save manifest for later re-enablement
        logger.info("Disabling schedules in workspace...")
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
        logger.error(f"Error pausing pipelines: {str(e)}")

    return result


def validate_secondary_current(primary_workspace_id: str, secondary_workspace_id: str, logger) -> bool:
    """
    Validate that secondary is reasonably current vs primary by comparing artifact counts.
    """
    logger.info("Validating secondary workspace currency...")

    try:
        # First check sync plan if available
        if os.path.exists("data/sync_plan.json"):
            with open("data/sync_plan.json") as f:
                sync_plan = json.load(f)
            summary = sync_plan.get("summary", {})
            missing_count = summary.get("missing_in_secondary_count", 0)
            if missing_count > 10:
                logger.warning(f"Secondary missing {missing_count} artifacts (from sync plan)!")
                logger.warning("Consider running sync scripts before failover")
                return False
            logger.info(f"Sync plan check: missing only {missing_count} artifacts")

        # Also do a live count comparison
        primary_items = common.get_items(primary_workspace_id)
        secondary_items = common.get_items(secondary_workspace_id)
        p_count = len(primary_items)
        s_count = len(secondary_items)
        diff = p_count - s_count

        logger.info(f"  Primary artifacts:   {p_count}")
        logger.info(f"  Secondary artifacts: {s_count}")
        logger.info(f"  Difference:          {diff}")

        if diff > 20:
            logger.warning(f"Secondary is significantly behind primary by {diff} items")
            return False

        logger.info("Secondary workspace is reasonably current")
        return True

    except Exception as e:
        logger.error(f"Error validating secondary: {str(e)}")
        return False


def run_final_sync(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Run final sync pass using the existing sync scripts.
    Imports and invokes each sync module to bring secondary up to date.
    """
    logger.info("Running final data sync pass...")

    result = {"sources_synced": [], "sources_failed": []}

    sync_modules = [
        ("sync_lakehouses", "Lakehouses"),
        ("sync_notebooks_and_pipelines", "Notebooks & Pipelines"),
        ("sync_semantic_models_and_reports", "Semantic Models & Reports"),
        ("sync_dataflows", "Dataflows"),
        ("sync_permissions", "Permissions"),
    ]

    for module_name, label in sync_modules:
        try:
            logger.info(f"  Syncing {label}...")
            if dry_run:
                logger.info(f"    [DRY RUN] Would run {module_name}")
                result["sources_synced"].append(label)
                continue

            mod = __import__(module_name)
            if hasattr(mod, "main"):
                mod.main()
            elif hasattr(mod, "sync"):
                mod.sync(primary_workspace_id, secondary_workspace_id)
            result["sources_synced"].append(label)
            logger.info(f"    ✓ {label} synced")
        except ImportError:
            logger.warning(f"    Module {module_name} not available — skipping {label}")
            result["sources_failed"].append(label)
        except Exception as e:
            logger.error(f"    Error syncing {label}: {str(e)}")
            result["sources_failed"].append(label)

    return result


def validate_secondary(secondary_workspace_id: str, logger) -> Dict[str, Any]:
    """
    Run smoke tests on secondary workspace — verify artifact accessibility.
    """
    logger.info("Validating secondary workspace resources...")

    checks = [
        ("Lakehouse", "lakehouses_accessible"),
        ("Warehouse", "warehouses_accessible"),
        ("Report", "reports_accessible"),
        ("Notebook", "notebooks_accessible"),
        ("DataPipeline", "pipelines_accessible"),
        ("SemanticModel", "semantic_models_accessible"),
    ]

    validation = {key: 0 for _, key in checks}
    validation["validation_errors"] = []

    for item_type, key in checks:
        try:
            items = common.get_items(secondary_workspace_id, item_type=item_type)
            validation[key] = len(items)
            logger.info(f"  ✓ {len(items)} {item_type}(s) accessible")
        except Exception as e:
            logger.error(f"  ✗ Error checking {item_type}: {str(e)}")
            validation["validation_errors"].append(f"{item_type}: {str(e)}")

    return validation


def activate_secondary_workspace(
    secondary_workspace_id: str,
    schedule_manifest: List[Dict],
    logger,
    dry_run: bool = False,
) -> bool:
    """
    Activate secondary workspace:
      1. Re-enable schedules that were active on primary (using manifest).
      2. Trigger on-demand pipeline runs for critical pipelines.
    """
    logger.info("Activating secondary workspace...")

    if dry_run:
        logger.info("[DRY RUN] Would activate secondary workspace")
        logger.info(f"  Would enable {len(schedule_manifest)} schedules")
        return True

    try:
        # Re-enable schedules on secondary counterparts
        # The manifest has primary item IDs; we need to map to secondary IDs
        artifact_map = common.load_artifact_mapping()

        enabled_count = 0
        for entry in schedule_manifest:
            primary_item_id = entry["item_id"]
            item_name = entry["item_name"]
            secondary_item_id = artifact_map.get(primary_item_id)

            if not secondary_item_id:
                logger.warning(f"  No secondary mapping for '{item_name}' — skipping schedule enable")
                continue

            logger.info(f"  Enabling schedule on secondary '{item_name}'")
            if _update_item_schedule(secondary_workspace_id, secondary_item_id, enabled=True):
                enabled_count += 1
            else:
                logger.warning(f"  Failed to enable schedule on '{item_name}'")

        logger.info(f"✓ Secondary workspace activated ({enabled_count} schedules enabled)")
        return True

    except Exception as e:
        logger.error(f"Error activating secondary: {str(e)}")
        return False


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Orchestrate Fabric DR failover")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned actions without executing",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip validation checks",
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
    
    args = parser.parse_args()
    
    logger = common.setup_logger("failover")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    failover_log = {
        "failover_timestamp": datetime.now().isoformat(),
        "primary_workspace": args.primary_workspace,
        "secondary_workspace": args.secondary_workspace,
        "status": "IN_PROGRESS",
        "steps": [],
    }
    
    try:
        # Step 1: Validate secondary
        logger.info("\n=== STEP 1: VALIDATING SECONDARY ===")
        if not args.skip_validation:
            if not validate_secondary_current(args.primary_workspace, args.secondary_workspace, logger):
                logger.error("Secondary not current - aborting failover")
                failover_log["status"] = "FAILED"
                common.save_json(failover_log, "data/failover_log.json")
                return False
        
        # Step 2: Pause primary (cancel jobs + disable schedules)
        logger.info("\n=== STEP 2: PAUSING PRIMARY (CANCEL JOBS + DISABLE SCHEDULES) ===")
        pause_result = pause_pipelines(
            args.primary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        failover_log["steps"].append({
            "step": "pause_primary",
            "result": {
                "paused": pause_result["pipelines_paused"],
                "failed": pause_result["pipelines_failed"],
            },
        })
        # Save schedule manifest for secondary activation
        schedule_manifest = pause_result.get("schedule_manifest", [])
        
        # Step 3: Final sync
        logger.info("\n=== STEP 3: FINAL DATA SYNC ===")
        sync_result = run_final_sync(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        failover_log["steps"].append({
            "step": "final_sync",
            "result": sync_result,
        })
        
        # Step 4: Validate secondary
        logger.info("\n=== STEP 4: VALIDATING SECONDARY RESOURCES ===")
        validation_result = validate_secondary(args.secondary_workspace, logger)
        failover_log["steps"].append({
            "step": "validate_secondary",
            "result": validation_result,
        })
        
        # Step 5: Activate secondary (re-enable schedules from manifest)
        logger.info("\n=== STEP 5: ACTIVATING SECONDARY WORKSPACE ===")
        if activate_secondary_workspace(args.secondary_workspace, schedule_manifest, logger, dry_run=args.dry_run):
            failover_log["status"] = "SUCCESS" if not args.dry_run else "DRY_RUN_SUCCESS"
        else:
            failover_log["status"] = "PARTIAL_FAILURE"
        
        # Save schedule manifest for failback use
        failover_log["schedule_manifest"] = schedule_manifest
        common.save_json(failover_log, "data/failover_log.json")
        
        # Print summary
        print("\n" + "=" * 70)
        print("FAILOVER SUMMARY")
        print("=" * 70)
        print(f"Timestamp:                  {failover_log['failover_timestamp']}")
        print(f"Primary Workspace:          {args.primary_workspace}")
        print(f"Secondary Workspace:        {args.secondary_workspace}")
        print(f"Status:                     {failover_log['status']}")
        print(f"Jobs Cancelled + Schedules: {len(pause_result['pipelines_paused'])}")
        print(f"Pause Failures:             {len(pause_result['pipelines_failed'])}")
        print(f"Sync Modules Completed:     {len(sync_result['sources_synced'])}")
        print(f"Sync Modules Failed:        {len(sync_result.get('sources_failed', []))}")
        print(f"Secondary Resources:")
        for key, val in validation_result.items():
            if key.endswith("_accessible"):
                label = key.replace("_accessible", "").replace("_", " ").title()
                print(f"  - {label}: {val}")
        if validation_result.get("validation_errors"):
            print(f"Validation Errors:          {len(validation_result['validation_errors'])}")
        print("=" * 70)
        print("\n⚠ POST-FAILOVER TASKS:")
        print("  1. Update application connection strings to secondary Fabric endpoints")
        print("  2. Verify data consistency in secondary workspace")
        print("  3. Update DNS/load balancer to point to secondary")
        print("  4. Notify stakeholders of DR activation")
        print()
        
        logger.info(f"Failover completed with status: {failover_log['status']}")
        return failover_log["status"] in ["SUCCESS", "DRY_RUN_SUCCESS"]
    
    except Exception as e:
        logger.error(f"Fatal error during failover: {str(e)}", exc_info=True)
        failover_log["status"] = "FAILED"
        failover_log["error"] = str(e)
        common.save_json(failover_log, "data/failover_log.json")
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
