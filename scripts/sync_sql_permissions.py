"""
Granular SQL permission replication (T-SQL; supported) for Warehouses and
Lakehouse SQL analytics endpoints. See sql_permissions.py.

  --plan    Read-only. Diff roles, role membership and GRANT/DENY between
            matched primary/secondary databases; write data/sql_permissions_plan.csv.
  --apply   Execute the reviewed CSV exactly as written (skipped rows are
            never sent). Never revokes.
  --verify  Re-plan and report what is still open.
"""

import argparse
import csv
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common
import sql_permissions as sp

PLAN_CSV = "data/sql_permissions_plan.csv"
FIELDS = ["item_type", "item_name", "secondary_host", "database", "kind", "statement", "note"]


def build(primary_ws: str, secondary_ws: str, logger) -> Dict[str, Any]:
    return sp.build_sql_plan(
        common.api_call, common.sql_host_from, lambda: common.get_token(scope=common.SQL_API_SCOPE),
        primary_ws, secondary_ws, common.get_items(primary_ws), common.get_items(secondary_ws),
        logger=logger,
    )


def apply_rows(rows: List[Dict[str, Any]], logger) -> Dict[str, Any]:
    """Group statements per secondary database and run them in plan order."""
    total = {"applied": 0, "failed": 0, "failures": []}
    token = common.get_token(scope=common.SQL_API_SCOPE)
    by_db: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        by_db.setdefault((r["secondary_host"], r["database"]), []).append(r)
    for (host, db), stmts in by_db.items():
        try:
            conn = sp.connect(host, db, token)
        except Exception as e:
            n = sum(1 for s in stmts if s.get("kind") != "skipped")
            total["failed"] += n
            total["failures"].append({"statement": f"<connect {db}>", "error": str(e)[:300]})
            continue
        try:
            res = sp.apply_statements(lambda sql: conn.cursor().execute(sql), stmts, logger)
        finally:
            conn.close()
        total["applied"] += res["applied"]
        total["failed"] += res["failed"]
        total["failures"] += res["failures"]
    return total


def summary(plan: Dict[str, Any], title: str) -> None:
    counts: Dict[str, int] = {}
    for r in plan["rows"]:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    cov = plan["coverage"]
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)
    for k in ("create_role", "role_member", "grant", "skipped"):
        print(f"  {k:24} {counts.get(k, 0)}")
    print(f"  Databases scanned:       {cov['databases_scanned']}")
    print(f"  Unmatched in secondary:  {cov['unmatched_in_secondary']}")
    print(f"  Extra on secondary:      {cov['extra_on_secondary']}  (reported, never revoked)")
    for e in cov["errors"][:10]:
        print(f"  ERROR: {e}")
    print("=" * 70 + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="SQL permission replication (T-SQL)")
    parser.add_argument("--primary-workspace", default=common.PRIMARY_WORKSPACE_ID)
    parser.add_argument("--secondary-workspace", default=common.SECONDARY_WORKSPACE_ID)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--plan-file", default=PLAN_CSV)
    args = parser.parse_args()
    logger = common.setup_logger("sync_sql_permissions")

    if args.apply:
        if not os.path.exists(args.plan_file):
            logger.error(f"{args.plan_file} not found. Run --plan first and review it.")
            return 1
        with open(args.plan_file, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        bad = [i for i, r in enumerate(rows, start=2) if not (r.get("statement") and r.get("secondary_host"))]
        if bad:
            logger.error(f"{args.plan_file}: blank statement/host on line(s) {bad[:5]}")
            return 1
        res = apply_rows(rows, logger)
        print(f"\nApplied: {res['applied']}  Failed: {res['failed']}")
        for f in res["failures"][:10]:
            print(f"  - {f['statement']}: {f['error']}")
        print("Run --verify to confirm the result.\n")
        return 0 if res["failed"] == 0 else 1

    plan = build(args.primary_workspace, args.secondary_workspace, logger)
    if args.plan:
        os.makedirs(os.path.dirname(args.plan_file) or ".", exist_ok=True)
        with open(args.plan_file, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(plan["rows"])
        summary(plan, "SQL PERMISSION PLAN (read-only)")
        print(f"Plan written to {args.plan_file}. Review it, then run --apply.\n")
        return 0 if not plan["coverage"]["errors"] else 1
    summary(plan, "SQL PERMISSION VERIFICATION")
    open_rows = sum(1 for r in plan["rows"] if r["kind"] != "skipped")
    return 0 if open_rows == 0 and not plan["coverage"]["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
