"""
Sync Eventstreams

Fabric-specific (real-time/streaming layer)

Purpose:
  Resiliency & Recovery for Eventstreams with destination artifact remapping.
  Delegates to rti/sync_rti.py for the actual sync logic to avoid duplication.

Artifact Types Covered:
  Eventstream

RPO/RTO:
  RPO: Real-time (depends on source lag)
  RTO: Seconds (zero-downtime failover with shortcuts/secondaries)

Prerequisites:
  - Eventstream source connections (Event Hub, IoT Hub, Kafka) must be
    pre-configured in secondary workspace
  - artifact_mapping.csv with destination artifact mappings
  - connection_mapping.csv with source connection mappings

Usage:
  python sync_eventstreams.py
  python sync_eventstreams.py --dry-run
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
    parser = argparse.ArgumentParser(description="Sync Fabric eventstreams")
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
    
    logger = common.setup_logger("sync_eventstreams")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    try:
        # Delegate to rti/sync_rti.py for Eventstream type only
        results = sync_all_rti(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
            types=["Eventstream"],
        )
        
        print_summary(results, logger)
        common.save_json(results, "data/eventstream_sync_report.json")
        
        print("\n⚠ NOTE: Eventstream source connections require manual re-authentication")
        print("  due to OAuth/credential limitations. Ensure source Event Hubs or")
        print("  Kafka clusters are available in the secondary region and update")
        print("  connection credentials in the Fabric UI after this sync.\n")
        
        logger.info("Eventstream sync complete")
        return True
    
    except Exception as e:
        logger.error(f"Error in eventstream sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

