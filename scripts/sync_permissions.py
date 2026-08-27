"""
Sync Permissions

Purpose:
  Resiliency & Recovery sync for workspace-level and item-level permissions with
  role assignments, OneLake data access controls, and connection role
  assignments for the DR executing service principal.

Artifact Types Covered:
  All (permissions apply to all artifact types)

RPO/RTO:
  RPO: Last permission sync
  RTO: Minutes

Prerequisites:
  - Service Principal must have workspace admin permissions
  - artifact_mapping.csv with primary → secondary artifact IDs
  - SERVICE_PRINCIPAL_OBJECT_ID must be set for connection role grants
    (Entra object ID of the service principal, not the client/app ID)

Usage:
  python sync_permissions.py
  python sync_permissions.py --dry-run
"""

import argparse
import json
from typing import Dict, List, Any

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common


# Fabric connections are tenant-scoped, so connection GUIDs embedded in copied
# Data Pipeline / Dataflow definitions still resolve in the secondary workspace.
# The identity that executes them in DR, however, needs an explicit role on each
# connection or the run fails at runtime.
CONNECTION_ROLE = "User"  # Owner | User | UserWithReshare

# Most item types do not implement the connections endpoint at all and answer
# 404 / "unsupported item type" instead of an empty list.  That is expected and
# must not be reported as a failure — a DR summary that cries wolf is worse than
# no summary.
_NO_CONNECTIONS_STATUS_CODES = (404,)
_NO_CONNECTIONS_MARKERS = (
    "not supported",
    "notsupported",
    "unsupported",
)


def _is_no_connections_error(error: Exception) -> bool:
    """
    True when an error means 'this item type exposes no connections', as opposed
    to a genuine failure (auth, 5xx, throttling exhaustion, network).
    """
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        if status_code in _NO_CONNECTIONS_STATUS_CODES:
            return True

    text = str(error).lower()

    # Fallback for errors raised without a status code attribute
    if status_code is None:
        for code in _NO_CONNECTIONS_STATUS_CODES:
            if f"api error {code}" in text:
                return True

    return any(marker in text for marker in _NO_CONNECTIONS_MARKERS)


def _paged_get(endpoint: str) -> List[Dict[str, Any]]:
    """GET a Fabric collection endpoint, following continuation tokens."""
    values: List[Dict[str, Any]] = []
    continuation_token = None

    while True:
        url = endpoint
        if continuation_token:
            sep = "&" if "?" in endpoint else "?"
            url = f"{endpoint}{sep}continuationToken={continuation_token}"

        response = common.api_call("GET", url)
        if not isinstance(response, dict):
            break

        values.extend(response.get("value", []))

        continuation_token = response.get("continuationToken")
        if not continuation_token:
            break

    return values


def resolve_service_principal_object_id(logger) -> str:
    """
    Resolve the Entra object ID used for connection role grants.

    Falls back to CLIENT_ID when SERVICE_PRINCIPAL_OBJECT_ID is unset, because a
    misconfiguration here should not abort the whole permissions sync.

    Returns:
        Principal object ID string (possibly the fallback client ID, or "")
    """
    object_id = (getattr(common, "SERVICE_PRINCIPAL_OBJECT_ID", "") or "").strip()
    if object_id:
        return object_id

    fallback = (getattr(common, "CLIENT_ID", "") or "").strip()
    logger.warning(
        "SERVICE_PRINCIPAL_OBJECT_ID is not set — falling back to CLIENT_ID. "
        "Connection role grants will most likely fail: the Fabric roleAssignments "
        "API identifies principals by their Entra object ID, which is different "
        "from the application (client) ID. Copy the Object ID from Entra "
        "(Enterprise applications -> your app -> Object ID) into "
        "SERVICE_PRINCIPAL_OBJECT_ID."
    )
    return fallback


def get_item_connections(
    workspace_id: str,
    item_id: str,
    logger,
) -> List[Dict[str, Any]]:
    """
    List the connections an item uses.

    Args:
        workspace_id: Workspace GUID
        item_id: Item GUID
        logger: Logger instance

    Returns:
        List of connection dicts (empty when the item uses none)

    Raises:
        Exception: propagated from the API. Callers should use
            _is_no_connections_error() to tell an item type that has no
            connections endpoint apart from a genuine failure.
    """
    connections = _paged_get(f"/workspaces/{workspace_id}/items/{item_id}/connections")
    logger.debug(f"Item {item_id} uses {len(connections)} connection(s)")
    return connections


