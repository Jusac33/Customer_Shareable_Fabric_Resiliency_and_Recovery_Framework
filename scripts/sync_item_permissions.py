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

Items other than semantic models have no public share-write API. They are
closed with the ACCESS-GROUP BOOTSTRAP (supported APIs only):

  --groups-plan   From the manual rows, plan one Entra security group per
                  (item, permission set) holding exactly the principals that
                  have direct access on primary. Writes data/item_access_groups.csv.
  --groups-apply  From the reviewed CSV: create/maintain the groups and add
                  members with Microsoft Graph, grant semantic models to the
                  group by API, and write data/item_access_portal_steps.csv with
                  the ONE-TIME portal share per remaining item. Add --dry-run
                  to only report. Members are never removed.

Once a secondary item is shared with its group, every later access change is a
group-membership change. --plan/--verify expand secondary group shares into
their members, so users covered through a group are reported as present.
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
GROUPS_CSV = "data/item_access_groups.csv"
PORTAL_STEPS_CSV = "data/item_access_portal_steps.csv"
FIELDS = ["action", "item_type", "item_name", "primary_item_id", "secondary_item_id",
          "principal", "principal_type", "display_name", "rights", "additional", "note"]
GROUP_FIELDS = ["kind", "group_nickname", "group_id", "group_display_name", "item_type", "item_name",
                "primary_item_id", "secondary_item_id", "rights", "additional", "share_mode",
                "member", "member_type"]
STEP_FIELDS = ["item_type", "item_name", "secondary_item_id", "group_display_name", "group_id",
               "rights", "additional", "verified", "note"]


def _plan(primary_ws: str, secondary_ws: str, use_admin_api: bool, logger,
          expand_groups: bool = True) -> Dict[str, Any]:
    resolver = ip.make_group_member_resolver(common.graph_api_call, logger) if expand_groups else None
    return ip.build_plan(
        common.api_call, common.powerbi_api_call, primary_ws, secondary_ws,
        common.get_items(primary_ws), common.get_items(secondary_ws),
        use_admin_api=use_admin_api, logger=logger, group_member_resolver=resolver,
    )


def write_csv(rows: List[Dict[str, Any]], path: str, fields: List[str] = FIELDS) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def specs_to_rows(specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One CSV row per (group, member) so reviewers can strike individual members."""
    rows = []
    for spec in specs:
        base = {k: spec.get(k, "") for k in GROUP_FIELDS if k not in ("member", "member_type")}
        if not spec["members"]:
            rows.append({**base, "member": "", "member_type": ""})
        for m in spec["members"]:
            rows.append({**base, "member": m["principal"], "member_type": m["principal_type"]})
    return rows


def rows_to_specs(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rebuild specs from the reviewed groups CSV; rejects malformed rows."""
    specs: Dict[tuple, Dict[str, Any]] = {}
    for i, r in enumerate(rows, start=2):
        kind = r.get("kind")
        if kind not in ("managed_group", "existing_group"):
            raise ValueError(f"groups CSV line {i}: unknown kind {kind!r}")
        if kind == "managed_group" and not r.get("group_nickname"):
            raise ValueError(f"groups CSV line {i}: managed_group needs group_nickname")
        if kind == "existing_group" and not r.get("group_id"):
            raise ValueError(f"groups CSV line {i}: existing_group needs group_id")
        if not r.get("secondary_item_id"):
            raise ValueError(f"groups CSV line {i}: blank secondary_item_id")
        key = (kind, r.get("group_nickname") or r.get("group_id"), r["secondary_item_id"])
        spec = specs.setdefault(key, {**{k: r.get(k, "") for k in GROUP_FIELDS
                                         if k not in ("member", "member_type")}, "members": []})
        if r.get("member"):
            spec["members"].append({"principal": r["member"], "principal_type": r.get("member_type") or "User"})
    return list(specs.values())


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
    mode.add_argument("--groups-plan", action="store_true",
                      help="Plan Entra access groups for items with no share-write API")
    mode.add_argument("--groups-apply", action="store_true",
                      help="Create/maintain access groups (Graph) from the reviewed groups CSV")
    parser.add_argument("--plan-file", default=PLAN_CSV)
    parser.add_argument("--groups-file", default=GROUPS_CSV)
    parser.add_argument("--dry-run", action="store_true", help="With --groups-apply: report only")
    parser.add_argument("--no-admin-api", action="store_true",
                        help="Skip the Fabric Admin API (semantic models only)")
    parser.add_argument("--no-group-expansion", action="store_true",
                        help="Do not credit members of groups shared on secondary items")
    args = parser.parse_args()
    logger = common.setup_logger("sync_item_permissions")

    if args.groups_apply:
        try:
            with open(args.groups_file, newline="", encoding="utf-8") as f:
                specs = rows_to_specs(list(csv.DictReader(f)))
        except (FileNotFoundError, ValueError) as e:
            logger.error(f"{e}. Run --groups-plan first and review the CSV.")
            return 1
        res = ip.apply_access_groups(common.graph_api_call, common.powerbi_api_call,
                                     args.secondary_workspace, specs, logger,
                                     dry_run=args.dry_run, fabric_call=common.api_call)
        write_csv(res["portal_steps"], PORTAL_STEPS_CSV, STEP_FIELDS)
        print("\n" + "=" * 70)
        print(f"ACCESS-GROUP BOOTSTRAP{' (dry run)' if args.dry_run else ''}")
        print("=" * 70)
        print(f"  Groups created:              {res['groups_created']}")
        print(f"  Groups to create:            {len(res['groups_to_create'])}")
        print(f"  Groups already present:      {res['groups_existing']}")
        print(f"  Members added:               {res['members_added']}")
        print(f"  Members to add:              {res['members_to_add']}")
        print(f"  Members already present:     {res['members_present']}")
        print(f"  Semantic-model group grants: {res['dataset_group_grants']}")
        print(f"  One-time portal shares:      {len(res['portal_steps'])}  -> {PORTAL_STEPS_CSV}")
        for x in res["extra_members"][:10]:
            print(f"  NOTE extra members (not removed) in {x['group']}: {len(x['object_ids'])}")
        for f in res["failures"][:10]:
            print(f"  FAILED {f['spec']} {f['member']}: {f['error']}")
        print("=" * 70)
        print("Share each listed secondary item once with its group (Manage permissions >")
        print("Add user > the group, with the listed permissions), then run --verify.\n")
        return 0 if not res["failures"] else 1

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

    plan = _plan(args.primary_workspace, args.secondary_workspace, not args.no_admin_api, logger,
                 expand_groups=not args.no_group_expansion)
    if args.groups_plan:
        gp = ip.plan_access_groups(plan["rows"])
        write_csv(specs_to_rows(gp["specs"]), args.groups_file, GROUP_FIELDS)
        print_summary(plan, "ITEM PERMISSION PLAN (read-only)")
        managed = [s for s in gp["specs"] if s["kind"] == "managed_group"]
        print(f"Access groups planned: {len(managed)} managed, "
              f"{len(gp['specs']) - len(managed)} existing group(s) to share, "
              f"{sum(len(s['members']) for s in managed)} membership(s), "
              f"{len(gp['unsupported'])} unsupported principal(s).")
        print(f"Written to {args.groups_file}. Review it, then run --groups-apply --dry-run.\n")
        return 0 if plan["complete"] else 1
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
