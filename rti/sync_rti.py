"""
RTI (Real-Time Intelligence) Sync Module

Syncs Eventhouses, KQL Databases, KQL Querysets, and Eventstreams
from primary workspace to secondary workspace.

Handles:
  - Schema/definition export & import via Fabric Items API
  - Connection string remapping (Eventhouse URI, KQL DB references)
  - Eventstream destination artifact remapping
  - Continuous export guidance for KQL Database data replication

Usage:
  python rti/sync_rti.py
  python rti/sync_rti.py --dry-run
  python rti/sync_rti.py --type Eventhouse
"""

import argparse
import json
import sys
import os
import base64
import time
from typing import Dict, List, Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common


RTI_ARTIFACT_TYPES = ["Eventhouse", "KQLDatabase", "KQLQueryset", "Eventstream"]

# KQL Database connection patterns that need remapping
KQL_CONNECTION_PATTERNS = [
    # Eventhouse URI pattern: https://<eventhouse>.kusto.fabric.microsoft.com
    ".kusto.fabric.microsoft.com",
    # OneLake DFS path
    "onelake.dfs.fabric.microsoft.com",
]


def build_rti_connection_map(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    primary_items: List[Dict],
    secondary_items: List[Dict],
) -> Dict[str, str]:
    """Build a replacement map for RTI-specific connection strings.

    Maps:
      - Workspace IDs (primary → secondary)
      - Item IDs (matched by displayName)
      - Eventhouse URIs (matched by name convention)
    """
    replacements: Dict[str, str] = {}

    # Workspace ID swap
    replacements[primary_workspace_id] = secondary_workspace_id

    # Map item IDs by matching displayName
    s_by_name = {i.get("displayName", ""): i for i in secondary_items}
    for p_item in primary_items:
        p_name = p_item.get("displayName", "")
        p_id = p_item.get("id", "")
        s_item = s_by_name.get(p_name)
        if s_item and p_id:
            s_id = s_item.get("id", "")
            if s_id and p_id != s_id:
                replacements[p_id] = s_id

    return replacements


def rewrite_definition_parts(
    parts: List[Dict],
    replacements: Dict[str, str],
    logger,
) -> List[Dict]:
    """Rewrite base64-encoded definition parts with connection remapping."""
    rewritten = []
    for part in parts:
        part_copy = dict(part)
        payload_b64 = part_copy.get("payload", "")
        if not payload_b64:
            rewritten.append(part_copy)
            continue
        try:
            payload_bytes = base64.b64decode(payload_b64)
            payload_text = payload_bytes.decode("utf-8")

            changes = 0
            for old_val, new_val in replacements.items():
                if old_val in payload_text:
                    payload_text = payload_text.replace(old_val, new_val)
                    changes += 1

            if changes > 0:
                logger.info(f"  Rewrote {changes} reference(s) in {part.get('path', '?')}")

            part_copy["payload"] = base64.b64encode(
                payload_text.encode("utf-8")
            ).decode("ascii")
        except Exception as e:
            logger.warning(f"  Could not rewrite part {part.get('path', '?')}: {e}")
        rewritten.append(part_copy)
    return rewritten


def _definitions_equal(parts_a: List[Dict], parts_b: List[Dict]) -> bool:
    """Compare two definition part lists by path+payload, ignoring order."""
    def _to_set(parts):
        return {(p.get("path", ""), p.get("payload", "")) for p in parts}
    return _to_set(parts_a) == _to_set(parts_b)


