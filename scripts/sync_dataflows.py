"""
Sync Dataflows

Purpose:
  BCDR for Dataflows Gen2 with connection reference remapping.

Artifact Types Covered:
  DataflowsGen2

RPO/RTO:
  RPO: Last dataflow refresh interval
  RTO: Minutes

Prerequisites:
  - Connection credentials must be pre-configured in secondary workspace
  - connection_mapping.csv with secondary connection names

Usage:
  python sync_dataflows.py
  python sync_dataflows.py --dry-run
"""

import argparse
import json
from typing import Dict, List, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common


def validate_secondary_connections(
    secondary_workspace_id: str,
    required_connections: List[str],
    logger,
) -> bool:
    """
    Validate that required connections exist in secondary workspace.
    
    Args:
        secondary_workspace_id: Secondary workspace GUID
        required_connections: List of required connection names
        logger: Logger instance
        
    Returns:
        True if all connections present, False otherwise
    """
    logger.info("Validating secondary workspace connections...")
    
    try:
        # Get connections from secondary workspace
        endpoint = f"/workspaces/{secondary_workspace_id}/connections"
        response = common.api_call("GET", endpoint)
        
        secondary_conns = {
            conn.get("displayName") for conn in response.get("value", [])
        }
        
        all_present = True
        for conn in required_connections:
            if conn in secondary_conns:
                logger.info(f"  ✓ Connection found: {conn}")
            else:
                logger.warning(f"  ✗ Connection missing: {conn}")
                all_present = False
        
        return all_present
    
    except Exception as e:
        logger.error(f"Error validating connections: {str(e)}")
        return False


def sync_dataflows(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync dataflows from primary to secondary.
    
    Args:
        primary_workspace_id: Primary workspace GUID
        secondary_workspace_id: Secondary workspace GUID
        logger: Logger instance
        dry_run: If True, don't execute
        
    Returns:
        Sync result
    """
    result = {
        "dataflows_synced": [],
        "dataflows_failed": [],
        "connections_remapped": 0,
    }
    
    try:
        # Get primary dataflows
        primary_dataflows = common.get_items(
            primary_workspace_id,
            item_type="DataflowsGen2",
        )
        logger.info(f"Found {len(primary_dataflows)} dataflows in primary")
        
        # Get secondary dataflows
        secondary_dataflows = common.get_items(
            secondary_workspace_id,
            item_type="DataflowsGen2",
        )
        secondary_by_name = {df["displayName"]: df for df in secondary_dataflows}
        
        # Build combined mapping for base64-aware remapping
        mapping = common.build_combined_mapping()
        
        for primary_df in primary_dataflows:
            df_name = primary_df["displayName"]
            df_id = primary_df["id"]
            
            logger.info(f"Processing dataflow: {df_name}")
            
            try:
                # Export definition
                definition = common.export_item_definition(
                    primary_workspace_id,
                    df_id,
                )
                
                # Remap all references (base64-aware)
                definition = common.remap_definition(definition, mapping)
                
                if dry_run:
                    logger.info(f"[DRY RUN] Would sync dataflow: {df_name}")
                    result["dataflows_synced"].append(df_name)
                else:
                    # Check if exists
                    if df_name in secondary_by_name:
                        secondary_df_id = secondary_by_name[df_name]["id"]
                        common.update_item_definition(
                            secondary_workspace_id,
                            secondary_df_id,
                            definition,
                        )
                        logger.info(f"✓ Updated dataflow: {df_name}")
                    else:
                        common.import_item(
                            secondary_workspace_id,
                            df_name,
                            "DataflowsGen2",
                            definition,
                        )
                        logger.info(f"✓ Created dataflow: {df_name}")
                    
                    result["dataflows_synced"].append(df_name)
            
            except Exception as e:
                logger.error(f"Failed to sync dataflow {df_name}: {str(e)}")
                result["dataflows_failed"].append({
                    "dataflow": df_name,
                    "error": str(e),
                })
    
    except Exception as e:
        logger.error(f"Error syncing dataflows: {str(e)}")
    
    return result


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Sync Fabric dataflows")
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
    
    logger = common.setup_logger("sync_dataflows")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    try:
        # Validate connections first
        if not args.dry_run:
            required_conns = list(common.load_connection_mapping().values())
            if required_conns:
                if not validate_secondary_connections(
                    args.secondary_workspace,
                    required_conns,
                    logger,
                ):
                    logger.warning("Some required connections missing in secondary!")
                    logger.info("Please pre-configure missing connections before syncing")
        
        # Sync dataflows
        result = sync_dataflows(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        
        common.save_json(result, "data/dataflow_sync_report.json")
        
        print("\n" + "=" * 70)
        print("DATAFLOW SYNC SUMMARY")
        print("=" * 70)
        print(f"Dataflows Synced:           {len(result['dataflows_synced'])}")
        print(f"Dataflows Failed:           {len(result['dataflows_failed'])}")
        print(f"Connections Remapped:       {result['connections_remapped']}")
        print("=" * 70 + "\n")
        
        logger.info("Dataflow sync complete")
        return True
    
    except Exception as e:
        logger.error(f"Error in dataflow sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
