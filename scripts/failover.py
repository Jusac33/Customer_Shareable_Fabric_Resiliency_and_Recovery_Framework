"""
Failover Orchestration Script

Purpose:
  Automate full DR activation - pause primary, sync final data,
  validate secondary, and activate secondary workspace for business continuity.

Steps:
  1. Validate secondary workspace is current
  2. Cancel running jobs & disable schedules in primary
  3. Perform final data sync pass (lakehouses, notebooks/pipelines, semantic models)
  4. Enable schedules & activate secondary workspace (from the live primary
     manifest, or the last-sync manifest when primary is unreachable)
  5. Take over semantic model ownership as the executing service principal
  6. Smoke-test secondary resources
  7. Generate failover report

Prerequisites:
  - Service Principal must have workspace admin permissions in both workspaces
  - Tenant setting 'Service principals can use Power BI APIs' must be enabled
    for the semantic model ownership takeover step to succeed

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
import bcdr_schedules

# Artifact types that support on-demand jobs and scheduling (Job Scheduler API)
SCHEDULABLE_TYPES = bcdr_schedules.SCHEDULABLE_TYPES


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


def _schedule_client() -> bcdr_schedules.ScheduleClient:
    return bcdr_schedules.ScheduleClient(common.api_call)


def _schedulable_items(workspace_id: str) -> List[Dict[str, Any]]:
    return [i for i in common.get_items(workspace_id) if i.get("type") in SCHEDULABLE_TYPES]


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
    Disable all job schedules for schedulable items in a workspace
    (Job Scheduler API). Returns a manifest of previously-enabled schedules
    (item, job type, schedule id, configuration) for re-enabling later.
    """
    res = bcdr_schedules.pause_schedules(_schedule_client(), workspace_id,
                                         _schedulable_items(workspace_id), logger, dry_run)
    return {
        "schedules_disabled": [e["item_name"] for e in res["paused"]],
        "previously_enabled": res["paused"],
        "errors": res["errors"],
    }


def enable_schedules(workspace_id: str, schedule_manifest: List[Dict], logger, dry_run: bool = False) -> Dict[str, Any]:
    """
    Re-enable, in workspace_id, the schedules a manifest marks enabled.
    Items are matched by type + name, so a manifest captured in the other
    workspace of the pair works too.
    """
    res = bcdr_schedules.activate_schedules(_schedule_client(), workspace_id, _schedulable_items(workspace_id),
                                            [{**e, "enabled": True} for e in schedule_manifest], logger, dry_run)
    return {
        "schedules_enabled": res["enabled"] + res["created"] + res["already_enabled"],
        "errors": res["errors"] + [f"{n}: no matching item" for n in res["missing_items"]],
    }


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
        # get_items() returns [] on failure, so probe reachability explicitly:
        # an unreachable primary must trigger the last-sync manifest fallback.
        common.api_call("GET", f"/workspaces/{workspace_id}")

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
        result["primary_unreachable"] = True

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
    primary_unreachable: bool = False,
) -> bool:
    """
    Activate secondary workspace: enable the schedules that were enabled on
    primary, on their secondary counterparts (matched by type + name + job
    type + configuration; created if the paused replica is missing).

    When the primary could not be read or paused (unplanned failover, primary
    region unreachable), the enabled schedules from the manifest captured at
    the last sync (data/schedule_manifest.json) are merged in, so schedules
    are activated without needing the primary.
    """
    logger.info("Activating secondary workspace...")
    manifest = list(schedule_manifest)
    if primary_unreachable:
        seen = {(e["item_type"], e["item_name"], e["job_type"],
                 json.dumps(bcdr_schedules.normalize_config(e.get("configuration")), sort_keys=True))
                for e in manifest}
        saved = [e for e in bcdr_schedules.load_manifest() if e.get("enabled")]
        for e in saved:
            k = (e["item_type"], e["item_name"], e["job_type"],
                 json.dumps(bcdr_schedules.normalize_config(e.get("configuration")), sort_keys=True))
            if k not in seen:
                manifest.append(e)
        logger.info(f"  Primary unreachable: merged {len(manifest) - len(schedule_manifest)} schedule(s) "
                    f"from the last-sync manifest ({bcdr_schedules.MANIFEST_PATH})")
    logger.info(f"  {len(manifest)} schedule(s) to enable")

    try:
        res = enable_schedules(secondary_workspace_id, manifest, logger, dry_run)
    except Exception as e:
        logger.error(f"Error activating secondary: {str(e)}")
        return False
    for err in res["errors"]:
        logger.warning(f"  Schedule not enabled: {err}")
    logger.info(f"✓ Secondary workspace activated ({len(res['schedules_enabled'])} schedules enabled"
                f"{', DRY RUN' if dry_run else ''})")
    return not res["errors"]


def _takeover_semantic_model(workspace_id: str, dataset_id: str) -> None:
    """
    Transfer semantic model ownership to the calling principal.

    Uses the Power BI Datasets API — this is the only generally available
    takeover endpoint; the Fabric item API has no equivalent.
    https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/take-over-in-group
    """
    endpoint = f"/groups/{workspace_id}/datasets/{dataset_id}/Default.TakeOver"
    common.powerbi_api_call("POST", endpoint)


