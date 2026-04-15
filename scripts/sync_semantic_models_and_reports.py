"""
Sync Semantic Models and Reports

Purpose:
  Resiliency & Recovery for BI artifacts (Semantic Models and Reports) with automatic
  connection string and dataset rebinding to secondary workspace.

Artifact Types Covered:
  SemanticModel, Report

RPO/RTO:
  RPO: Last definition export
  RTO: Minutes

Prerequisites:
  - Semantic models must have connections pointing to secondary data sources
  - artifact_mapping.csv must be populated before running

Usage:
  python sync_semantic_models_and_reports.py
  python sync_semantic_models_and_reports.py --dry-run
"""

import argparse
import json
from typing import Dict, List, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common


def sync_semantic_models(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync semantic models from primary to secondary.
    
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
        "model_id_mapping": {},
    }
    
    try:
        # Get primary models
        primary_models = common.get_items(
            primary_workspace_id,
            item_type="SemanticModel",
        )
        logger.info(f"Found {len(primary_models)} semantic models in primary")
        
        # Get secondary models
        secondary_models = common.get_items(
            secondary_workspace_id,
            item_type="SemanticModel",
        )
        secondary_by_name = {m["displayName"]: m for m in secondary_models}
        
        # Build combined mapping for base64-aware remapping
        mapping = common.build_combined_mapping()
        
        for primary_model in primary_models:
            model_name = primary_model["displayName"]
            model_id = primary_model["id"]
            
            logger.info(f"Processing semantic model: {model_name}")
            
            try:
                # Export definition
                definition = common.export_item_definition(
                    primary_workspace_id,
                    model_id,
                )
                
                # Remap references (base64-aware: handles expressions.tmdl
                # OneLake URLs with workspace/lakehouse GUIDs)
                definition = common.remap_definition(definition, mapping)
                
                if dry_run:
                    logger.info(f"[DRY RUN] Would sync model: {model_name}")
                    result["models_synced"].append(model_name)
                    result["model_id_mapping"][model_id] = f"secondary_{model_id}"
                else:
                    # Check if exists
                    if model_name in secondary_by_name:
                        secondary_model_id = secondary_by_name[model_name]["id"]
                        common.update_item_definition(
                            secondary_workspace_id,
                            secondary_model_id,
                            definition,
                        )
                        logger.info(f"✓ Updated model: {model_name}")
                        result["model_id_mapping"][model_id] = secondary_model_id
                    else:
                        # Create new
                        response = common.import_item(
                            secondary_workspace_id,
                            model_name,
                            "SemanticModel",
                            definition,
                        )
                        secondary_model_id = response.get("id", "unknown")
                        logger.info(f"✓ Created model: {model_name}")
                        result["model_id_mapping"][model_id] = secondary_model_id
                    
                    result["models_synced"].append(model_name)
            
            except Exception as e:
                logger.error(f"Failed to sync model {model_name}: {str(e)}")
                result["models_failed"].append({
                    "model": model_name,
                    "error": str(e),
                })
    
    except Exception as e:
        logger.error(f"Error syncing semantic models: {str(e)}")
    
    return result


def sync_reports(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    model_mapping: Dict[str, str],
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync reports from primary to secondary with dataset rebinding.
    
    Args:
        primary_workspace_id: Primary workspace GUID
        secondary_workspace_id: Secondary workspace GUID
        model_mapping: Mapping of primary model IDs to secondary
        logger: Logger instance
        dry_run: If True, don't execute
        
    Returns:
        Sync result
    """
    result = {
        "reports_synced": [],
        "reports_failed": [],
        "reports_rebound": 0,
    }
    
    try:
        # Get primary reports
        primary_reports = common.get_items(
            primary_workspace_id,
            item_type="Report",
        )
        logger.info(f"Found {len(primary_reports)} reports in primary")
        
        # Get secondary reports
        secondary_reports = common.get_items(
            secondary_workspace_id,
            item_type="Report",
        )
        secondary_by_name = {r["displayName"]: r for r in secondary_reports}
        
        for primary_report in primary_reports:
            report_name = primary_report["displayName"]
            report_id = primary_report["id"]
            
            logger.info(f"Processing report: {report_name}")
            
            try:
                # Export definition
                definition = common.export_item_definition(
                    primary_workspace_id,
                    report_id,
                )
                
                # Build combined mapping: model_mapping + all reference/artifact mappings
                # This ensures definition.pbir connectionString, semanticmodelid,
                # and workspace references are all remapped
                mapping = common.build_combined_mapping()
                mapping.update(model_mapping)
                
                # Remap all references (base64-aware)
                definition = common.remap_definition(definition, mapping)
                
                if dry_run:
                    logger.info(f"[DRY RUN] Would sync report: {report_name}")
                    result["reports_synced"].append(report_name)
                else:
                    # Check if exists
                    if report_name in secondary_by_name:
                        secondary_report_id = secondary_by_name[report_name]["id"]
                        common.update_item_definition(
                            secondary_workspace_id,
                            secondary_report_id,
                            definition,
                        )
                        logger.info(f"✓ Updated report: {report_name}")
                    else:
                        common.import_item(
                            secondary_workspace_id,
                            report_name,
                            "Report",
                            definition,
                        )
                        logger.info(f"✓ Created report: {report_name}")
                    
                    result["reports_synced"].append(report_name)
            
            except Exception as e:
                logger.error(f"Failed to sync report {report_name}: {str(e)}")
                result["reports_failed"].append({
                    "report": report_name,
                    "error": str(e),
                })
    
    except Exception as e:
        logger.error(f"Error syncing reports: {str(e)}")
    
    return result


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Sync Fabric semantic models and reports"
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
    
    logger = common.setup_logger("sync_semantic_models_and_reports")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    try:
        sync_summary = {
            "semantic_models": {},
            "reports": {},
        }
        
        # Sync semantic models first
        logger.info("\n=== SYNCING SEMANTIC MODELS ===")
        model_result = sync_semantic_models(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["semantic_models"] = model_result
        
        # Sync reports with model rebinding
        logger.info("\n=== SYNCING REPORTS ===")
        report_result = sync_reports(
            args.primary_workspace,
            args.secondary_workspace,
            model_result["model_id_mapping"],
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["reports"] = report_result
        
        common.save_json(sync_summary, "data/bi_sync_report.json")
        
        print("\n" + "=" * 70)
        print("BI ARTIFACTS SYNC SUMMARY")
        print("=" * 70)
        print(f"Semantic Models Synced:     {len(model_result['models_synced'])}")
        print(f"Semantic Models Failed:     {len(model_result['models_failed'])}")
        print(f"Reports Synced:             {len(report_result['reports_synced'])}")
        print(f"Reports Failed:             {len(report_result['reports_failed'])}")
        print(f"Reports Rebound:            {report_result['reports_rebound']}")
        print("=" * 70 + "\n")
        
        logger.info("BI artifacts sync complete")
        return True
    
    except Exception as e:
        logger.error(f"Error in BI sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

