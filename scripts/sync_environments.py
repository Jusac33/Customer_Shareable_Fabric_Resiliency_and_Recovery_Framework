"""
Sync Environments

Purpose:
  Resiliency & Recovery for Fabric Environments with Spark compute settings,
  environment variables, and library replication.

Artifact Types Covered:
  Environment

RPO/RTO:
  RPO: Last environment sync
  RTO: Minutes (publish may take several minutes for library installation)

Prerequisites:
  - Service Principal must have workspace contributor or admin permissions
  - instance_pool_id may need manual remapping if custom Spark pools differ between regions

Usage:
  python sync_environments.py
  python sync_environments.py --dry-run
"""

import argparse
import base64
import json
import time
from typing import Dict, List, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common


def get_environments(workspace_id: str, logger) -> List[Dict[str, Any]]:
    """List all environments in a workspace."""
    try:
        items = common.get_items(workspace_id, item_type="Environment")
        logger.info(f"Found {len(items)} environments")
        return items
    except Exception as e:
        logger.error(f"Error listing environments: {str(e)}")
        return []


def get_environment_definition(workspace_id: str, env_id: str, logger) -> Dict[str, Any]:
    """Export the full definition of an environment."""
    endpoint = f"/workspaces/{workspace_id}/environments/{env_id}/getDefinition"
    resp = common.api_call("POST", endpoint)
    defn = resp.get("definition", {})
    parts = defn.get("parts", [])
    logger.info(f"Exported definition with {len(parts)} parts")
    for p in parts:
        logger.debug(f"  Part: {p.get('path')}")
    return defn


def update_environment_definition(workspace_id: str, env_id: str, definition: Dict, logger):
    """Update an existing environment's definition (stages changes, does not publish)."""
    endpoint = f"/workspaces/{workspace_id}/environments/{env_id}/updateDefinition"
    payload = {"definition": definition}
    common.api_call("POST", endpoint, payload)
    logger.info(f"Updated environment definition for {env_id}")


def publish_environment(workspace_id: str, env_id: str, logger, timeout: int = 300):
    """Publish an environment and wait for completion."""
    endpoint = f"/workspaces/{workspace_id}/environments/{env_id}/staging/publish?beta=false"
    try:
        resp = common.api_call("POST", endpoint)
        logger.info(f"Publish triggered for {env_id}")
    except Exception as e:
        # 202 Accepted is expected for LRO
        if "202" in str(e) or "accepted" in str(e).lower():
            logger.info(f"Publish accepted (LRO) for {env_id}")
        else:
            raise

    # Poll for publish completion
    start = time.time()
    while time.time() - start < timeout:
        try:
            env_resp = common.api_call("GET", f"/workspaces/{workspace_id}/environments/{env_id}")
            publish_info = env_resp.get("properties", {}).get("publishDetails", {})
            state = publish_info.get("state", "")
            if state == "Success":
                logger.info(f"✓ Environment {env_id} published successfully")
                return True
            elif state in ("Failed", "Cancelled"):
                logger.error(f"Publish {state} for {env_id}: {publish_info}")
                return False
            elif state in ("Running", "Waiting"):
                logger.debug(f"Publish in progress ({state})...")
            # No publish info means it completed instantly or not started
            elif not state:
                logger.info(f"✓ Environment {env_id} publish complete (no pending state)")
                return True
        except Exception as e:
            logger.debug(f"Poll error: {e}")
        time.sleep(10)

    logger.warning(f"Publish timeout after {timeout}s for {env_id}")
    return False


def _strip_platform(definition: Dict) -> Dict:
    """Remove .platform parts from definition (they contain workspace-specific metadata)."""
    import copy
    result = copy.deepcopy(definition)
    parts = result.get("parts", [])
    result["parts"] = [p for p in parts if p.get("path") != ".platform"
                       and not p.get("path", "").endswith("/.platform")]
    return result


def _decode_part(part: Dict) -> str:
    """Decode a base64 definition part to string."""
    payload = part.get("payload", "")
    if payload and part.get("payloadType") == "InlineBase64":
        return base64.b64decode(payload).decode("utf-8")
    return payload


def _compare_definitions(primary_def: Dict, secondary_def: Dict, logger) -> bool:
    """Compare two environment definitions, return True if they match."""
    p_parts = {p["path"]: p.get("payload", "") for p in primary_def.get("parts", [])
               if p.get("path") != ".platform"}
    s_parts = {p["path"]: p.get("payload", "") for p in secondary_def.get("parts", [])
               if p.get("path") != ".platform"}

    if set(p_parts.keys()) != set(s_parts.keys()):
        logger.info(f"  Part mismatch: primary={sorted(p_parts.keys())} secondary={sorted(s_parts.keys())}")
        return False

    for path in p_parts:
        if p_parts[path] != s_parts.get(path, ""):
            logger.info(f"  Content differs: {path}")
            return False

    return True


