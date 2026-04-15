"""
Sync Workspaces Metadata

Purpose:
  Inventory all artifacts in primary workspace, compare to secondary,
  and produce a detailed sync plan for all artifact types.

Artifact Types Covered:
  All supported types: Lakehouse, Warehouse, Notebook, DataPipeline,
  SemanticModel, Report, DataflowsGen2, KQLDatabase, Eventstream, etc.

RPO/RTO:
  RPO: Last metadata sync
  RTO: Minutes (metadata only, no data sync)

Usage:
  python sync_workspaces_metadata.py
  python sync_workspaces_metadata.py --dry-run
"""

import json
import argparse
from datetime import datetime
from typing import Dict, List, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common


def generate_artifact_manifest(workspace_id: str) -> Dict[str, Any]:
    """
    Generate a manifest of all artifacts in a workspace.
    
    Args:
        workspace_id: Fabric workspace GUID
        
    Returns:
        Manifest dict with artifact inventory
    """
    logger = common.setup_logger("sync_workspaces_metadata")
    logger.info(f"Generating artifact manifest for workspace {workspace_id}...")
    
    artifacts_by_type = {}
    total_artifacts = 0
    
    try:
        for artifact_type in common.ARTIFACT_TYPES_TO_SYNC:
            try:
                items = common.get_items(workspace_id, item_type=artifact_type)
                artifacts_by_type[artifact_type] = [
                    {
                        "id": item.get("id"),
                        "displayName": item.get("displayName"),
                        "type": item.get("type"),
                        "description": item.get("description", ""),
                        "workspaceId": workspace_id,
                    }
                    for item in items
                ]
                total_artifacts += len(artifacts_by_type[artifact_type])
                logger.info(f"  {artifact_type}: {len(artifacts_by_type[artifact_type])} items")
            except Exception as e:
                logger.warning(f"Error fetching {artifact_type}: {str(e)}")
                artifacts_by_type[artifact_type] = []
    
    except Exception as e:
        logger.error(f"Error generating manifest: {str(e)}")
        raise
    
    return {
        "timestamp": datetime.now().isoformat(),
        "workspace_id": workspace_id,
        "total_artifacts": total_artifacts,
        "artifacts_by_type": artifacts_by_type,
    }


