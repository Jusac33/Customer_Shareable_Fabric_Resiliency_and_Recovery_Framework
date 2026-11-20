"""
Item-level permission replication (supported APIs only).

  --plan    Read-only. Detect direct item shares on primary, diff against
            secondary, write data/item_permissions_plan.csv, print coverage.
  --apply   Apply the reviewed CSV exactly as written. Only semantic-model rows
            are sent (Power BI Datasets users API, GA). Never re-plans.
  --verify  Re-run detection and report what is still open.

Coverage depends on the caller:
  * Fabric administrator, or service principal with the tenant setting
    "Service principals can access read-only admin APIs": every item type
    is scanned via the Fabric Admin item-access API.
  * Anyone else: semantic models only. Other item types are reported as
    NOT SCANNED, never as "no shares".

Items other than semantic models have no public write API; they are listed as
manual_portal_share rows so they can be re-shared in the portal and verified.
"""

import argparse
import csv
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import item_permissions as ip

PLAN_CSV = "data/item_permissions_plan.csv"
FIELDS = ["action", "item_type", "item_name", "primary_item_id", "secondary_item_id",
          "principal", "principal_type", "rights", "note"]


def _plan(primary_ws: str, secondary_ws: str, use_admin_api: bool, logger) -> Dict[str, Any]:
    return ip.build_plan(
        common.api_call, common.powerbi_api_call, primary_ws, secondary_ws,
        common.get_items(primary_ws), common.get_items(secondary_ws),
        use_admin_api=use_admin_api, logger=logger,
    )


def write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def read_csv(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} not found. Run --plan first and review it.")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for i, r in enumerate(rows, start=2):
        if r.get("action") in (ip.ACTION_GRANT, ip.ACTION_UPGRADE):
            missing = [k for k in ("secondary_item_id", "principal", "principal_type", "rights") if not r.get(k)]
            if missing:
                raise ValueError(f"{path} line {i}: blank {missing}")
    return rows


def print_summary(plan: Dict[str, Any], title: str) -> None:
    cov = plan["coverage"]
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)
    for action in (ip.ACTION_GRANT, ip.ACTION_UPGRADE, ip.ACTION_MANUAL, ip.ACTION_PRESENT):
        print(f"  {action:24} {plan['counts'].get(action, 0)}")
    print(f"  Scanned (admin API):     {cov['scanned_admin_api']}")
    print(f"  Scanned (dataset API):   {cov['scanned_dataset_api']}")
    print(f"  NOT scanned:             {cov['not_scanned']}  {cov.get('not_scanned_by_type') or ''}")
    print(f"  Unmatched in secondary:  {cov['unmatched_in_secondary']}")
    if cov.get("admin_api_denied"):
        print("\n  The Fabric Admin item-access API was denied (403). Non-semantic-model")
        print("  items were NOT scanned. Run as a Fabric administrator, or a service")
        print("  principal with 'Service principals can access read-only admin APIs'.")
    for e in cov["errors"][:10]:
        print(f"  ERROR: {e}")
    print("=" * 70 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Item permission replication (supported APIs only)")
    parser.add_argument("--primary-workspace", default=common.PRIMARY_WORKSPACE_ID)
    parser.add_argument("--secondary-workspace", default=common.SECONDARY_WORKSPACE_ID)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--plan-file", default=PLAN_CSV)
    parser.add_argument("--no-admin-api", action="store_true",
                        help="Skip the Fabric Admin API (semantic models only)")
    args = parser.parse_args()
    logger = common.setup_logger("sync_item_permissions")

    if args.apply:
        try:
            rows = read_csv(args.plan_file)
        except (FileNotFoundError, ValueError) as e:
            logger.error(str(e))
            return 1
        res = ip.apply_rows(common.powerbi_api_call, args.secondary_workspace, rows, logger)
        print(f"\nApplied: {res['applied']}  Failed: {res['failed']}  Manual (not sent): {res['skipped_manual']}")
        for f in res["failures"][:10]:
            print(f"  - {f['item']} / {f['principal']}: {f['error']}")
        print("Run --verify to confirm the result.\n")
        return 0 if res["failed"] == 0 else 1

    plan = _plan(args.primary_workspace, args.secondary_workspace, not args.no_admin_api, logger)
    if args.plan:
        write_csv(plan["rows"], args.plan_file)
        print_summary(plan, "ITEM PERMISSION PLAN (read-only)")
        print(f"Plan written to {args.plan_file}. Review it, then run --apply.\n")
    else:
        print_summary(plan, "ITEM PERMISSION VERIFICATION")
    open_rows = sum(plan["counts"].get(a, 0) for a in (ip.ACTION_GRANT, ip.ACTION_UPGRADE, ip.ACTION_MANUAL))
    return 0 if plan["complete"] and (args.plan or open_rows == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