def sync_environments(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Sync Fabric Environments from primary to secondary workspace.

    For each environment in primary:
    1. Export its definition (Spark settings, libraries, env vars)
    2. Create or update in secondary (strip .platform)
    3. Publish in secondary to activate changes
    """
    result = {
        "environments_synced": 0,
        "environments_created": 0,
        "environments_updated": 0,
        "environments_skipped": 0,
        "environments_failed": [],
        "publish_results": [],
    }

    try:
        p_envs = get_environments(primary_workspace_id, logger)
        s_envs = get_environments(secondary_workspace_id, logger)
        s_env_by_name = {e["displayName"]: e for e in s_envs}

        logger.info(f"Primary: {len(p_envs)} environments, Secondary: {len(s_envs)} environments")

        for p_env in p_envs:
            name = p_env["displayName"]
            p_env_id = p_env["id"]
            logger.info(f"\nProcessing environment: {name}")

            try:
                # Step 1: Export definition from primary
                p_defn = get_environment_definition(primary_workspace_id, p_env_id, logger)

                if not p_defn.get("parts"):
                    logger.warning(f"Skipping {name}: empty definition")
                    result["environments_skipped"] += 1
                    continue

                # Log what's inside
                for part in p_defn.get("parts", []):
                    path = part.get("path", "")
                    if path in ("Setting/Sparkcompute.yml", "Libraries/PublicLibraries/environment.yml"):
                        decoded = _decode_part(part)
                        logger.info(f"  {path}:\n{decoded}")

                # Strip .platform for secondary
                clean_defn = _strip_platform(p_defn)

                s_env = s_env_by_name.get(name)

                if dry_run:
                    if s_env:
                        logger.info(f"[DRY RUN] Would update environment: {name}")
                    else:
                        logger.info(f"[DRY RUN] Would create environment: {name}")
                    logger.info(f"[DRY RUN] Would publish environment: {name}")
                    result["environments_synced"] += 1
                    continue

                # Step 2: Create or update in secondary
                if s_env:
                    s_env_id = s_env["id"]

                    # Check if definitions match
                    try:
                        s_defn = get_environment_definition(secondary_workspace_id, s_env_id, logger)
                        if _compare_definitions(p_defn, s_defn, logger):
                            logger.info(f"✓ {name}: already in sync, skipping")
                            result["environments_skipped"] += 1
                            continue
                    except Exception:
                        pass  # If we can't get secondary def, update anyway

                    logger.info(f"Updating environment: {name}")
                    update_environment_definition(secondary_workspace_id, s_env_id, clean_defn, logger)
                    result["environments_updated"] += 1
                else:
                    # Create new environment with definition
                    logger.info(f"Creating environment: {name}")
                    resp = common.import_item(
                        secondary_workspace_id,
                        name,
                        "Environment",
                        clean_defn,
                    )
                    s_env_id = resp.get("id", "unknown")
                    result["environments_created"] += 1

                # Step 3: Publish to activate the changes
                logger.info(f"Publishing environment: {name}")
                pub_ok = publish_environment(secondary_workspace_id, s_env_id, logger)
                result["publish_results"].append({
                    "name": name,
                    "published": pub_ok,
                })

                result["environments_synced"] += 1
                logger.info(f"✓ Synced environment: {name}")

            except Exception as e:
                logger.error(f"Failed to sync environment {name}: {str(e)}")
                result["environments_failed"].append({
                    "name": name,
                    "error": str(e)[:300],
                })

    except Exception as e:
        logger.error(f"Error in environment sync: {str(e)}")

    return result


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Sync Fabric Environments")
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

    logger = common.setup_logger("sync_environments")
    common.DRY_RUN = args.dry_run

    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")

    try:
        result = sync_environments(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )

        common.save_json(result, "data/environment_sync_report.json")

        print("\n" + "=" * 70)
        print("ENVIRONMENT SYNC SUMMARY")
        print("=" * 70)
        print(f"Environments Synced:    {result['environments_synced']}")
        print(f"  Created:              {result['environments_created']}")
        print(f"  Updated:              {result['environments_updated']}")
        print(f"Environments Skipped:   {result['environments_skipped']}")
        print(f"Environments Failed:    {len(result['environments_failed'])}")
        print(f"Publish Results:")
        for pr in result["publish_results"]:
            status = "✓" if pr["published"] else "✗"
            print(f"  {status} {pr['name']}")
        if result["environments_failed"]:
            print(f"\nFailed:")
            for f in result["environments_failed"]:
                print(f"  ✗ {f['name']}: {f['error'][:100]}")
        print("=" * 70 + "\n")

        logger.info("Environment sync complete")
        return True

    except Exception as e:
        logger.error(f"Error in environment sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

