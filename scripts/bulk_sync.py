"""
Bulk Definition Sync (Fabric Bulk Item Definition APIs — beta)

Uses the new Bulk Export / Import Item Definition APIs to replicate
all workspace item definitions in **two** API calls instead of 2×N.

Falls back gracefully to per-item getDefinition + createItem/updateDefinition
if the bulk endpoints are unavailable (404/4xx) or return errors.

Artifact Types Covered:
  All types that support getDefinition / updateDefinition
  (Notebook, DataPipeline, Report, SemanticModel, Lakehouse, Eventhouse,
   KQLDatabase, KQLQueryset, Eventstream, SparkJobDefinition, Environment,
   KQLDashboard, GraphQLApi, Dataflow, CopyJob)

Usage:
  python scripts/bulk_sync.py
  python scripts/bulk_sync.py --dry-run
  python scripts/bulk_sync.py --type Notebook
"""

import argparse
import base64
import json
import sys
import os
from typing import Dict, List, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common


# Types where getDefinition is supported (from Item Management Overview docs)
DEFINITION_SUPPORTED_TYPES = {
    "Notebook", "DataPipeline", "Report", "SemanticModel",
    "SparkJobDefinition", "Environment", "Eventhouse",
    "KQLDatabase", "KQLQueryset", "KQLDashboard", "Eventstream",
    "GraphQLApi", "Dataflow", "CopyJob",
}

# Types we should NOT attempt to replicate via definition (empty-only or no definition)
SKIP_DEFINITION_TYPES = {"MLModel", "MLExperiment", "Lakehouse", "Warehouse", "SQLEndpoint"}


def _rewrite_parts(parts: List[Dict], replacements: Dict[str, str]) -> List[Dict]:
    """Rewrite base64-encoded definition parts, stripping .platform parts."""
    rewritten = []
    for part in parts:
        path = part.get("path", "")
        if path == ".platform" or path.endswith("/.platform"):
            continue
        part_copy = dict(part)
        payload_b64 = part_copy.get("payload", "")
        if not payload_b64:
            rewritten.append(part_copy)
            continue
        try:
            text = base64.b64decode(payload_b64).decode("utf-8")
            for old_val, new_val in replacements.items():
                text = text.replace(old_val, new_val)
            part_copy["payload"] = base64.b64encode(text.encode("utf-8")).decode("ascii")
        except Exception:
            pass
        rewritten.append(part_copy)
    return rewritten


def _build_connection_map(
    primary_ws: str,
    secondary_ws: str,
    p_items: List[Dict],
    s_items: List[Dict],
) -> Dict[str, str]:
    """Build primary → secondary ID replacement map."""
    replacements = {primary_ws: secondary_ws}
    s_by_name = {i.get("displayName", ""): i for i in s_items}
    for p in p_items:
        name = p.get("displayName", "")
        pid = p.get("id", "")
        s = s_by_name.get(name)
        if s and pid:
            sid = s.get("id", "")
            if sid and pid != sid:
                replacements[pid] = sid
    return replacements


