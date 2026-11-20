"""
Item-level permission replication using SUPPORTED, documented APIs only.

Fabric has no public API to write per-item permissions for most item types
(see IMPLEMENTATION_GUIDE.md section 28.2). This module does what the
documented APIs allow, and reports the rest explicitly instead of guessing:

  Detect (read)
    * Fabric Admin  GET /v1/admin/workspaces/{ws}/items/{item}/users
        all item types; caller must be a Fabric administrator, or a service
        principal with the "Service principals can access read-only admin APIs"
        tenant setting. Preview; 200 requests/hour.
    * Power BI      GET /v1.0/myorg/groups/{ws}/datasets/{id}/users
        semantic models only; GA; no admin rights required.

  Replicate (write)
    * Power BI      POST/PUT /v1.0/myorg/groups/{ws}/datasets/{id}/users
        semantic models only; GA. Cannot grant Write; cannot grant to service
        principals (principalType App). Both are flagged, not silently dropped.

  Everything else
    * Emitted as a manual remediation row (share in the Fabric portal), and
      re-checked with the same read API, so the gap is tracked to closure.

Principals that hold a workspace role are excluded from "direct shares":
workspace roles are replicated separately through roleAssignments.

No undocumented or internal endpoints are used.
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

RIGHT_ORDER = ("Read", "Write", "Reshare", "Explore")
GRANTABLE_ON_DATASET = {"Read", "Reshare", "Explore"}

ACTION_GRANT = "grant_dataset_user"
ACTION_UPGRADE = "upgrade_dataset_user"
ACTION_MANUAL = "manual_portal_share"
ACTION_PRESENT = "already_present"


class AdminAccessRequired(Exception):
    """The Fabric admin item-access API returned 401/403 for this caller."""


# ----------------------------------------------------------------- normalise

def rights_from_dataset_right(value: Optional[str]) -> Set[str]:
    """'ReadWriteReshareExplore' -> {'Read','Write','Reshare','Explore'}; 'None' -> set()."""
    if not value or value == "None":
        return set()
    return {r for r in RIGHT_ORDER if r in value}


def dataset_right_from_rights(rights: Iterable[str]) -> str:
    """Compose the DatasetUserAccessRight enum value from a set of rights."""
    rs = set(rights)
    if not rs:
        return "None"
    rs.add("Read")
    return "".join(r for r in RIGHT_ORDER if r in rs)


def principal_keys(principal: Dict[str, Any]) -> Set[str]:
    """All identifiers a principal may be known by (object id and, for users, UPN)."""
    keys = set()
    for k in ("id", "identifier", "graphId"):
        v = principal.get(k)
        if v:
            keys.add(str(v).lower())
    upn = (principal.get("userDetails") or {}).get("userPrincipalName") or principal.get("emailAddress")
    if upn:
        keys.add(upn.lower())
    return keys


def workspace_role_keys(role_assignments: List[Dict[str, Any]]) -> Set[str]:
    keys: Set[str] = set()
    for ra in role_assignments:
        keys |= principal_keys(ra.get("principal") or {})
    return keys


# ------------------------------------------------------------------ readers

def read_dataset_shares(pbi_call: Callable, workspace_id: str, dataset_id: str,
                        role_keys: Set[str]) -> List[Dict[str, Any]]:
    """Direct semantic-model shares (workspace-role holders excluded)."""
    resp = pbi_call("GET", f"/groups/{workspace_id}/datasets/{dataset_id}/users")
    out = []
    for u in resp.get("value", []):
        keys = principal_keys(u)
        if keys & role_keys:
            continue
        rights = rights_from_dataset_right(u.get("datasetUserAccessRight"))
        if not rights:
            continue
        out.append({
            "principal_key": sorted(keys)[0] if keys else "",
            "identifier": u.get("identifier", ""),
            "principal_type": u.get("principalType", ""),
            "rights": rights,
            "keys": keys,
        })
    return out


_ADMIN_TYPE_PARAM = {"Report", "Dashboard", "SemanticModel", "App", "Dataflow"}
_PRINCIPAL_TYPE_TO_PBI = {"User": "User", "Group": "Group", "ServicePrincipal": "App",
                          "ServicePrincipalProfile": "App", "EntireTenant": "None"}


def read_admin_item_shares(fabric_call: Callable, workspace_id: str, item_id: str, item_type: str,
                           role_keys: Set[str]) -> List[Dict[str, Any]]:
    """Direct item shares from the Fabric admin API (workspace-role holders excluded)."""
    endpoint = f"/admin/workspaces/{workspace_id}/items/{item_id}/users"
    if item_type in _ADMIN_TYPE_PARAM:
        endpoint += f"?type={item_type}"
    try:
        resp = fabric_call("GET", endpoint)
    except Exception as e:  # FabricApiError carries status_code
        if getattr(e, "status_code", None) in (401, 403):
            raise AdminAccessRequired(str(e)) from e
        raise
    out = []
    for entry in resp.get("accessDetails", []):
        p = entry.get("principal") or {}
        keys = principal_keys(p)
        if keys & role_keys:
            continue
        details = entry.get("itemAccessDetails") or {}
        rights = {r for r in details.get("permissions", []) if r in RIGHT_ORDER}
        if not rights:
            continue
        upn = (p.get("userDetails") or {}).get("userPrincipalName")
        out.append({
            "principal_key": (upn or p.get("id") or "").lower(),
            "identifier": upn or p.get("id", ""),
            "principal_type": _PRINCIPAL_TYPE_TO_PBI.get(p.get("type", ""), p.get("type", "")),
            "display_name": p.get("displayName", ""),
            "rights": rights,
            "additional": list(details.get("additionalPermissions", [])),
            "keys": keys,
        })
    return out


# ------------------------------------------------------------------- planner

def plan_for_item(item: Dict[str, Any], secondary_item_id: str,
                  primary_shares: List[Dict[str, Any]],
                  secondary_shares: List[Dict[str, Any]],
                  secondary_role_keys: Set[str]) -> List[Dict[str, Any]]:
    """Delta rows for one primary item vs its secondary counterpart. Pure (unit-tested)."""
    rows = []
    s_by_key: Dict[str, Set[str]] = {}
    for s in secondary_shares:
        for k in s["keys"]:
            s_by_key[k] = s_by_key.get(k, set()) | s["rights"]

    for share in primary_shares:
        base = {
            "item_type": item["type"], "item_name": item["displayName"],
            "primary_item_id": item["id"], "secondary_item_id": secondary_item_id,
            "principal": share["identifier"], "principal_type": share["principal_type"],
            "rights": dataset_right_from_rights(share["rights"]), "note": "",
        }
        if share["keys"] & secondary_role_keys:
            rows.append({**base, "action": ACTION_PRESENT, "note": "has a workspace role in secondary"})
            continue
        have = set()
        for k in share["keys"]:
            have |= s_by_key.get(k, set())
        if share["rights"] <= have:
            rows.append({**base, "action": ACTION_PRESENT})
            continue

        if item["type"] != "SemanticModel":
            rows.append({**base, "action": ACTION_MANUAL,
                         "note": "no public API to write item permissions for this item type; share in the Fabric portal"})
            continue
        if share["principal_type"] == "App":
            rows.append({**base, "action": ACTION_MANUAL,
                         "note": "dataset users API cannot grant to service principals"})
            continue

        grantable = share["rights"] & GRANTABLE_ON_DATASET
        note = ""
        if "Write" in share["rights"]:
            note = "Write cannot be granted via API; give the principal Contributor on the secondary workspace or grant Write in the portal"
        rows.append({**base,
                     "action": ACTION_UPGRADE if have else ACTION_GRANT,
                     "rights": dataset_right_from_rights(grantable | have),
                     "note": note})
    return rows


def build_plan(fabric_call: Callable, pbi_call: Callable,
               primary_ws: str, secondary_ws: str,
               primary_items: List[Dict[str, Any]], secondary_items: List[Dict[str, Any]],
               use_admin_api: bool = True, logger=None) -> Dict[str, Any]:
    """Detect direct shares on every matched item and plan the delta. Read-only."""
    p_roles = fabric_call("GET", f"/workspaces/{primary_ws}/roleAssignments").get("value", [])
    s_roles = fabric_call("GET", f"/workspaces/{secondary_ws}/roleAssignments").get("value", [])
    p_role_keys, s_role_keys = workspace_role_keys(p_roles), workspace_role_keys(s_roles)
    s_by_key = {(i["type"], i["displayName"]): i for i in secondary_items}

    rows: List[Dict[str, Any]] = []
    coverage = {"scanned_admin_api": 0, "scanned_dataset_api": 0, "not_scanned": 0,
                "unmatched_in_secondary": 0, "errors": []}
    admin_available = use_admin_api
    not_scanned_types: Dict[str, int] = {}

    for item in primary_items:
        if item.get("displayName", "").startswith("BCDR_") or item.get("type") == "SQLEndpoint":
            continue
        s_item = s_by_key.get((item["type"], item["displayName"]))
        if not s_item:
            coverage["unmatched_in_secondary"] += 1
            continue
        try:
            if item["type"] == "SemanticModel":
                p_sh = read_dataset_shares(pbi_call, primary_ws, item["id"], p_role_keys)
                s_sh = read_dataset_shares(pbi_call, secondary_ws, s_item["id"], s_role_keys)
                coverage["scanned_dataset_api"] += 1
            elif admin_available:
                try:
                    p_sh = read_admin_item_shares(fabric_call, primary_ws, item["id"], item["type"], p_role_keys)
                    s_sh = read_admin_item_shares(fabric_call, secondary_ws, s_item["id"], s_item["type"], s_role_keys)
                    coverage["scanned_admin_api"] += 1
                except AdminAccessRequired:
                    admin_available = False
                    coverage["admin_api_denied"] = True
                    not_scanned_types[item["type"]] = not_scanned_types.get(item["type"], 0) + 1
                    coverage["not_scanned"] += 1
                    continue
            else:
                not_scanned_types[item["type"]] = not_scanned_types.get(item["type"], 0) + 1
                coverage["not_scanned"] += 1
                continue
        except Exception as e:
            coverage["errors"].append(f"{item['type']}:{item['displayName']}: {e}")
            if logger:
                logger.error(f"Item permission read failed for {item['displayName']}: {e}")
            continue
        rows.extend(plan_for_item(item, s_item["id"], p_sh, s_sh, s_role_keys))

    coverage["not_scanned_by_type"] = not_scanned_types
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {
        "rows": rows,
        "counts": counts,
        "coverage": coverage,
        "complete": coverage["not_scanned"] == 0 and not coverage["errors"],
    }


# ------------------------------------------------------------------- applier

def apply_rows(pbi_call: Callable, secondary_ws: str, rows: List[Dict[str, Any]], logger=None) -> Dict[str, Any]:
    """Apply only API-grantable rows (semantic models). Fails loudly per row."""
    result = {"applied": 0, "failed": 0, "skipped_manual": 0, "failures": []}
    for r in rows:
        if r["action"] not in (ACTION_GRANT, ACTION_UPGRADE):
            if r["action"] == ACTION_MANUAL:
                result["skipped_manual"] += 1
            continue
        body = {"identifier": r["principal"], "principalType": r["principal_type"],
                "datasetUserAccessRight": r["rights"]}
        method = "PUT" if r["action"] == ACTION_UPGRADE else "POST"
        try:
            pbi_call(method, f"/groups/{secondary_ws}/datasets/{r['secondary_item_id']}/users", body)
            result["applied"] += 1
            if logger:
                logger.info(f"[GRANT] {r['item_name']} -> {r['principal']} ({r['rights']})")
        except Exception as e:
            result["failed"] += 1
            result["failures"].append({"item": r["item_name"], "principal": r["principal"], "error": str(e)[:300]})
            if logger:
                logger.error(f"[GRANT FAILED] {r['item_name']} -> {r['principal']}: {e}")
    return result
