"""
Sync ML Models and Experiments

Fabric-specific (AI/ML layer)

Purpose:
  BCDR for MLflow Models, Experiments, Spark Job Definitions, and Data Agents.
  Environment sync is delegated to sync_environments.py (which includes the
  required publish step to activate Spark settings & libraries).

Artifact Types Covered:
  MLModel, MLExperiment, SparkJobDefinition, GraphQLApi (Data Agent)

RPO/RTO:
  RPO: Last model/experiment export
  RTO: Minutes

Prerequisites:
  - azcopy for model artifact sync (optional)
  - OneLake credentials for artifact storage

Usage:
  python sync_ml_models_and_experiments.py
  python sync_ml_models_and_experiments.py --dry-run
"""

import argparse
import csv
import json
import subprocess
from typing import Dict, List, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common

# Delegate environment sync to the dedicated module (includes publish step)
from sync_environments import sync_environments as _sync_environments_dedicated


def sync_spark_jobs(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync Spark Job Definitions from primary to secondary.
    
    Args:
        primary_workspace_id: Primary workspace GUID
        secondary_workspace_id: Secondary workspace GUID
        logger: Logger instance
        dry_run: If True, don't execute
        
    Returns:
        Sync result
    """
    result = {
        "jobs_synced": [],
        "jobs_failed": [],
    }
    
    try:
        # Get primary job definitions
        primary_jobs = common.get_items(
            primary_workspace_id,
            item_type="SparkJobDefinition",
        )
        logger.info(f"Found {len(primary_jobs)} Spark job definitions in primary")
        
        # Get secondary jobs
        secondary_jobs = common.get_items(
            secondary_workspace_id,
            item_type="SparkJobDefinition",
        )
        secondary_by_name = {job["displayName"]: job for job in secondary_jobs}
        
        # Load combined mapping for remapping
        mapping = common.build_combined_mapping()
        
        for primary_job in primary_jobs:
            job_name = primary_job["displayName"]
            job_id = primary_job["id"]
            
            logger.info(f"Processing Spark job definition: {job_name}")
            
            try:
                # Export definition
                definition = common.export_item_definition(
                    primary_workspace_id,
                    job_id,
                )
                
                # Remap references (base64-aware)
                definition = common.remap_definition(definition, mapping)
                
                if dry_run:
                    logger.info(f"[DRY RUN] Would sync job definition: {job_name}")
                    result["jobs_synced"].append(job_name)
                else:
                    # Check if exists
                    if job_name in secondary_by_name:
                        secondary_job_id = secondary_by_name[job_name]["id"]
                        common.update_item_definition(
                            secondary_workspace_id,
                            secondary_job_id,
                            definition,
                        )
                        logger.info(f"✓ Updated job definition: {job_name}")
                    else:
                        common.import_item(
                            secondary_workspace_id,
                            job_name,
                            "SparkJobDefinition",
                            definition,
                        )
                        logger.info(f"✓ Created job definition: {job_name}")
                    
                    result["jobs_synced"].append(job_name)
            
            except Exception as e:
                logger.error(f"Failed to sync job definition {job_name}: {str(e)}")
                result["jobs_failed"].append({
                    "job": job_name,
                    "error": str(e),
                })
    
    except Exception as e:
        logger.error(f"Error syncing Spark job definitions: {str(e)}")
    
    return result


def _update_artifact_mapping(item_name: str, item_type: str,
                             primary_id: str, new_secondary_id: str,
                             logger) -> None:
    """Update artifact_mapping.csv when a secondary item ID changes (delete+recreate)."""
    csv_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "artifact_mapping.csv",
    )
    if not os.path.exists(csv_path):
        logger.warning(f"  artifact_mapping.csv not found at {csv_path}")
        return

    rows = []
    updated = False
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if (row.get("artifact_type") == item_type
                    and row.get("primary_artifact_id") == primary_id):
                old_id = row.get("secondary_artifact_id", "")
                row["secondary_artifact_id"] = new_secondary_id
                updated = True
                logger.info(f"  Updated artifact_mapping: {item_name} secondary_id {old_id[:8]}… → {new_secondary_id[:8]}…")
            rows.append(row)

    if updated and fieldnames:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    elif not updated:
        logger.info(f"  No matching row in artifact_mapping.csv for {item_name} — skipping CSV update")


def _azcopy_ml_item(
    primary_workspace_id: str,
    primary_item_id: str,
    secondary_workspace_id: str,
    secondary_item_id: str,
    logger,
) -> bool:
    """Copy OneLake artifacts for an ML item using azcopy.

    Returns True on success, False on failure.
    """
    source = (
        f"https://onelake.dfs.fabric.microsoft.com/"
        f"{primary_workspace_id}/{primary_item_id}/*"
    )
    dest = (
        f"https://onelake.dfs.fabric.microsoft.com/"
        f"{secondary_workspace_id}/{secondary_item_id}"
    )

    azcopy_bin = "azcopy"
    # Try to find azcopy in common locations
    for candidate in ["azcopy", "azcopy.exe"]:
        if subprocess.run(
            [candidate, "--version"],
            capture_output=True, timeout=10,
        ).returncode == 0:
            azcopy_bin = candidate
            break

    cmd = [
        azcopy_bin, "copy",
        source, dest,
        "--recursive",
        "--overwrite=ifSourceNewer",
        "--exclude-pattern=.platform",
        "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com",
    ]

    logger.info(f"  azcopy ML artifacts: {source} → {dest}")
    try:
        env = os.environ.copy()
        env["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=env)
        if proc.returncode == 0:
            # Extract summary
            for line in (proc.stdout or "").split("\n"):
                line = line.strip()
                if any(kw in line.lower() for kw in ["total", "transfer", "elapsed"]):
                    logger.info(f"    {line}")
            return True
        else:
            stderr = (proc.stderr or proc.stdout or "")[:500]
            logger.error(f"  azcopy failed (rc={proc.returncode}): {stderr}")
            return False
    except FileNotFoundError:
        logger.warning("  azcopy not found — skipping OneLake artifact copy")
        return False
    except subprocess.TimeoutExpired:
        logger.error("  azcopy timed out (30 min)")
        return False
    except Exception as e:
        logger.error(f"  azcopy error: {e}")
        return False


def sync_ml_models(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync ML Models from primary to secondary.
    
    MLModel definitions contain references to MLExperiment IDs that must
    be remapped. If the model already exists in secondary, only the OneLake
    data is synced (no delete+recreate). New models are created with their
    remapped definition first, then data is copied.
    
    Args:
        primary_workspace_id: Primary workspace GUID
        secondary_workspace_id: Secondary workspace GUID
        logger: Logger instance
        dry_run: If True, don't execute
        
    Returns:
        Sync result
    """
    result = {
        "models_synced": [],
        "models_failed": [],
    }
    
    try:
        primary_models = common.get_items(
            primary_workspace_id,
            item_type="MLModel",
        )
        logger.info(f"Found {len(primary_models)} ML models in primary")
        
        secondary_models = common.get_items(
            secondary_workspace_id,
            item_type="MLModel",
        )
        secondary_by_name = {m["displayName"]: m for m in secondary_models}
        
        # Build combined mapping (includes MLExperiment ID mapping)
        mapping = common.build_combined_mapping()
        
        for primary_model in primary_models:
            model_name = primary_model["displayName"]
            model_id = primary_model["id"]
            
            logger.info(f"Processing ML model: {model_name}")
            
            try:
                secondary_item_id = None

                if model_name in secondary_by_name:
                    # Model already exists — no action needed
                    secondary_item_id = secondary_by_name[model_name]["id"]
                    logger.info(f"  Model exists in secondary ({secondary_item_id[:8]}…) — no action needed")
                else:
                    # Model doesn't exist — create as EMPTY placeholder (no definition).
                    # Fabric validates MLModel definitions against the MLExperiment's
                    # internal MLflow state and deletes mismatches within ~60s.
                    logger.info(f"  Model not in secondary — creating empty placeholder")

                    if dry_run:
                        logger.info(f"[DRY RUN] Would create ML model: {model_name}")
                        result["models_synced"].append(model_name)
                        continue

                    common.import_item(
                        secondary_workspace_id,
                        model_name,
                        "MLModel",
                        None,
                    )
                    logger.info(f"  ✓ Created ML model placeholder: {model_name}")

                    # Get the new secondary item ID
                    refreshed = common.get_items(
                        secondary_workspace_id, item_type="MLModel",
                    )
                    for m in refreshed:
                        if m["displayName"] == model_name:
                            secondary_item_id = m["id"]
                            break

                    # Update artifact_mapping.csv with the new secondary ID
                    if secondary_item_id:
                        _update_artifact_mapping(
                            model_name, "MLModel",
                            model_id, secondary_item_id, logger,
                        )

                if dry_run:
                    logger.info(f"[DRY RUN] Would sync ML model data: {model_name}")
                    result["models_synced"].append(model_name)
                    continue

                # Skip azcopy for MLModel — copying data with primary model-version
                # UUIDs causes Fabric's MLflow service to detect inconsistency and
                # delete the item. MLModel is replicated by definition only;
                # actual artifacts live in MLExperiment.
                logger.info(f"  MLModel replicated by definition only (skipping azcopy data copy)")

                logger.info(f"✓ Synced ML model: {model_name}")
                result["models_synced"].append(model_name)
            
            except Exception as e:
                logger.error(f"Failed to sync ML model {model_name}: {str(e)}")
                result["models_failed"].append({
                    "model": model_name,
                    "error": str(e),
                })
    
    except Exception as e:
        logger.error(f"Error syncing ML models: {str(e)}")
    
    return result


def sync_ml_experiments(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync ML Experiments from primary to secondary.
    
    Args:
        primary_workspace_id: Primary workspace GUID
        secondary_workspace_id: Secondary workspace GUID
        logger: Logger instance
        dry_run: If True, don't execute
        
    Returns:
        Sync result
    """
    result = {
        "experiments_synced": [],
        "experiments_failed": [],
    }
    
    try:
        primary_experiments = common.get_items(
            primary_workspace_id,
            item_type="MLExperiment",
        )
        logger.info(f"Found {len(primary_experiments)} ML experiments in primary")
        
        secondary_experiments = common.get_items(
            secondary_workspace_id,
            item_type="MLExperiment",
        )
        secondary_by_name = {e["displayName"]: e for e in secondary_experiments}
        
        mapping = common.build_combined_mapping()
        
        for primary_exp in primary_experiments:
            exp_name = primary_exp["displayName"]
            exp_id = primary_exp["id"]
            
            logger.info(f"Processing ML experiment: {exp_name}")
            
            try:
                # Export definition
                definition = common.export_item_definition(
                    primary_workspace_id,
                    exp_id,
                )
                
                # Remap references
                definition = common.remap_definition(definition, mapping)
                
                if dry_run:
                    logger.info(f"[DRY RUN] Would sync ML experiment: {exp_name}")
                    result["experiments_synced"].append(exp_name)
                else:
                    sec_exp_id = None
                    if exp_name in secondary_by_name:
                        sec_exp_id = secondary_by_name[exp_name]["id"]
                        common.update_item_definition(
                            secondary_workspace_id,
                            sec_exp_id,
                            definition,
                        )
                        logger.info(f"  ✓ Updated ML experiment definition: {exp_name}")
                    else:
                        common.import_item(
                            secondary_workspace_id,
                            exp_name,
                            "MLExperiment",
                            definition,
                        )
                        logger.info(f"  ✓ Created ML experiment: {exp_name}")
                        # Get new secondary ID
                        refreshed = common.get_items(
                            secondary_workspace_id, item_type="MLExperiment",
                        )
                        for m in refreshed:
                            if m["displayName"] == exp_name:
                                sec_exp_id = m["id"]
                                break

                    # Copy OneLake experiment artifacts (runs, metrics, model files)
                    if sec_exp_id:
                        ok = _azcopy_ml_item(
                            primary_workspace_id, exp_id,
                            secondary_workspace_id, sec_exp_id,
                            logger,
                        )
                        if ok:
                            logger.info(f"  ✓ Copied experiment artifacts: {exp_name}")
                        else:
                            logger.warning(f"  ⚠ Experiment artifacts copy failed for {exp_name}")

                    result["experiments_synced"].append(exp_name)
            
            except Exception as e:
                logger.error(f"Failed to sync ML experiment {exp_name}: {str(e)}")
                result["experiments_failed"].append({
                    "experiment": exp_name,
                    "error": str(e),
                })
    
    except Exception as e:
        logger.error(f"Error syncing ML experiments: {str(e)}")
    
    return result


def sync_data_agents(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync Data Agents (GraphQL API) from primary to secondary.
    
    Data Agent definitions contain datasource references with artifactId
    and workspaceId that must be remapped to secondary workspace items.
    
    Args:
        primary_workspace_id: Primary workspace GUID
        secondary_workspace_id: Secondary workspace GUID
        logger: Logger instance
        dry_run: If True, don't execute
        
    Returns:
        Sync result
    """
    result = {
        "agents_synced": [],
        "agents_failed": [],
    }
    
    try:
        primary_agents = common.get_items(
            primary_workspace_id,
            item_type="GraphQLApi",
        )
        logger.info(f"Found {len(primary_agents)} data agents in primary")
        
        secondary_agents = common.get_items(
            secondary_workspace_id,
            item_type="GraphQLApi",
        )
        secondary_by_name = {a["displayName"]: a for a in secondary_agents}
        
        # Build combined mapping (workspace IDs, SM IDs, LH IDs, etc.)
        mapping = common.build_combined_mapping()
        
        for primary_agent in primary_agents:
            agent_name = primary_agent["displayName"]
            agent_id = primary_agent["id"]
            
            logger.info(f"Processing data agent: {agent_name}")
            
            try:
                # Export definition
                definition = common.export_item_definition(
                    primary_workspace_id,
                    agent_id,
                )
                
                # Remap all references (artifactId, workspaceId in datasources)
                definition = common.remap_definition(definition, mapping)
                
                if dry_run:
                    logger.info(f"[DRY RUN] Would sync data agent: {agent_name}")
                    result["agents_synced"].append(agent_name)
                else:
                    if agent_name in secondary_by_name:
                        secondary_agent_id = secondary_by_name[agent_name]["id"]
                        common.update_item_definition(
                            secondary_workspace_id,
                            secondary_agent_id,
                            definition,
                        )
                        logger.info(f"✓ Updated data agent: {agent_name}")
                    else:
                        common.import_item(
                            secondary_workspace_id,
                            agent_name,
                            "GraphQLApi",
                            definition,
                        )
                        logger.info(f"✓ Created data agent: {agent_name}")
                    
                    result["agents_synced"].append(agent_name)
            
            except Exception as e:
                logger.error(f"Failed to sync data agent {agent_name}: {str(e)}")
                result["agents_failed"].append({
                    "agent": agent_name,
                    "error": str(e),
                })
    
    except Exception as e:
        logger.error(f"Error syncing data agents: {str(e)}")
    
    return result


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Sync Fabric ML models and related artifacts"
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
    
    args = parser.parse_args()
    
    logger = common.setup_logger("sync_ml_models_and_experiments")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    try:
        sync_summary = {
            "environments": {},
            "spark_jobs": {},
            "ml_models": {},
            "ml_experiments": {},
            "data_agents": {},
        }
        
        # Sync environments (delegated to sync_environments.py which includes publish)
        logger.info("\n=== SYNCING ENVIRONMENTS ===")
        env_result = _sync_environments_dedicated(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["environments"] = env_result
        
        # Sync Spark jobs
        logger.info("\n=== SYNCING SPARK JOB DEFINITIONS ===")
        job_result = sync_spark_jobs(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["spark_jobs"] = job_result
        
        # Sync ML experiments (before models, since models reference experiments)
        logger.info("\n=== SYNCING ML EXPERIMENTS ===")
        exp_result = sync_ml_experiments(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["ml_experiments"] = exp_result
        
        # Sync ML models
        logger.info("\n=== SYNCING ML MODELS ===")
        model_result = sync_ml_models(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["ml_models"] = model_result
        
        # Sync Data Agents
        logger.info("\n=== SYNCING DATA AGENTS ===")
        agent_result = sync_data_agents(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["data_agents"] = agent_result
        
        common.save_json(sync_summary, "data/ml_sync_report.json")
        
        print("\n" + "=" * 70)
        print("ML & AI ARTIFACTS SYNC SUMMARY")
        print("=" * 70)
        # env_result uses int counts (from sync_environments.py)
        print(f"Environments Synced:        {env_result.get('environments_synced', 0)}")
        print(f"Environments Failed:        {len(env_result.get('environments_failed', []))}")
        print(f"Spark Jobs Synced:          {len(job_result['jobs_synced'])}")
        print(f"Spark Jobs Failed:          {len(job_result['jobs_failed'])}")
        print(f"ML Experiments Synced:      {len(exp_result['experiments_synced'])}")
        print(f"ML Experiments Failed:      {len(exp_result['experiments_failed'])}")
        print(f"ML Models Synced:           {len(model_result['models_synced'])}")
        print(f"ML Models Failed:           {len(model_result['models_failed'])}")
        print(f"Data Agents Synced:         {len(agent_result['agents_synced'])}")
        print(f"Data Agents Failed:         {len(agent_result['agents_failed'])}")
        print("=" * 70 + "\n")
        
        logger.info("ML artifacts sync complete")
        return True
    
    except Exception as e:
        logger.error(f"Error in ML sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