def takeover_semantic_models(
    workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Take ownership of every semantic model in a workspace.

    Semantic model ownership is not carried by getDefinition/updateDefinition —
    a replicated model stays owned by whichever identity created it in DR.  The
    owner's identity backs the scheduled-refresh OAuth credentials, so if that
    owner is unavailable at failover time the DR refreshes fail.  Taking over as
    the automation service principal makes DR refreshes independent of any human
    account.

    Requires the calling principal to have workspace access and the tenant
    setting 'Service principals can use Power BI APIs' to be enabled.

    Args:
        workspace_id: Workspace GUID to take models over in (the DR workspace)
        logger: Logger instance
        dry_run: When True, no takeover calls are issued

    Returns:
        Result dict with counters and per-model detail
    """
    logger.info("Transferring semantic model ownership to the executing principal...")

    result = {
        "models_found": 0,
        "models_taken_over": 0,
        "models_failed": 0,
        "taken_over": [],
        "failures": [],
    }

    try:
        models = common.get_items(workspace_id, item_type="SemanticModel")
    except Exception as e:
        logger.error(f"Error listing semantic models: {str(e)[:200]}")
        return result

    result["models_found"] = len(models)

    if not models:
        logger.info("  No semantic models found — nothing to take over")
        return result

    for model in models:
        model_id = model.get("id")
        model_name = model.get("displayName", model_id)

        if not model_id:
            continue

        if dry_run:
            logger.info(f"[DRY RUN] Would take over semantic model '{model_name}'")
            result["models_taken_over"] += 1
            result["taken_over"].append(model_name)
            continue

        try:
            _takeover_semantic_model(workspace_id, model_id)
            logger.info(f"✓ Took over semantic model '{model_name}'")
            result["models_taken_over"] += 1
            result["taken_over"].append(model_name)
        except Exception as e:
            logger.error(f"Failed to take over '{model_name}': {str(e)[:200]}")
            result["models_failed"] += 1
            result["failures"].append({"model": model_name, "error": str(e)[:200]})

    logger.info(
        f"Semantic model takeover: "
        f"{result['models_taken_over']} succeeded, "
        f"{result['models_failed']} failed "
        f"(of {result['models_found']} found)"
    )

    return result


def refresh_semantic_models(workspace_id: str, logger, dry_run: bool = False) -> Dict[str, Any]:
    """
    Trigger a refresh of every semantic model in the DR workspace.

    Replicated models carry definitions, not data: Import models are empty and
    DirectLake models are unframed until refreshed, so reports render nothing.
    Uses the GA Power BI API (Datasets - Refresh Dataset In Group); for
    DirectLake this performs framing. Run after ownership takeover so the
    refresh executes as the DR principal.
    https://learn.microsoft.com/en-us/rest/api/power-bi/datasets/refresh-dataset-in-group
    """
    result = {"models_found": 0, "refresh_requested": 0, "refresh_failed": 0, "failures": []}
    models = [i for i in common.get_items(workspace_id, "SemanticModel")
              if not i.get("displayName", "").startswith("BCDR_")]
    result["models_found"] = len(models)
    for m in models:
        name = m.get("displayName", m.get("id"))
        if dry_run:
            logger.info(f"[DRY RUN] Would refresh semantic model '{name}'")
            result["refresh_requested"] += 1
            continue
        try:
            common.powerbi_api_call("POST", f"/groups/{workspace_id}/datasets/{m['id']}/refreshes",
                                    {"notifyOption": "NoNotification"})
            logger.info(f"✓ Refresh requested for '{name}'")
            result["refresh_requested"] += 1
        except Exception as e:
            logger.error(f"Refresh request failed for '{name}': {str(e)[:200]}")
            result["refresh_failed"] += 1
            result["failures"].append({"model": name, "error": str(e)[:200]})
    return result


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
        
        # Step 4: Activate secondary (re-enable schedules from manifest)
        logger.info("\n=== STEP 4: ACTIVATING SECONDARY WORKSPACE ===")
        if activate_secondary_workspace(args.secondary_workspace, schedule_manifest, logger, dry_run=args.dry_run,
                                        primary_unreachable=pause_result.get("primary_unreachable", False)
                                        or bool(pause_result["pipelines_failed"])):
            failover_log["status"] = "SUCCESS" if not args.dry_run else "DRY_RUN_SUCCESS"
        else:
            failover_log["status"] = "PARTIAL_FAILURE"
        
        # Step 5: Take over semantic model ownership so DR refreshes run as the SP
        logger.info("\n=== STEP 5: SEMANTIC MODEL OWNERSHIP TAKEOVER ===")
        takeover_result = takeover_semantic_models(
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        failover_log["steps"].append({
            "step": "takeover_semantic_models",
            "result": takeover_result,
        })

        # Step 5b: Refresh / frame semantic models so reports serve data in DR
        logger.info("\n=== STEP 5b: SEMANTIC MODEL REFRESH (IMPORT / DIRECTLAKE FRAMING) ===")
        refresh_result = refresh_semantic_models(args.secondary_workspace, logger, dry_run=args.dry_run)
        failover_log["steps"].append({"step": "refresh_semantic_models", "result": refresh_result})
        if refresh_result["refresh_failed"] and failover_log["status"] == "SUCCESS":
            failover_log["status"] = "PARTIAL_FAILURE"

        # Step 6: Validate secondary (smoke tests)
        logger.info("\n=== STEP 6: VALIDATING SECONDARY RESOURCES ===")
        validation_result = validate_secondary(args.secondary_workspace, logger)
        failover_log["steps"].append({
            "step": "validate_secondary",
            "result": validation_result,
        })
        
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
        print(f"Semantic Models Found:      {takeover_result['models_found']}")
        print(f"Ownership Taken Over:       {takeover_result['models_taken_over']}")
        print(f"Takeover Failures:          {takeover_result['models_failed']}")
        print(f"Model Refresh Requested:    {refresh_result['refresh_requested']}")
        print(f"Model Refresh Failures:     {refresh_result['refresh_failed']}")
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
        if takeover_result["models_failed"]:
            print("  5. Investigate semantic model takeover failures — DR scheduled")
            print("     refreshes for those models still depend on the original owner")
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
