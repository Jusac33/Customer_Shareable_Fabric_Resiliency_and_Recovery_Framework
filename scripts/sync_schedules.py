"""
Job-schedule replication (Fabric Job Scheduler API). See bcdr_schedules.py.

Every primary schedule is replicated to its secondary counterpart PAUSED and a
manifest of primary schedules is written to data/schedule_manifest.json, so a
failover can enable the right schedules even when the primary is unreachable.
Run after item sync (the secondary items must exist).

  python scripts/sync_schedules.py            # replicate + write manifest
  python scripts/sync_schedules.py --dry-run  # report only (manifest still written)
"""

import argparse
import os
import sys
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import bcdr_schedules as bs


def sync_schedules(primary_ws: str, secondary_ws: str, logger, dry_run: bool = False,
                   manifest_path: str = bs.MANIFEST_PATH) -> Dict[str, Any]:
    client = bs.ScheduleClient(common.api_call)
    p_items = [i for i in common.get_items(primary_ws) if i.get("type") in bs.SCHEDULABLE_TYPES]
    s_items = [i for i in common.get_items(secondary_ws) if i.get("type") in bs.SCHEDULABLE_TYPES]
    p = bs.collect(client, primary_ws, p_items)
    s = bs.collect(client, secondary_ws, s_items)
    if not p["errors"]:
        bs.write_manifest(p["entries"], primary_ws, manifest_path)
    else:
        logger.warning("Primary schedules not fully readable; keeping the previous manifest")
    plan = bs.plan_replication(p["entries"], s["entries"], s_items)
    applied = bs.apply_replication(client, secondary_ws, plan["rows"], logger, dry_run)
    enabled_on_secondary = [e for e in s["entries"] if e["enabled"]]
    counts: Dict[str, int] = {}
    for r in plan["rows"]:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {
        "primary_schedules": len(p["entries"]),
        "primary_enabled": sum(1 for e in p["entries"] if e["enabled"]),
        "counts": counts,
        "created_paused": applied["created"],
        "failed": applied["failed"],
        "failures": applied["failures"],
        "extra_on_secondary": len(plan["extra_on_secondary"]),
        "enabled_on_secondary": [f"{e['item_name']} ({e['job_type']})" for e in enabled_on_secondary],
        "unsupported_types": {**p["unsupported_types"], **s["unsupported_types"]},
        "errors": p["errors"] + s["errors"],
        "manifest_written": not p["errors"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replicate job schedules (paused) to the secondary")
    parser.add_argument("--primary-workspace", default=common.PRIMARY_WORKSPACE_ID)
    parser.add_argument("--secondary-workspace", default=common.SECONDARY_WORKSPACE_ID)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logger = common.setup_logger("sync_schedules")
    res = sync_schedules(args.primary_workspace, args.secondary_workspace, logger, args.dry_run)
    print("\n" + "=" * 70)
    print(f"SCHEDULE REPLICATION{' (dry run)' if args.dry_run else ''}")
    print("=" * 70)
    print(f"  Primary schedules:          {res['primary_schedules']} ({res['primary_enabled']} enabled)")
    print(f"  Already on secondary:       {res['counts'].get(bs.ACTION_PRESENT, 0)}")
    print(f"  Replicated paused:          {res['created_paused']}")
    print(f"  Failed:                     {res['failed']}")
    print(f"  Item missing in secondary:  {res['counts'].get(bs.ACTION_MISSING_ITEM, 0)}  (run item sync first)")
    print(f"  Extra on secondary:         {res['extra_on_secondary']}  (reported, never deleted)")
    if res["enabled_on_secondary"]:
        print(f"  WARNING enabled on standby secondary: {', '.join(res['enabled_on_secondary'][:5])}")
    if res["unsupported_types"]:
        print(f"  Job type not resolved for:  {res['unsupported_types']}")
    for e in res["errors"][:5]:
        print(f"  ERROR: {e}")
    print(f"  Manifest:                   {bs.MANIFEST_PATH if res['manifest_written'] else 'NOT updated'}")
    print("=" * 70 + "\n")
    return 0 if not res["failed"] and not res["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