def compare_manifests(
    primary_manifest: Dict[str, Any],
    secondary_manifest: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Compare primary and secondary manifests to identify sync needs.
    
    Args:
        primary_manifest: Primary workspace manifest
        secondary_manifest: Secondary workspace manifest
        
    Returns:
        Comparison dict with categorized artifacts
    """
    logger = common.setup_logger("sync_workspaces_metadata")
    
    comparison = {
        "primary_total": primary_manifest["total_artifacts"],
        "secondary_total": secondary_manifest["total_artifacts"],
        "sync_plan": {
            "MISSING_IN_SECONDARY": [],
            "TYPE_MISMATCH": [],
            "IN_SYNC": [],
            "EXTRA_IN_SECONDARY": [],
        },
        "summary": {},
    }
    
    # Build secondary artifact lookup by name
    secondary_by_name = {}
    for artifact_type, artifacts in secondary_manifest["artifacts_by_type"].items():
        for artifact in artifacts:
            key = f"{artifact['displayName']}:{artifact_type}"
            secondary_by_name[key] = artifact
    
    # Compare primary artifacts
    for artifact_type, primary_artifacts in primary_manifest["artifacts_by_type"].items():
        for p_artifact in primary_artifacts:
            artifact_key = f"{p_artifact['displayName']}:{artifact_type}"
            
            if artifact_key not in secondary_by_name:
                comparison["sync_plan"]["MISSING_IN_SECONDARY"].append({
                    "id": p_artifact["id"],
                    "displayName": p_artifact["displayName"],
                    "type": artifact_type,
                    "description": p_artifact.get("description", ""),
                })
            else:
                s_artifact = secondary_by_name[artifact_key]
                if p_artifact.get("type") == s_artifact.get("type"):
                    comparison["sync_plan"]["IN_SYNC"].append({
                        "displayName": p_artifact["displayName"],
                        "type": artifact_type,
                        "primary_id": p_artifact["id"],
                        "secondary_id": s_artifact["id"],
                    })
                else:
                    comparison["sync_plan"]["TYPE_MISMATCH"].append({
                        "displayName": p_artifact["displayName"],
                        "primary_type": p_artifact.get("type"),
                        "secondary_type": s_artifact.get("type"),
                    })
    
    # Find extra artifacts in secondary
    primary_by_name = {}
    for artifact_type, artifacts in primary_manifest["artifacts_by_type"].items():
        for artifact in artifacts:
            key = f"{artifact['displayName']}:{artifact_type}"
            primary_by_name[key] = artifact
    
    for artifact_type, secondary_artifacts in secondary_manifest["artifacts_by_type"].items():
        for s_artifact in secondary_artifacts:
            artifact_key = f"{s_artifact['displayName']}:{artifact_type}"
            if artifact_key not in primary_by_name:
                comparison["sync_plan"]["EXTRA_IN_SECONDARY"].append({
                    "displayName": s_artifact["displayName"],
                    "type": artifact_type,
                    "description": s_artifact.get("description", ""),
                })
    
    # Generate summary
    comparison["summary"] = {
        "missing_in_secondary_count": len(comparison["sync_plan"]["MISSING_IN_SECONDARY"]),
        "type_mismatch_count": len(comparison["sync_plan"]["TYPE_MISMATCH"]),
        "in_sync_count": len(comparison["sync_plan"]["IN_SYNC"]),
        "extra_in_secondary_count": len(comparison["sync_plan"]["EXTRA_IN_SECONDARY"]),
    }
    
    return comparison


def print_sync_summary(comparison: Dict[str, Any], logger):
    """Print formatted sync summary"""
    summary = comparison["summary"]
    
    print("\n" + "=" * 70)
    print("WORKSPACE METADATA SYNC SUMMARY")
    print("=" * 70)
    print(f"Primary Artifacts:          {comparison['primary_total']}")
    print(f"Secondary Artifacts:        {comparison['secondary_total']}")
    print(f"\nSync Plan:")
    print(f"  ✓ In Sync:                {summary['in_sync_count']}")
    print(f"  + Missing in Secondary:   {summary['missing_in_secondary_count']}")
    print(f"  ⚠ Type Mismatches:        {summary['type_mismatch_count']}")
    print(f"  - Extra in Secondary:     {summary['extra_in_secondary_count']}")
    print("=" * 70 + "\n")
    
    if summary["type_mismatch_count"] > 0:
        print("⚠ TYPE MISMATCH WARNINGS:")
        for item in comparison["sync_plan"]["TYPE_MISMATCH"]:
            print(f"  - {item['displayName']}: "
                  f"{item['primary_type']} → {item['secondary_type']}")
    
    if summary["extra_in_secondary_count"] > 0:
        print("\n⚠ EXTRA ITEMS IN SECONDARY (consider deleting):")
        for item in comparison["sync_plan"]["EXTRA_IN_SECONDARY"][:5]:
            print(f"  - {item['displayName']} ({item['type']})")
        if summary["extra_in_secondary_count"] > 5:
            print(f"  ... and {summary['extra_in_secondary_count'] - 5} more")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Sync Fabric workspace metadata")
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
    
    logger = common.setup_logger("sync_workspaces_metadata")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    try:
        # Validate access
        logger.info("Validating workspace access...")
        if not common.validate_workspace_access(args.primary_workspace):
            logger.error("Cannot access primary workspace")
            return False
        
        if not common.validate_workspace_access(args.secondary_workspace):
            logger.error("Cannot access secondary workspace")
            return False
        
        # Generate manifests
        logger.info("Generating primary workspace manifest...")
        primary_manifest = generate_artifact_manifest(args.primary_workspace)
        common.save_json(primary_manifest, "data/primary_artifact_manifest.json")
        
        logger.info("Generating secondary workspace manifest...")
        secondary_manifest = generate_artifact_manifest(args.secondary_workspace)
        common.save_json(secondary_manifest, "data/secondary_artifact_manifest.json")
        
        # Compare
        logger.info("Comparing manifests...")
        comparison = compare_manifests(primary_manifest, secondary_manifest)
        common.save_json(comparison, "data/sync_plan.json")
        
        # Print summary
        print_sync_summary(comparison, logger)
        
        logger.info("Metadata sync plan complete")
        return True
    
    except Exception as e:
        logger.error(f"Error in metadata sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
