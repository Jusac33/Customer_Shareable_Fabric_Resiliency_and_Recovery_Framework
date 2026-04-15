"""
Sync Notebooks and Pipelines

Purpose:
  Resiliency & Recovery for code artifacts (Notebooks, DataPipelines) with automatic
  reference remapping for workspace IDs and connection names.

Artifact Types Covered:
  Notebook, DataPipeline, SparkJobDefinition

RPO/RTO:
  RPO: Last definition export
  RTO: Minutes

Prerequisites:
  - artifact_mapping.csv for artifact ID mapping
  - connection_mapping.csv for connection name mapping
  - reference_mapping.csv for OneLake path and ID remapping

Usage:
  python sync_notebooks_and_pipelines.py
  python sync_notebooks_and_pipelines.py --dry-run
"""

import argparse
import json
import re
from typing import Dict, List, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common


def remap_notebook_content(
    content: str,
    artifact_mapping: Dict[str, str],
    connection_mapping: Dict[str, str],
    reference_mapping: Dict[str, str],
    logger,
) -> str:
    """
    Remap notebook content for secondary workspace.
    
    Args:
        content: Notebook content (code)
        artifact_mapping: Artifact ID mapping
        connection_mapping: Connection name mapping
        reference_mapping: Reference (path/ID) mapping
        logger: Logger instance
        
    Returns:
        Remapped content
    """
    remapped = content
    
    # Apply connection mapping
    for primary_conn, secondary_conn in connection_mapping.items():
        remapped = remapped.replace(primary_conn, secondary_conn)
    
    # Apply artifact ID mapping
    for primary_id, secondary_id in artifact_mapping.items():
        remapped = remapped.replace(primary_id, secondary_id)
    
    # Apply reference mapping (OneLake URLs, etc.)
    for primary_ref, secondary_ref in reference_mapping.items():
        remapped = remapped.replace(primary_ref, secondary_ref)
    
    return remapped