def bulk_sync(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
    item_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Sync workspace item definitions using bulk APIs with per-item fallback.

    Strategy:
      1. Try bulk export from primary  → if 404/error, fall back to per-item
      2. Remap IDs in all definitions
      3. Try bulk import to secondary  → if 404/error, fall back to per-item
    """
    result = {
        "mode": "unknown",  # "bulk" or "per-item"
        "exported": 0,
        "imported": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": [],
        "unchanged": 0,
    }

    p_id = primary_workspace_id
    s_id = secondary_workspace_id

    # Get workspace item lists
    p_items = common.get_items(p_id)
    s_items = common.get_items(s_id)

    # Filter by type if requested
    target_types = set(item_types) if item_types else None

    p_typed = [
        i for i in p_items
        if i.get("type") in DEFINITION_SUPPORTED_TYPES
        and (target_types is None or i.get("type") in target_types)
    ]

    if not p_typed:
        logger.info("No items with definition support to sync")
        result["mode"] = "none"
        return result

    s_by_name = {}
    for s in s_items:
        key = (s.get("displayName", ""), s.get("type", ""))
        s_by_name[key] = s

    # Build connection map
    conn_map = _build_connection_map(p_id, s_id, p_items, s_items)
    logger.info(f"Connection map: {len(conn_map)} replacement(s)")
    logger.info(f"Items to sync: {len(p_typed)} (types: {set(i['type'] for i in p_typed)})")

    # ── Step 1: Try bulk export ──────────────────────────────────────────
    bulk_export_ok = False
    exported_defs = {}  # keyed by item id

    try:
        logger.info("Attempting bulk export (beta API)...")
        item_ids = [i["id"] for i in p_typed]
        resp = common.bulk_export_definitions(p_id, item_ids=item_ids)

        items_list = resp.get("itemDefinitions", [])
        if isinstance(resp, list):
            items_list = resp

        for item_def in items_list:
            iid = item_def.get("id") or item_def.get("itemId")
            if iid:
                exported_defs[iid] = item_def
        result["exported"] = len(exported_defs)
        logger.info(f"Bulk export returned {len(exported_defs)} definition(s)")
        bulk_export_ok = True
    except Exception as e:
        err_str = str(e)
        if "404" in err_str or "NotFound" in err_str or "not supported" in err_str.lower():
            logger.info(f"Bulk export API not available (beta): {err_str[:120]}")
        else:
            logger.warning(f"Bulk export failed, falling back to per-item: {err_str[:200]}")

    # Fallback: per-item getDefinition
    if not bulk_export_ok:
        logger.info("Using per-item getDefinition fallback...")
        for item in p_typed:
            iid = item["id"]
            name = item["displayName"]
            try:
                resp = common.api_call(
                    "POST",
                    f"/workspaces/{p_id}/items/{iid}/getDefinition",
                    timeout=120,
                )
                definition = resp.get("definition", {})
                if definition and definition.get("parts"):
                    exported_defs[iid] = {
                        "id": iid,
                        "displayName": name,
                        "type": item["type"],
                        "definition": definition,
                    }
            except Exception as ex:
                logger.debug(f"Could not export definition for {name}: {ex}")
        result["exported"] = len(exported_defs)
        logger.info(f"Per-item export: got {len(exported_defs)} definition(s)")

    if not exported_defs:
        logger.info("No definitions to import")
        result["mode"] = "none"
        return result

    # ── Step 2: Remap IDs and compare with secondary ────────────────────
    to_create = []  # new items (not in secondary)
    to_update = []  # existing items (definition changed)

    for iid, item_def in exported_defs.items():
        name = item_def.get("displayName", "")
        itype = item_def.get("type", "")
        definition = item_def.get("definition", {})
        parts = definition.get("parts", [])

        if not parts:
            result["skipped"] += 1
            continue

        # Remap
        remapped_parts = _rewrite_parts(parts, conn_map)
        remapped_def = {"parts": remapped_parts}
        if definition.get("format"):
            remapped_def["format"] = definition["format"]

        s_item = s_by_name.get((name, itype))

        if s_item is None:
            # New item — needs creation
            to_create.append({
                "displayName": name,
                "type": itype,
                "definition": remapped_def,
            })
        else:
            # Existing — check if definition changed
            s_item_id = s_item["id"]
            changed = True  # assume changed unless we can prove identical

            # Try to get secondary definition for delta comparison
            try:
                s_resp = common.api_call(
                    "POST",
                    f"/workspaces/{s_id}/items/{s_item_id}/getDefinition",
                    timeout=120,
                )
                s_parts = s_resp.get("definition", {}).get("parts", [])
                # Compare by (path, payload) sets
                p_set = {(p.get("path", ""), p.get("payload", "")) for p in remapped_parts}
                s_set = {(p.get("path", ""), p.get("payload", "")) for p in s_parts}
                if p_set == s_set:
                    changed = False
            except Exception:
                pass  # can't compare → treat as changed

            if changed:
                to_update.append({
                    "id": s_item_id,
                    "displayName": name,
                    "type": itype,
                    "definition": remapped_def,
                })
            else:
                result["unchanged"] += 1
                logger.info(f"  = {itype}: {name} — unchanged")

    logger.info(f"Delta: {len(to_create)} to create, {len(to_update)} to update, "
                f"{result['unchanged']} unchanged, {result['skipped']} skipped")

    if dry_run:
        for c in to_create:
            logger.info(f"  [DRY RUN] Would create {c['type']}: {c['displayName']}")
        for u in to_update:
            logger.info(f"  [DRY RUN] Would update {u['type']}: {u['displayName']}")
        result["mode"] = "dry-run"
        result["created"] = len(to_create)
        result["updated"] = len(to_update)
        return result

    # ── Step 3: Try bulk import, else per-item fallback ─────────────────
    all_to_import = to_create + to_update
    if not all_to_import:
        result["mode"] = "bulk" if bulk_export_ok else "per-item"
        return result

    bulk_import_ok = False
    try:
        logger.info(f"Attempting bulk import of {len(all_to_import)} item(s) (beta API)...")
        common.bulk_import_definitions(s_id, all_to_import)
        result["created"] = len(to_create)
        result["updated"] = len(to_update)
        result["imported"] = len(all_to_import)
        bulk_import_ok = True
        logger.info(f"Bulk import succeeded: {len(all_to_import)} item(s)")
    except Exception as e:
        err_str = str(e)
        if "404" in err_str or "NotFound" in err_str or "not supported" in err_str.lower():
            logger.info(f"Bulk import API not available (beta): {err_str[:120]}")
        else:
            logger.warning(f"Bulk import failed, falling back to per-item: {err_str[:200]}")

    # Fallback: per-item create/update
    if not bulk_import_ok:
        logger.info("Using per-item create/update fallback...")

        # Create new items
        for item_def in to_create:
            name = item_def["displayName"]
            try:
                common.api_call(
                    "POST",
                    f"/workspaces/{s_id}/items",
                    payload=item_def,
                    timeout=120,
                )
                result["created"] += 1
                logger.info(f"  ✓ Created {item_def['type']}: {name}")
            except Exception as e:
                logger.error(f"  ✗ Failed to create {name}: {e}")
                result["failed"].append({"name": name, "action": "create", "error": str(e)[:200]})

        # Update existing items
        for item_def in to_update:
            name = item_def["displayName"]
            s_item_id = item_def["id"]
            try:
                common.api_call(
                    "POST",
                    f"/workspaces/{s_id}/items/{s_item_id}/updateDefinition",
                    payload={"definition": item_def["definition"]},
                    timeout=120,
                )
                result["updated"] += 1
                logger.info(f"  ✓ Updated {item_def['type']}: {name}")
            except Exception as e:
                logger.error(f"  ✗ Failed to update {name}: {e}")
                result["failed"].append({"name": name, "action": "update", "error": str(e)[:200]})

        result["imported"] = result["created"] + result["updated"]

    result["mode"] = "bulk" if (bulk_export_ok and bulk_import_ok) else "per-item"
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Bulk sync item definitions from primary to secondary workspace"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    parser.add_argument("--type", action="append", dest="types",
                        help="Sync only these item types (can be repeated)")
    parser.add_argument("--primary-workspace", default=common.PRIMARY_WORKSPACE_ID)
    parser.add_argument("--secondary-workspace", default=common.SECONDARY_WORKSPACE_ID)

    args = parser.parse_args()
    logger = common.setup_logger("bulk_sync")
    common.DRY_RUN = args.dry_run

    if args.dry_run:
        logger.info("DRY RUN MODE — no changes will be made")

    result = bulk_sync(
        args.primary_workspace,
        args.secondary_workspace,
        logger,
        dry_run=args.dry_run,
        item_types=args.types,
    )

    print("\n" + "=" * 70)
    print("BULK SYNC SUMMARY")
    print("=" * 70)
    print(f"  Mode:        {result['mode']}")
    print(f"  Exported:    {result['exported']}")
    print(f"  Created:     {result['created']}")
    print(f"  Updated:     {result['updated']}")
    print(f"  Unchanged:   {result['unchanged']}")
    print(f"  Skipped:     {result['skipped']}")
    print(f"  Failed:      {len(result['failed'])}")
    print("=" * 70)

    return len(result["failed"]) == 0


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