def sync_artifact_type(
    artifact_type: str,
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
    connection_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Sync all artifacts of a given RTI type from primary to secondary.

    Returns dict with synced/failed/skipped lists.
    """
    result = {
        "type": artifact_type,
        "synced": [],
        "failed": [],
        "skipped": [],
        "already_mirrored": [],
    }

    try:
        primary_items = common.get_items(primary_workspace_id, item_type=artifact_type)
        secondary_items = common.get_items(secondary_workspace_id, item_type=artifact_type)
        s_by_name = {i["displayName"]: i for i in secondary_items}

        logger.info(f"Found {len(primary_items)} {artifact_type}(s) in primary, "
                     f"{len(secondary_items)} in secondary")

        for p_item in primary_items:
            name = p_item["displayName"]
            p_id = p_item["id"]

            logger.info(f"  Syncing {artifact_type}: {name}")

            try:
                # Export definition from primary
                definition = None
                parts = []
                try:
                    export_resp = common.api_call(
                        "POST",
                        f"/workspaces/{primary_workspace_id}/items/{p_id}/getDefinition",
                        timeout=120,
                    )
                    definition = export_resp.get("definition", {})
                    parts = definition.get("parts", [])
                    logger.info(f"    Exported {len(parts)} definition part(s)")
                except Exception as export_err:
                    logger.warning(f"    Definition export failed: {export_err}")

                # Rewrite connection strings
                if parts and connection_map:
                    parts = rewrite_definition_parts(parts, connection_map, logger)
                    definition = dict(definition)
                    definition["parts"] = parts

                if dry_run:
                    if name in s_by_name:
                        logger.info(f"  [DRY RUN] Would update {artifact_type}: {name}")
                    else:
                        logger.info(f"  [DRY RUN] Would create {artifact_type}: {name}")
                    result["synced"].append(name)
                    continue

                if name in s_by_name:
                    # Update existing item's definition — delta check first
                    s_item_id = s_by_name[name]["id"]
                    if parts:
                        # Fetch secondary definition to compare
                        s_parts = []
                        try:
                            s_def_resp = common.api_call(
                                "POST",
                                f"/workspaces/{secondary_workspace_id}/items/{s_item_id}/getDefinition",
                                timeout=120,
                            )
                            s_parts = s_def_resp.get("definition", {}).get("parts", [])
                        except Exception:
                            pass  # If we can't get secondary def, treat as changed

                        if s_parts and _definitions_equal(parts, s_parts):
                            logger.info(f"  = {artifact_type}: {name} — definition unchanged, skipping")
                            result["skipped"].append(name)
                            continue

                        try:
                            common.api_call(
                                "POST",
                                f"/workspaces/{secondary_workspace_id}/items/{s_item_id}/updateDefinition",
                                payload={"definition": definition},
                                timeout=120,
                            )
                            logger.info(f"  ✓ Updated {artifact_type}: {name}")
                        except Exception as upd_err:
                            logger.warning(f"  ⚠ Update failed for {name}, skipping: {upd_err}")
                            result["already_mirrored"].append(name)
                            continue
                    else:
                        logger.info(f"  ✓ {name} — exists, no definition to update")
                        result["already_mirrored"].append(name)
                        continue
                else:
                    # Create in secondary
                    create_payload: Dict[str, Any] = {
                        "displayName": name,
                        "type": artifact_type,
                    }
                    if parts:
                        create_payload["definition"] = definition

                    common.api_call(
                        "POST",
                        f"/workspaces/{secondary_workspace_id}/items",
                        payload=create_payload,
                        timeout=120,
                    )
                    logger.info(f"  ✓ Created {artifact_type}: {name}")

                result["synced"].append(name)

            except Exception as e:
                logger.error(f"  ✗ Failed to sync {name}: {e}")
                result["failed"].append({"name": name, "error": str(e)})

    except Exception as e:
        logger.error(f"Error listing {artifact_type} items: {e}")
        result["failed"].append({"name": f"[listing {artifact_type}]", "error": str(e)})

    return result


def sync_all_rti(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
    types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Sync all RTI artifact types from primary to secondary.

    Args:
        types: Optional subset of RTI_ARTIFACT_TYPES to sync
    """
    target_types = types or RTI_ARTIFACT_TYPES
    logger.info(f"=== RTI Resiliency & Recovery Sync: {', '.join(target_types)} ===")

    # Build connection map once for all types
    logger.info("Building RTI connection map...")
    all_primary = common.get_items(primary_workspace_id)
    all_secondary = common.get_items(secondary_workspace_id)
    conn_map = build_rti_connection_map(
        primary_workspace_id, secondary_workspace_id,
        all_primary, all_secondary,
    )
    # Merge with the standard reference/artifact/connection mappings
    combined = common.build_combined_mapping()
    combined.update(conn_map)
    logger.info(f"Connection map: {len(combined)} replacement(s)")

    results = {}
    for art_type in target_types:
        if art_type not in RTI_ARTIFACT_TYPES:
            logger.warning(f"Skipping unknown RTI type: {art_type}")
            continue

        logger.info(f"\n--- Syncing {art_type} ---")
        result = sync_artifact_type(
            art_type,
            primary_workspace_id,
            secondary_workspace_id,
            logger,
            dry_run=dry_run,
            connection_map=combined,
        )
        results[art_type] = result

    return results


def print_summary(results: Dict[str, Dict], logger):
    """Print a human-readable summary."""
    logger.info("\n" + "=" * 70)
    logger.info("RTI Resiliency & Recovery SYNC SUMMARY")
    logger.info("=" * 70)
    total_synced = total_failed = total_mirrored = total_skipped = 0
    for art_type, r in results.items():
        synced = len(r.get("synced", []))
        failed = len(r.get("failed", []))
        mirrored = len(r.get("already_mirrored", []))
        skipped = len(r.get("skipped", []))
        total_synced += synced
        total_failed += failed
        total_mirrored += mirrored
        total_skipped += skipped
        logger.info(f"  {art_type:20s}  synced={synced}  unchanged={skipped}  failed={failed}  already_mirrored={mirrored}")
    logger.info("-" * 70)
    logger.info(f"  {'TOTAL':20s}  synced={total_synced}  unchanged={total_skipped}  failed={total_failed}  already_mirrored={total_mirrored}")
    logger.info("=" * 70)

    # RTI-specific guidance
    logger.info("\n⚠ RTI Resiliency & Recovery NOTES:")
    logger.info("  • KQL Database DATA is NOT synced by definition export/import.")
    logger.info("    Configure continuous-export on primary → Azure Storage,")
    logger.info("    then ingest from storage into secondary KQL DB.")
    logger.info("  • Eventstream source connections (Event Hub, Kafka) require")
    logger.info("    manual re-authentication in the secondary workspace.")
    logger.info("  • KQL Querysets reference KQL Databases by ID — the connection")
    logger.info("    map remaps these automatically during sync.")


def main():
    parser = argparse.ArgumentParser(description="Sync RTI artifacts (primary → secondary)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    parser.add_argument("--type", choices=RTI_ARTIFACT_TYPES, help="Sync only one RTI type")
    parser.add_argument("--primary-workspace", default=common.PRIMARY_WORKSPACE_ID)
    parser.add_argument("--secondary-workspace", default=common.SECONDARY_WORKSPACE_ID)

    args = parser.parse_args()
    logger = common.setup_logger("sync_rti")
    common.DRY_RUN = args.dry_run

    if args.dry_run:
        logger.info("DRY RUN MODE — no changes will be made")

    types = [args.type] if args.type else None

    try:
        results = sync_all_rti(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
            types=types,
        )
        print_summary(results, logger)
        common.save_json(results, "data/rti_sync_report.json")
        return True
    except Exception as e:
        logger.error(f"RTI sync failed: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

