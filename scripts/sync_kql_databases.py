"""
Sync KQL Databases

Purpose:
  BCDR for KQL Databases (Eventhouse) and KQL Querysets with schema
  and query definition sync. Delegates to rti/sync_rti.py for the
  actual sync logic to avoid duplication.

Artifact Types Covered:
  KQLDatabase, KQLQueryset

RPO/RTO:
  RPO: Depends on continuous export/ingestion lag
  RTO: Minutes

Prerequisites:
  - KQL Database REST API access
  - Continuous export configured for data replication

Usage:
  python sync_kql_databases.py
  python sync_kql_databases.py --dry-run
"""

import argparse
import json
from typing import Dict, List, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common

# Delegate to the canonical RTI sync module
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rti"))
from sync_rti import sync_all_rti, print_summary


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Sync Fabric KQL databases")
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
    
    logger = common.setup_logger("sync_kql_databases")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    try:
        # Delegate to rti/sync_rti.py for KQL types only
        results = sync_all_rti(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
            types=["KQLDatabase", "KQLQueryset"],
        )
        
        print_summary(results, logger)
        common.save_json(results, "data/kql_sync_report.json")
        
        logger.info("KQL artifacts sync complete")
        return True
    
    except Exception as e:
        logger.error(f"Error in KQL sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