def get_connection_role_assignments(connection_id: str) -> List[Dict[str, Any]]:
    """Get existing role assignments on a connection."""
    return _paged_get(f"/connections/{connection_id}/roleAssignments")


def sync_connection_role_assignments(
    secondary_workspace_id: str,
    principal_id: str,
    logger,
    dry_run: bool = False,
    role: str = CONNECTION_ROLE,
) -> Dict[str, Any]:
    """
    Delta-sync connection role assignments for the DR executing principal.

    Walks every item in the secondary workspace, collects the deduplicated set of
    connections those items actually use, and grants the principal a role on each
    connection it does not already hold one on.

    Item types that expose no connections endpoint are counted as scanned with
    zero connections, not as failures — `items_failed` is reserved for genuine
    errors (auth, 5xx, throttling exhaustion, network) so the summary stays
    trustworthy during a real failover.  Per-item and per-connection failures are
    tolerated so a single bad item cannot abort the run.

    Args:
        secondary_workspace_id: DR workspace GUID
        principal_id: Entra object ID of the service principal to grant
        logger: Logger instance
        dry_run: When True, no grants are issued
        role: Connection role to grant (Owner | User | UserWithReshare)

    Returns:
        Result dict with counters and delta detail
    """
    result = {
        "items_scanned": 0,
        "items_without_connections": 0,
        "items_failed": 0,
        "connections_discovered": 0,
        "roles_added": 0,
        "roles_unchanged": 0,
        "roles_failed": 0,
        "connection_ids": [],
        "delta_details": [],
    }

    if not principal_id:
        logger.error(
            "No principal ID available — set SERVICE_PRINCIPAL_OBJECT_ID (or "
            "CLIENT_ID) to grant connection roles. Skipping connection sync."
        )
        return result

    try:
        items = common.get_items(secondary_workspace_id)
        logger.info(f"Scanning {len(items)} secondary items for connection usage...")

        # ── Build the deduplicated connection set ─────────────────────────
        connections: Dict[str, str] = {}

        for item in items:
            item_id = item.get("id")
            item_name = item.get("displayName", item_id)
            if not item_id:
                continue

            try:
                item_connections = get_item_connections(
                    secondary_workspace_id, item_id, logger
                )
            except Exception as e:
                if _is_no_connections_error(e):
                    # Expected: this item type has no connections endpoint.
                    logger.debug(f"  '{item_name}' exposes no connections endpoint")
                    result["items_scanned"] += 1
                    result["items_without_connections"] += 1
                else:
                    logger.warning(
                        f"Error listing connections for '{item_name}': {str(e)[:200]}"
                    )
                    result["items_failed"] += 1
                continue

            result["items_scanned"] += 1
            if not item_connections:
                result["items_without_connections"] += 1

            for conn in item_connections:
                conn_id = conn.get("id")
                if not conn_id:
                    continue
                # Many items share the same connection — grant it only once.
                connections.setdefault(
                    conn_id, conn.get("displayName") or conn.get("name") or conn_id
                )
                logger.debug(f"  '{item_name}' uses connection {conn_id}")

        result["connections_discovered"] = len(connections)
        result["connection_ids"] = sorted(connections)
        logger.info(
            f"Found {len(connections)} distinct connection(s) in use across "
            f"{result['items_scanned']} scanned item(s) "
            f"({result['items_without_connections']} use none, "
            f"{result['items_failed']} could not be read)"
        )

        # ── Delta: grant only where the principal has no role yet ─────────
        for conn_id, conn_name in connections.items():
            try:
                existing = get_connection_role_assignments(conn_id)
            except Exception as e:
                logger.error(
                    f"Failed to read role assignments on connection {conn_id}: {str(e)[:200]}"
                )
                result["roles_failed"] += 1
                continue

            existing_role = None
            for assignment in existing:
                if assignment.get("principal", {}).get("id") == principal_id:
                    existing_role = assignment.get("role")
                    break

            if existing_role is not None:
                logger.info(
                    f"[DELTA SKIP] {conn_name} ({conn_id}): principal already has "
                    f"role {existing_role}"
                )
                result["roles_unchanged"] += 1
                result["delta_details"].append(
                    {"action": "skip", "connection": conn_id, "role": existing_role}
                )
                continue

            logger.info(f"[DELTA ADD] {conn_name} ({conn_id}) → {role}")
            result["delta_details"].append(
                {"action": "add", "connection": conn_id, "role": role}
            )

            if dry_run:
                logger.info(f"[DRY RUN] Would POST role {role} on connection {conn_id}")
                result["roles_added"] += 1
                continue

            try:
                common.api_call(
                    "POST",
                    f"/connections/{conn_id}/roleAssignments",
                    {
                        "principal": {"id": principal_id, "type": "ServicePrincipal"},
                        "role": role,
                    },
                )
                logger.info(f"✓ Granted {role} on connection {conn_name}")
                result["roles_added"] += 1
            except Exception as e:
                logger.error(
                    f"Failed to grant {role} on connection {conn_id}: {str(e)[:200]}"
                )
                result["roles_failed"] += 1

        logger.info(
            f"Connection roles delta: "
            f"+{result['roles_added']} granted, "
            f"={result['roles_unchanged']} unchanged, "
            f"x{result['roles_failed']} failed "
            f"({result['connections_discovered']} connection(s) in use)"
        )

    except Exception as e:
        logger.error(f"Error in connection role assignment sync: {str(e)}")

    return result