def sync_notebooks(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync notebooks from primary to secondary.
    
    Args:
        primary_workspace_id: Primary workspace GUID
        secondary_workspace_id: Secondary workspace GUID
        logger: Logger instance
        dry_run: If True, don't execute
        
    Returns:
        Sync result
    """
    result = {
        "notebooks_synced": [],
        "notebooks_skipped": [],
        "notebooks_failed": [],
    }
    
    try:
        # Get primary notebooks
        primary_notebooks = common.get_items(
            primary_workspace_id,
            item_type="Notebook",
        )
        logger.info(f"Found {len(primary_notebooks)} notebooks in primary")
        
        # Get secondary notebooks for comparison
        secondary_notebooks = common.get_items(
            secondary_workspace_id,
            item_type="Notebook",
        )
        secondary_by_name = {nb["displayName"]: nb for nb in secondary_notebooks}
        
        # Build combined mapping for base64-aware remapping
        mapping = common.build_combined_mapping()
        
        for primary_nb in primary_notebooks:
            nb_name = primary_nb["displayName"]
            nb_id = primary_nb["id"]
            
            logger.info(f"Processing notebook: {nb_name}")
            
            try:
                # Export definition
                definition = common.export_item_definition(
                    primary_workspace_id,
                    nb_id,
                )
                
                # Remap all references (base64-aware: handles metadata block
                # with default_lakehouse, known_lakehouses, workspace IDs, etc.)
                definition = common.remap_definition(definition, mapping)
                
                if dry_run:
                    logger.info(f"[DRY RUN] Would sync notebook: {nb_name}")
                    result["notebooks_synced"].append(nb_name)
                else:
                    # Check if already exists
                    if nb_name in secondary_by_name:
                        # Update existing
                        secondary_nb_id = secondary_by_name[nb_name]["id"]
                        common.update_item_definition(
                            secondary_workspace_id,
                            secondary_nb_id,
                            definition,
                        )
                        logger.info(f"✓ Updated notebook: {nb_name}")
                    else:
                        # Create new
                        common.import_item(
                            secondary_workspace_id,
                            nb_name,
                            "Notebook",
                            definition,
                        )
                        logger.info(f"✓ Created notebook: {nb_name}")
                    
                    result["notebooks_synced"].append(nb_name)
            
            except Exception as e:
                logger.error(f"Failed to sync notebook {nb_name}: {str(e)}")
                result["notebooks_failed"].append({
                    "notebook": nb_name,
                    "error": str(e),
                })
    
    except Exception as e:
        logger.error(f"Error syncing notebooks: {str(e)}")
    
    return result


def remap_pipeline_definition(
    definition: Dict,
    artifact_mapping: Dict[str, str],
    connection_mapping: Dict[str, str],
    reference_mapping: Dict[str, str],
    logger,
) -> Dict:
    """
    Remap pipeline definition for secondary workspace.
    
    Args:
        definition: Pipeline definition dict
        artifact_mapping: Artifact ID mapping
        connection_mapping: Connection name mapping
        reference_mapping: Reference mapping
        logger: Logger instance
        
    Returns:
        Remapped definition
    """
    remapped = json.loads(json.dumps(definition))  # Deep copy
    
    # Convert to JSON string for text replacement
    def_str = json.dumps(definition)
    
    # Apply mappings
    for primary_id, secondary_id in artifact_mapping.items():
        def_str = def_str.replace(primary_id, secondary_id)
    
    for primary_conn, secondary_conn in connection_mapping.items():
        def_str = def_str.replace(primary_conn, secondary_conn)
    
    for primary_ref, secondary_ref in reference_mapping.items():
        def_str = def_str.replace(primary_ref, secondary_ref)
    
    remapped = json.loads(def_str)
    return remapped


def sync_pipelines(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync data pipelines from primary to secondary.
    
    Args:
        primary_workspace_id: Primary workspace GUID
        secondary_workspace_id: Secondary workspace GUID
        logger: Logger instance
        dry_run: If True, don't execute
        
    Returns:
        Sync result
    """
    result = {
        "pipelines_synced": [],
        "pipelines_skipped": [],
        "pipelines_failed": [],
        "pipelines_with_remapped_references": 0,
    }
    
    try:
        # Get primary pipelines
        primary_pipelines = common.get_items(
            primary_workspace_id,
            item_type="DataPipeline",
        )
        logger.info(f"Found {len(primary_pipelines)} pipelines in primary")
        
        # Get secondary pipelines
        secondary_pipelines = common.get_items(
            secondary_workspace_id,
            item_type="DataPipeline",
        )
        secondary_by_name = {p["displayName"]: p for p in secondary_pipelines}
        
        # Build combined mapping for base64-aware remapping
        mapping = common.build_combined_mapping()
        
        for primary_pipeline in primary_pipelines:
            pipeline_name = primary_pipeline["displayName"]
            pipeline_id = primary_pipeline["id"]
            
            logger.info(f"Processing pipeline: {pipeline_name}")
            
            try:
                # Export definition
                definition = common.export_item_definition(
                    primary_workspace_id,
                    pipeline_id,
                )
                
                # Remap definition (base64-aware)
                remapped_def = common.remap_definition(definition, mapping)
                
                if remapped_def != definition:
                    result["pipelines_with_remapped_references"] += 1
                
                if dry_run:
                    logger.info(f"[DRY RUN] Would sync pipeline: {pipeline_name}")
                    result["pipelines_synced"].append(pipeline_name)
                else:
                    # Check if exists
                    if pipeline_name in secondary_by_name:
                        secondary_pipeline_id = secondary_by_name[pipeline_name]["id"]
                        common.update_item_definition(
                            secondary_workspace_id,
                            secondary_pipeline_id,
                            remapped_def,
                        )
                        logger.info(f"✓ Updated pipeline: {pipeline_name}")
                    else:
                        common.import_item(
                            secondary_workspace_id,
                            pipeline_name,
                            "DataPipeline",
                            remapped_def,
                        )
                        logger.info(f"✓ Created pipeline: {pipeline_name}")
                    
                    result["pipelines_synced"].append(pipeline_name)
            
            except Exception as e:
                logger.error(f"Failed to sync pipeline {pipeline_name}: {str(e)}")
                result["pipelines_failed"].append({
                    "pipeline": pipeline_name,
                    "error": str(e),
                })
    
    except Exception as e:
        logger.error(f"Error syncing pipelines: {str(e)}")
    
    return result


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Sync Fabric notebooks and pipelines"
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
    
    logger = common.setup_logger("sync_notebooks_and_pipelines")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    try:
        sync_summary = {
            "notebooks": {},
            "pipelines": {},
        }
        
        # Sync notebooks
        logger.info("\n=== SYNCING NOTEBOOKS ===")
        notebook_result = sync_notebooks(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["notebooks"] = notebook_result
        
        # Sync pipelines
        logger.info("\n=== SYNCING PIPELINES ===")
        pipeline_result = sync_pipelines(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["pipelines"] = pipeline_result
        
        common.save_json(sync_summary, "data/code_sync_report.json")
        
        print("\n" + "=" * 70)
        print("CODE ARTIFACTS SYNC SUMMARY")
        print("=" * 70)
        print(f"Notebooks Synced:           {len(notebook_result['notebooks_synced'])}")
        print(f"Notebooks Failed:           {len(notebook_result['notebooks_failed'])}")
        print(f"Pipelines Synced:           {len(pipeline_result['pipelines_synced'])}")
        print(f"Pipelines Failed:           {len(pipeline_result['pipelines_failed'])}")
        print(f"Pipelines Remapped:         {pipeline_result['pipelines_with_remapped_references']}")
        print("=" * 70 + "\n")
        
        logger.info("Code artifacts sync complete")
        return True
    
    except Exception as e:
        logger.error(f"Error in code sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