def get_workspace_permissions(workspace_id: str, logger) -> List[Dict[str, Any]]:
    """
    Get workspace-level role assignments.
    
    Args:
        workspace_id: Workspace GUID
        logger: Logger instance
        
    Returns:
        List of role assignments
    """
    try:
        endpoint = f"/workspaces/{workspace_id}/roleAssignments"
        response = common.api_call("GET", endpoint)
        assignments = response.get("value", [])
        logger.info(f"Found {len(assignments)} workspace-level role assignments")
        return assignments
    except Exception as e:
        logger.error(f"Error getting workspace permissions: {str(e)}")
        return []


def get_item_permissions(
    workspace_id: str,
    item_id: str,
    logger,
) -> List[Dict[str, Any]]:
    """
    Get permissions for a specific item.
    
    Args:
        workspace_id: Workspace GUID
        item_id: Item GUID
        logger: Logger instance
        
    Returns:
        List of permission assignments
    """
    try:
        endpoint = f"/workspaces/{workspace_id}/items/{item_id}/permissions"
        response = common.api_call("GET", endpoint)
        assignments = response.get("value", [])
        return assignments
    except Exception as e:
        logger.debug(f"Error getting item permissions: {str(e)}")
        return []


def sync_workspace_permissions(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Delta-sync workspace-level role assignments.

    Compares primary vs secondary by principal_id.  Only applies changes:
      - POST   new assignments (principal exists only on primary)
      - PATCH  changed roles   (same principal, different role)
      - Report removed         (principal exists only on secondary — logged but not auto-deleted for safety)
      - Skip   unchanged       (same principal + role)
    """
    result = {
        "permissions_added": 0,
        "permissions_updated": 0,
        "permissions_unchanged": 0,
        "permissions_removed_detected": 0,
        "permissions_failed": [],
        "primary_assignments": [],
        "secondary_assignments": [],
        "delta_details": [],
    }

    try:
        # ── Fetch both sides ──────────────────────────────────────────────
        primary_assignments = get_workspace_permissions(primary_workspace_id, logger)
        result["primary_assignments"] = [
            {"principal": a.get("principal"), "role": a.get("role")}
            for a in primary_assignments
        ]

        secondary_assignments = get_workspace_permissions(secondary_workspace_id, logger)
        result["secondary_assignments"] = [
            {"principal": a.get("principal"), "role": a.get("role")}
            for a in secondary_assignments
        ]

        # ── Build lookup dicts keyed by principal_id ──────────────────────
        primary_by_pid = {}
        for a in primary_assignments:
            pid = a.get("principal", {}).get("id")
            if pid:
                primary_by_pid[pid] = a

        secondary_by_pid = {}
        for a in secondary_assignments:
            pid = a.get("principal", {}).get("id")
            if pid:
                secondary_by_pid[pid] = a

        # ── Delta: added + changed ────────────────────────────────────────
        for pid, p_assgn in primary_by_pid.items():
            p_role = p_assgn.get("role")
            p_type = p_assgn.get("principal", {}).get("type")

            s_assgn = secondary_by_pid.get(pid)

            if s_assgn is None:
                # NEW — principal missing in secondary
                logger.info(f"[DELTA ADD] {pid} ({p_type}) → {p_role}")
                result["delta_details"].append({"action": "add", "principal": pid, "role": p_role})

                if dry_run:
                    logger.info(f"[DRY RUN] Would POST role {p_role} for {pid}")
                    result["permissions_added"] += 1
                else:
                    try:
                        common.api_call(
                            "POST",
                            f"/workspaces/{secondary_workspace_id}/roleAssignments",
                            {"principal": {"id": pid, "type": p_type}, "role": p_role},
                        )
                        logger.info(f"✓ Added {p_role} for {pid}")
                        result["permissions_added"] += 1
                    except Exception as e:
                        logger.error(f"Failed to add permission: {str(e)}")
                        result["permissions_failed"].append({"principal": pid, "role": p_role, "error": str(e)})

            elif s_assgn.get("role") != p_role:
                # CHANGED — same principal, different role
                s_role = s_assgn.get("role")
                ra_id = s_assgn.get("id", "")
                logger.info(f"[DELTA CHANGE] {pid}: {s_role} → {p_role}")
                result["delta_details"].append({"action": "change", "principal": pid, "from": s_role, "to": p_role})

                if dry_run:
                    logger.info(f"[DRY RUN] Would PATCH role {s_role}→{p_role} for {pid}")
                    result["permissions_updated"] += 1
                else:
                    try:
                        common.api_call(
                            "PATCH",
                            f"/workspaces/{secondary_workspace_id}/roleAssignments/{ra_id}",
                            {"role": p_role},
                        )
                        logger.info(f"✓ Updated {pid} from {s_role} to {p_role}")
                        result["permissions_updated"] += 1
                    except Exception as e:
                        logger.error(f"Failed to update permission: {str(e)}")
                        result["permissions_failed"].append({"principal": pid, "role": p_role, "error": str(e)})
            else:
                # UNCHANGED
                result["permissions_unchanged"] += 1

        # ── Delta: removed (in secondary but not primary) ─────────────────
        for pid in secondary_by_pid:
            if pid not in primary_by_pid:
                s_role = secondary_by_pid[pid].get("role")
                logger.info(f"[DELTA REMOVED] {pid} has role {s_role} in secondary but not in primary")
                result["delta_details"].append({"action": "removed_detected", "principal": pid, "role": s_role})
                result["permissions_removed_detected"] += 1

        logger.info(
            f"Workspace permissions delta: "
            f"+{result['permissions_added']} added, "
            f"~{result['permissions_updated']} updated, "
            f"={result['permissions_unchanged']} unchanged, "
            f"-{result['permissions_removed_detected']} removed (not auto-deleted)"
        )

    except Exception as e:
        logger.error(f"Error syncing workspace permissions: {str(e)}")

    return result


def sync_item_permissions(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    artifact_mapping: Dict[str, str],
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Delta-sync item-level permissions.

    For each item in primary that has a secondary counterpart, compares existing
    secondary permissions and only applies the difference.
    """
    result = {
        "items_processed": 0,
        "items_skipped": 0,
        "item_permissions_added": 0,
        "item_permissions_unchanged": 0,
        "item_permissions_failed": 0,
    }

    try:
        primary_items = common.get_items(primary_workspace_id)
        logger.info(f"Delta-syncing permissions for {len(primary_items)} items...")

        for primary_item in primary_items:
            primary_item_id = primary_item["id"]
            secondary_item_id = artifact_mapping.get(primary_item_id)

            if not secondary_item_id:
                result["items_skipped"] += 1
                continue

            try:
                # Get both sides
                p_perms = get_item_permissions(primary_workspace_id, primary_item_id, logger)
                if not p_perms:
                    continue

                s_perms = get_item_permissions(secondary_workspace_id, secondary_item_id, logger)

                # Build secondary key set: (principal_id, role)
                s_keys = {
                    (p.get("principal", {}).get("id"), p.get("role"))
                    for p in s_perms
                }

                for perm in p_perms:
                    principal_id = perm.get("principal", {}).get("id")
                    principal_type = perm.get("principal", {}).get("type")
                    role = perm.get("role")
                    key = (principal_id, role)

                    if key in s_keys:
                        result["item_permissions_unchanged"] += 1
                        continue

                    # Missing in secondary → apply
                    if dry_run:
                        result["item_permissions_added"] += 1
                    else:
                        try:
                            common.set_item_permissions(
                                secondary_workspace_id,
                                secondary_item_id,
                                principal_id,
                                principal_type,
                                role,
                            )
                            result["item_permissions_added"] += 1
                        except Exception as e:
                            logger.debug(f"Failed to set item permission: {str(e)}")
                            result["item_permissions_failed"] += 1

                result["items_processed"] += 1

            except Exception as e:
                logger.error(f"Error syncing permissions for item {primary_item_id}: {str(e)}")

        logger.info(
            f"Item permissions delta: "
            f"+{result['item_permissions_added']} added, "
            f"={result['item_permissions_unchanged']} unchanged, "
            f"x{result['item_permissions_failed']} failed"
        )

    except Exception as e:
        logger.error(f"Error in item permission sync: {str(e)}")

    return result


def _get_data_access_roles(workspace_id: str, item_id: str, logger):
    """Get OneLake Data Access Roles for an item. Returns (roles_list, error_or_None)."""
    try:
        resp = common.api_call("GET", f"/workspaces/{workspace_id}/items/{item_id}/dataAccessRoles")
        roles = resp.get("value", []) if isinstance(resp, dict) else []
        return roles, None
    except Exception as e:
        return [], str(e)[:300]


def _enable_onelake_security(workspace_id: str, item_id: str, logger):
    """Enable OneLake security on an item by PUT-ting a DefaultReader role."""
    default_role = {
        "name": "DefaultReader",
        "decisionRules": [{
            "effect": "Permit",
            "permission": [
                {"attributeName": "Path", "attributeValueIncludedIn": ["*"]},
                {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
            ],
        }],
        "members": {
            "fabricItemMembers": [{
                "itemAccess": ["ReadAll"],
                "sourcePath": f"{workspace_id}/{item_id}",
            }],
        },
    }
    try:
        common.api_call("PUT", f"/workspaces/{workspace_id}/items/{item_id}/dataAccessRoles",
                         payload={"value": [default_role]})
        logger.info(f"✓ Enabled OneLake security on {item_id}")
        return True, "Enabled"
    except Exception as e:
        return False, str(e)[:300]


def _normalize_role_for_comparison(role: Dict) -> Dict:
    """Return a deterministic dict for role comparison (sort lists, strip whitespace)."""
    def _sorted_rules(rules):
        out = []
        for r in rules:
            perms = r.get("permission", [])
            sorted_perms = sorted(perms, key=lambda p: p.get("attributeName", ""))
            out.append({"effect": r.get("effect", ""), "permission": sorted_perms})
        return sorted(out, key=lambda r: r.get("effect", ""))

    def _sorted_members(members):
        entra = sorted(members.get("microsoftEntraMembers", []),
                       key=lambda m: m.get("tenantId", "") + m.get("objectId", ""))
        fim = sorted(members.get("fabricItemMembers", []),
                     key=lambda m: m.get("sourcePath", ""))
        return {"microsoftEntraMembers": entra, "fabricItemMembers": fim}

    return {
        "name": role.get("name", ""),
        "decisionRules": _sorted_rules(role.get("decisionRules", [])),
        "members": _sorted_members(role.get("members", {})),
    }


def sync_data_access_roles(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Delta-sync OneLake Data Access Roles from primary to secondary lakehouses.

    Compares remapped primary roles with existing secondary roles.
    Skips the PUT call entirely when roles are already identical.
    """
    result = {
        "lakehouses_processed": 0,
        "lakehouses_skipped": 0,
        "lakehouses_unchanged": 0,
        "roles_synced": 0,
        "roles_failed": 0,
        "security_enabled": 0,
    }
    
    try:
        p_items = common.get_items(primary_workspace_id)
        s_items = common.get_items(secondary_workspace_id)
        
        p_lakehouses = [i for i in p_items if i.get("type") == "Lakehouse"]
        s_lh_by_name = {i["displayName"]: i for i in s_items if i.get("type") == "Lakehouse"}
        
        logger.info(f"Found {len(p_lakehouses)} primary lakehouses")
        
        for p_lh in p_lakehouses:
            name = p_lh["displayName"]
            p_lh_id = p_lh["id"]
            s_lh = s_lh_by_name.get(name)
            
            if not s_lh:
                logger.debug(f"Skipping {name}: no matching secondary lakehouse")
                result["lakehouses_skipped"] += 1
                continue
            
            s_lh_id = s_lh["id"]
            
            # Get primary roles
            p_roles, p_err = _get_data_access_roles(primary_workspace_id, p_lh_id, logger)
            if p_err or not p_roles:
                logger.info(f"Skipping {name}: no data access roles on primary")
                result["lakehouses_skipped"] += 1
                continue
            
            logger.info(f"Syncing {len(p_roles)} role(s) for {name}...")
            
            # Check if secondary has security enabled
            s_roles, s_err = _get_data_access_roles(secondary_workspace_id, s_lh_id, logger)
            if s_err and "UniversalSecurityFeatureDisabled" in s_err:
                logger.info(f"OneLake security not enabled on secondary {name}, enabling...")
                if dry_run:
                    logger.info(f"[DRY RUN] Would enable OneLake security on secondary {name}")
                    result["security_enabled"] += 1
                else:
                    ok, msg = _enable_onelake_security(secondary_workspace_id, s_lh_id, logger)
                    if ok:
                        result["security_enabled"] += 1
                        s_roles, s_err = _get_data_access_roles(secondary_workspace_id, s_lh_id, logger)
                    else:
                        logger.error(
                            f"Cannot enable OneLake security on secondary {name}. "
                            f"Enable it manually in Fabric portal: open lakehouse → "
                            f"Manage OneLake security (preview)")
                        result["roles_failed"] += len(p_roles)
                        continue
            
            # Build remapped roles
            remapped_roles = []
            for p_role in p_roles:
                members = p_role.get("members", {})
                remapped_members = {
                    "microsoftEntraMembers": members.get("microsoftEntraMembers", []),
                    "fabricItemMembers": [],
                }
                for fim in members.get("fabricItemMembers", []):
                    source_path = fim.get("sourcePath", "")
                    if source_path.startswith(primary_workspace_id):
                        remapped_path = source_path.replace(
                            primary_workspace_id, secondary_workspace_id, 1)
                        remapped_path = remapped_path.replace(p_lh_id, s_lh_id, 1)
                    else:
                        remapped_path = source_path
                    remapped_members["fabricItemMembers"].append({
                        "itemAccess": fim.get("itemAccess", []),
                        "sourcePath": remapped_path,
                    })
                
                remapped_roles.append({
                    "name": p_role.get("name", ""),
                    "decisionRules": p_role.get("decisionRules", []),
                    "members": remapped_members,
                })

            # ── Delta comparison: skip PUT if roles are identical ─────────
            norm_remapped = sorted(
                [_normalize_role_for_comparison(r) for r in remapped_roles],
                key=lambda r: r["name"],
            )
            norm_secondary = sorted(
                [_normalize_role_for_comparison(r) for r in s_roles],
                key=lambda r: r["name"],
            )

            if norm_remapped == norm_secondary:
                logger.info(f"[DELTA SKIP] {name}: roles already identical ({len(norm_remapped)} role(s))")
                result["lakehouses_unchanged"] += 1
                result["lakehouses_processed"] += 1
                continue

            # Roles differ — apply
            logger.info(f"[DELTA UPDATE] {name}: roles differ, applying {len(remapped_roles)} role(s)")

            if dry_run:
                for rr in remapped_roles:
                    logger.info(f"[DRY RUN] Would sync role '{rr['name']}' to secondary {name}")
                result["roles_synced"] += len(remapped_roles)
            else:
                try:
                    common.api_call(
                        "PUT",
                        f"/workspaces/{secondary_workspace_id}/items/{s_lh_id}/dataAccessRoles",
                        payload={"value": remapped_roles},
                    )
                    for rr in remapped_roles:
                        logger.info(f"✓ Synced role '{rr['name']}' to secondary {name}")
                    result["roles_synced"] += len(remapped_roles)
                except Exception as e:
                    logger.error(f"Failed to PUT roles on secondary {name}: {str(e)[:200]}")
                    result["roles_failed"] += len(remapped_roles)

            result["lakehouses_processed"] += 1

        logger.info(
            f"OneLake DAR delta: "
            f"{result['lakehouses_processed']} processed, "
            f"={result['lakehouses_unchanged']} unchanged, "
            f"+{result['roles_synced']} synced, "
            f"x{result['roles_failed']} failed"
        )

    except Exception as e:
        logger.error(f"Error in data access roles sync: {str(e)}")

    return result


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Sync Fabric workspace permissions")
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
    
    logger = common.setup_logger("sync_permissions")
    common.DRY_RUN = args.dry_run
    
    if args.dry_run:
        logger.info("DRY RUN MODE - no changes will be made")
    
    try:
        sync_summary = {
            "workspace_permissions": {},
            "item_permissions": {},
            "data_access_roles": {},
            "connection_role_assignments": {},
        }
        
        # Sync OneLake Data Access Roles (RLS/CLS)
        logger.info("\n=== SYNCING ONELAKE DATA ACCESS ROLES ===")
        dar_result = sync_data_access_roles(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["data_access_roles"] = dar_result
        
        # Sync workspace permissions
        logger.info("\n=== SYNCING WORKSPACE PERMISSIONS ===")
        ws_perm_result = sync_workspace_permissions(
            args.primary_workspace,
            args.secondary_workspace,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["workspace_permissions"] = ws_perm_result
        
        # Sync item permissions
        logger.info("\n=== SYNCING ITEM PERMISSIONS ===")
        artifact_mapping = common.load_artifact_mapping()
        item_perm_result = sync_item_permissions(
            args.primary_workspace,
            args.secondary_workspace,
            artifact_mapping,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["item_permissions"] = item_perm_result
        
        # Grant the DR executing principal a role on every connection in use
        logger.info("\n=== SYNCING CONNECTION ROLE ASSIGNMENTS ===")
        principal_id = resolve_service_principal_object_id(logger)
        conn_role_result = sync_connection_role_assignments(
            args.secondary_workspace,
            principal_id,
            logger,
            dry_run=args.dry_run,
        )
        sync_summary["connection_role_assignments"] = conn_role_result
        
        common.save_json(sync_summary, "data/permissions_audit.json")
        
        print("\n" + "=" * 70)
        print("PERMISSIONS SYNC SUMMARY (DELTA)")
        print("=" * 70)
        print(f"--- OneLake Data Access Roles ---")
        print(f"  Lakehouses Processed:      {dar_result['lakehouses_processed']}")
        print(f"  Lakehouses Unchanged:      {dar_result['lakehouses_unchanged']}")
        print(f"  Lakehouses Skipped:        {dar_result['lakehouses_skipped']}")
        print(f"  Roles Synced:              {dar_result['roles_synced']}")
        print(f"  Roles Failed:              {dar_result['roles_failed']}")
        print(f"  Security Auto-Enabled:     {dar_result['security_enabled']}")
        print(f"--- Workspace Permissions ---")
        print(f"  Added:                     {ws_perm_result['permissions_added']}")
        print(f"  Updated:                   {ws_perm_result['permissions_updated']}")
        print(f"  Unchanged:                 {ws_perm_result['permissions_unchanged']}")
        print(f"  Removed (detected only):   {ws_perm_result['permissions_removed_detected']}")
        print(f"  Failed:                    {len(ws_perm_result['permissions_failed'])}")
        print(f"--- Item Permissions ---")
        print(f"  Items Processed:           {item_perm_result['items_processed']}")
        print(f"  Permissions Added:         {item_perm_result['item_permissions_added']}")
        print(f"  Permissions Unchanged:     {item_perm_result['item_permissions_unchanged']}")
        print(f"  Permissions Failed:        {item_perm_result['item_permissions_failed']}")
        print(f"--- Connection Role Assignments ---")
        print(f"  Items Scanned:             {conn_role_result['items_scanned']}")
        print(f"  Items Using No Connection: {conn_role_result['items_without_connections']}")
        print(f"  Items Unreadable:          {conn_role_result['items_failed']}")
        print(f"  Connections In Use:        {conn_role_result['connections_discovered']}")
        print(f"  Roles Granted:             {conn_role_result['roles_added']}")
        print(f"  Roles Unchanged:           {conn_role_result['roles_unchanged']}")
        print(f"  Roles Failed:              {conn_role_result['roles_failed']}")
        print("=" * 70 + "\n")
        
        logger.info("Permissions sync complete")
        return True
    
    except Exception as e:
        logger.error(f"Error in permissions sync: {str(e)}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

