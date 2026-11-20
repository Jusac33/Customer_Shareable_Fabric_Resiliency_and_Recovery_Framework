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
    * No public API writes a direct share on other item types. The supported
      workaround is the ACCESS-GROUP BOOTSTRAP (plan_access_groups /
      apply_access_groups):
        1. Detect who has direct access to each primary item (admin API).
        2. Push those principals into an Entra security group per
           (item, permission set) with Microsoft Graph (supported, GA).
        3. Share the secondary item with that group ONCE: by API for
           semantic models (principalType Group), in the portal otherwise.
      After step 3 every later access change is a group-membership change,
      which the bootstrap keeps in sync by API. Verification expands group
      shares on the secondary into their members, so a user covered through
      the group counts as replicated.

Principals that hold a workspace role are excluded from "direct shares":
workspace roles are replicated separately through roleAssignments.

No undocumented or internal endpoints are used.
"""

import hashlib
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

RIGHT_ORDER = ("Read", "Write", "Reshare", "Explore")
GRANTABLE_ON_DATASET = {"Read", "Reshare", "Explore"}

ACTION_GRANT = "grant_dataset_user"
ACTION_UPGRADE = "upgrade_dataset_user"
ACTION_MANUAL = "manual_portal_share"
ACTION_PRESENT = "already_present"

GROUP_NICK_PREFIX = "bcdr-ia-"
SHARE_MODE_API = "api"
SHARE_MODE_PORTAL = "portal_once"
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


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
            "display_name": u.get("displayName", ""),
            "rights": rights,
            "additional": [],
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

def expand_group_shares(shares: List[Dict[str, Any]],
                        member_resolver: Optional[Callable[[str], Optional[Set[str]]]]) -> List[Dict[str, Any]]:
    """Add each group share's (transitive) members as virtual shares with the group's rights.

    A user who reaches a secondary item through a shared group is covered,
    so they must not be planned again. Resolver returns None when it cannot
    read the group; that group is then left unexpanded (conservative).
    """
    if not member_resolver:
        return shares
    out = list(shares)
    for s in shares:
        if s.get("principal_type") != "Group" or not s.get("identifier"):
            continue
        members = member_resolver(s["identifier"])
        if members:
            out.append({**s, "principal_type": "GroupMember", "keys": set(members),
                        "via_group": s["identifier"]})
    return out


def plan_for_item(item: Dict[str, Any], secondary_item_id: str,
                  primary_shares: List[Dict[str, Any]],
                  secondary_shares: List[Dict[str, Any]],
                  secondary_role_keys: Set[str]) -> List[Dict[str, Any]]:
    """Delta rows for one primary item vs its secondary counterpart. Pure (unit-tested)."""
    rows = []
    s_by_key: Dict[str, Tuple[Set[str], Set[str]]] = {}
    for s in secondary_shares:
        for k in s["keys"]:
            r, a = s_by_key.get(k, (set(), set()))
            s_by_key[k] = (r | s["rights"], a | set(s.get("additional") or []))

    for share in primary_shares:
        additional = set(share.get("additional") or [])
        base = {
            "item_type": item["type"], "item_name": item["displayName"],
            "primary_item_id": item["id"], "secondary_item_id": secondary_item_id,
            "principal": share["identifier"], "principal_type": share["principal_type"],
            "display_name": share.get("display_name", ""),
            "rights": dataset_right_from_rights(share["rights"]),
            "additional": ";".join(sorted(additional)), "note": "",
        }
        if share["keys"] & secondary_role_keys:
            rows.append({**base, "action": ACTION_PRESENT, "note": "has a workspace role in secondary"})
            continue
        have, have_add = set(), set()
        for k in share["keys"]:
            r, a = s_by_key.get(k, (set(), set()))
            have |= r
            have_add |= a
        if share["rights"] <= have and additional <= have_add:
            rows.append({**base, "action": ACTION_PRESENT})
            continue

        if item["type"] != "SemanticModel":
            rows.append({**base, "action": ACTION_MANUAL,
                         "note": "no public API writes a direct share on this item type; use the access-group bootstrap (sync_item_permissions.py --groups-plan)"})
            continue
        if share["principal_type"] == "App":
            rows.append({**base, "action": ACTION_MANUAL,
                         "note": "dataset users API cannot grant to service principals; use the access-group bootstrap (group grant is automated)"})
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
               use_admin_api: bool = True, logger=None,
               group_member_resolver: Optional[Callable[[str], Optional[Set[str]]]] = None) -> Dict[str, Any]:
    """Detect direct shares on every matched item and plan the delta. Read-only.

    group_member_resolver (optional, see make_group_member_resolver) lets
    secondary group shares count for their members (access-group bootstrap).
    """
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
        rows.extend(plan_for_item(item, s_item["id"], p_sh,
                                  expand_group_shares(s_sh, group_member_resolver), s_role_keys))

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


# ------------------------------------------------- access-group bootstrap
#
# Workaround for item types with no public share-write API: identify who has
# direct access to a primary item, push them into an Entra security group
# per (item, permission set) with Microsoft Graph, and share the secondary
# item with that group once. Group membership is then the only thing that
# changes, and Graph manages it by API.

def access_group_nickname(primary_item_id: str, rights: str, additional: str) -> str:
    """Stable mailNickname per (primary item, permission set); <= 64 chars, Graph-safe."""
    digest = hashlib.sha1(f"{rights}|{additional}".encode("utf-8")).hexdigest()[:6]
    return f"{GROUP_NICK_PREFIX}{primary_item_id.replace('-', '').lower()[:12]}-{digest}"


def access_group_display_name(item_type: str, item_name: str, rights: str, additional: str) -> str:
    extra = f" +{additional.replace(';', '+')}" if additional else ""
    return f"BCDR item access | {item_type} | {item_name} | {rights}{extra}"[:256]


def plan_access_groups(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn manual rows into group specs. Pure (unit-tested).

    * User / App principal  -> managed group (created/maintained via Graph).
    * Group principal       -> the same group must be shared once on the
                               secondary item (membership is already shared).
    * EntireTenant / other  -> unsupported (reported).
    """
    specs: Dict[Tuple, Dict[str, Any]] = {}
    unsupported: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("action") != ACTION_MANUAL:
            continue
        ptype = r.get("principal_type", "")
        additional = r.get("additional", "") or ""
        rights = r.get("rights", "")
        share_mode = SHARE_MODE_API if r.get("item_type") == "SemanticModel" else SHARE_MODE_PORTAL
        common_fields = {
            "item_type": r["item_type"], "item_name": r["item_name"],
            "primary_item_id": r["primary_item_id"], "secondary_item_id": r["secondary_item_id"],
            "rights": rights, "additional": additional, "share_mode": share_mode,
        }
        if ptype == "Group":
            key = ("existing", r["secondary_item_id"], r["principal"].lower(), rights, additional)
            specs.setdefault(key, {
                **common_fields, "kind": "existing_group", "group_id": r["principal"],
                "group_nickname": "", "group_display_name": r.get("display_name") or r["principal"],
                "members": [],
            })
        elif ptype in ("User", "App"):
            nick = access_group_nickname(r["primary_item_id"], rights, additional)
            spec = specs.setdefault(("managed", nick), {
                **common_fields, "kind": "managed_group", "group_id": "",
                "group_nickname": nick,
                "group_display_name": access_group_display_name(r["item_type"], r["item_name"], rights, additional),
                "members": [],
            })
            member = {"principal": r["principal"], "principal_type": ptype}
            if member not in spec["members"]:
                spec["members"].append(member)
        else:
            unsupported.append({**r, "note": f"principal type {ptype or '?'} cannot be a group member"})
    ordered = sorted(specs.values(), key=lambda s: (s["item_type"], s["item_name"], s["kind"], s["rights"]))
    return {"specs": ordered, "unsupported": unsupported}


class _TooManyMembers(Exception):
    pass


def _graph_paged(graph_call: Callable, path: str, max_items: Optional[int] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    resp = graph_call("GET", path)
    while True:
        out.extend(resp.get("value", []))
        if max_items is not None and len(out) > max_items:
            raise _TooManyMembers(f"more than {max_items} members")
        nxt = resp.get("@odata.nextLink")
        if not nxt:
            return out
        resp = graph_call("GET", nxt)


def _member_keys(obj: Dict[str, Any]) -> Set[str]:
    keys = {str(obj.get("id", "")).lower()} if obj.get("id") else set()
    for k in ("userPrincipalName", "mail", "appId"):
        if obj.get(k):
            keys.add(str(obj[k]).lower())
    return keys


def make_group_member_resolver(graph_call: Callable, logger=None,
                               max_members: int = 20000) -> Callable[[str], Optional[Set[str]]]:
    """Cached group-id -> transitive member keys (object id, UPN, appId).

    Returns None (group not credited, so its members stay planned) when the
    group can't be read or is larger than max_members; this keeps --verify
    bounded and never over-reports coverage.
    """
    cache: Dict[str, Optional[Set[str]]] = {}

    def resolve(group_id: str) -> Optional[Set[str]]:
        gid = (group_id or "").lower()
        if gid not in cache:
            try:
                members = _graph_paged(graph_call,
                                       f"/groups/{gid}/transitiveMembers?$select=id,userPrincipalName,mail,appId&$top=999",
                                       max_items=max_members)
                keys: Set[str] = set()
                for m in members:
                    keys |= _member_keys(m)
                cache[gid] = keys
            except Exception as e:
                cache[gid] = None
                if logger:
                    logger.warning(f"Group {gid} not credited (members unreadable: {str(e)[:160]})")
        return cache[gid]
    return resolve


def _status(e: Exception) -> Optional[int]:
    return getattr(e, "status_code", None)


def _resolve_directory_id(graph_call: Callable, principal: str, ptype: str) -> str:
    """Directory object id for a user (UPN or id) or service principal (object id)."""
    if _GUID_RE.match(principal or ""):
        return principal.lower()
    if ptype == "User":
        return graph_call("GET", f"/users/{quote(principal)}?$select=id")["id"].lower()
    raise ValueError(f"cannot resolve {ptype} principal '{principal}' to a directory object id")


def _ensure_group(graph_call: Callable, spec: Dict[str, Any], dry_run: bool, result: Dict[str, Any]) -> str:
    nick = spec["group_nickname"]
    found = graph_call("GET", f"/groups?$filter=mailNickname eq '{nick}'&$select=id,displayName").get("value", [])
    if found:
        result["groups_existing"] += 1
        return found[0]["id"].lower()
    if dry_run:
        result["groups_to_create"].append(spec["group_display_name"])
        return ""
    created = graph_call("POST", "/groups", {
        "displayName": spec["group_display_name"],
        "mailNickname": nick,
        "mailEnabled": False,
        "securityEnabled": True,
        "description": (f"Managed by the Fabric BCDR framework. Grants {spec['rights']}"
                        f"{' +' + spec['additional'] if spec['additional'] else ''} on "
                        f"{spec['item_type']} '{spec['item_name']}' in primary and secondary workspaces.")[:1024],
    })
    result["groups_created"] += 1
    return created["id"].lower()


def _group_already_shared(fabric_call: Callable, secondary_ws: str, spec: Dict[str, Any], gid: str) -> Optional[bool]:
    """True/False from the admin item-access API; None when the caller can't read it."""
    try:
        shares = read_admin_item_shares(fabric_call, secondary_ws, spec["secondary_item_id"], spec["item_type"], set())
    except Exception:
        return None
    want_r = rights_from_dataset_right(spec["rights"]) - {"Write"}
    want_a = set(filter(None, (spec.get("additional") or "").split(";")))
    for s in shares:
        if gid in s["keys"]:
            return want_r <= s["rights"] and want_a <= set(s.get("additional") or [])
    return False


def apply_access_groups(graph_call: Callable, pbi_call: Callable, secondary_ws: str,
                        specs: List[Dict[str, Any]], logger=None, dry_run: bool = False,
                        sleep: Callable[[float], None] = time.sleep,
                        fabric_call: Optional[Callable] = None) -> Dict[str, Any]:
    """Create/maintain access groups by Graph and share them on the secondary item.

    Members are only ever ADDED; extra members are reported, never removed.
    Semantic models get the group grant by API; other items yield a one-time
    portal step. Every failure is recorded per spec; nothing is skipped silently.
    """
    result: Dict[str, Any] = {
        "groups_created": 0, "groups_existing": 0, "groups_to_create": [],
        "members_added": 0, "members_present": 0, "members_to_add": 0,
        "dataset_group_grants": 0, "portal_steps": [], "extra_members": [], "failures": [],
    }
    for spec in specs:
        label = f"{spec['item_type']}:{spec['item_name']} [{spec['rights']}]"
        try:
            if spec["kind"] == "managed_group":
                gid = spec.get("group_id") or _ensure_group(graph_call, spec, dry_run, result)
                spec["group_id"] = gid
                current: Set[str] = set()
                if gid:
                    for m in _graph_paged(graph_call, f"/groups/{gid}/members?$select=id,userPrincipalName,appId&$top=999"):
                        current |= _member_keys(m)
                wanted_ids: Set[str] = set()
                for m in spec["members"]:
                    if m["principal"].lower() in current:
                        result["members_present"] += 1
                        wanted_ids.add(m["principal"].lower())
                        continue
                    try:
                        oid = _resolve_directory_id(graph_call, m["principal"], m["principal_type"])
                    except Exception as e:
                        result["failures"].append({"spec": label, "member": m["principal"], "error": str(e)[:300]})
                        continue
                    wanted_ids.add(oid)
                    if oid in current:
                        result["members_present"] += 1
                        continue
                    if dry_run or not gid:
                        result["members_to_add"] += 1
                        continue
                    for attempt in range(4):
                        try:
                            graph_call("POST", f"/groups/{gid}/members/$ref",
                                       {"@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{oid}"})
                            result["members_added"] += 1
                            break
                        except Exception as e:
                            body = str(e)
                            if _status(e) == 400 and "already exist" in body:
                                result["members_present"] += 1
                                break
                            if _status(e) == 404 and attempt < 3:
                                sleep(5 * (attempt + 1))  # new group not yet replicated in Entra
                                continue
                            result["failures"].append({"spec": label, "member": m["principal"], "error": body[:300]})
                            break
                if gid:
                    extras = sorted(k for k in current if _GUID_RE.match(k) and k not in wanted_ids)
                    if extras:
                        result["extra_members"].append({"group": spec["group_display_name"], "object_ids": extras})
            gid = spec.get("group_id", "")

            if spec["share_mode"] == SHARE_MODE_API:
                if not gid:
                    continue  # dry run: group not created yet
                grantable = rights_from_dataset_right(spec["rights"]) & GRANTABLE_ON_DATASET
                existing = pbi_call("GET", f"/groups/{secondary_ws}/datasets/{spec['secondary_item_id']}/users").get("value", [])
                have = next((rights_from_dataset_right(u.get("datasetUserAccessRight"))
                             for u in existing if str(u.get("identifier", "")).lower() == gid), None)
                if have is not None and grantable <= have:
                    continue
                if dry_run:
                    result["dataset_group_grants"] += 1
                    continue
                body = {"identifier": gid, "principalType": "Group",
                        "datasetUserAccessRight": dataset_right_from_rights(grantable | (have or set()))}
                pbi_call("PUT" if have is not None else "POST",
                         f"/groups/{secondary_ws}/datasets/{spec['secondary_item_id']}/users", body)
                result["dataset_group_grants"] += 1
                if logger:
                    logger.info(f"[GROUP GRANT] {label} -> {spec['group_display_name']}")
            else:
                shared = _group_already_shared(fabric_call, secondary_ws, spec, gid) if (fabric_call and gid) else None
                if shared:
                    continue
                note = "Write cannot be granted by sharing; give the group Contributor on the workspace" \
                    if "Write" in spec["rights"] else ""
                result["portal_steps"].append({
                    "item_type": spec["item_type"], "item_name": spec["item_name"],
                    "secondary_item_id": spec["secondary_item_id"],
                    "group_display_name": spec["group_display_name"], "group_id": gid,
                    "rights": spec["rights"], "additional": spec["additional"],
                    "verified": "no" if shared is False else "unknown (admin API unavailable)",
                    "note": note,
                })
        except Exception as e:
            result["failures"].append({"spec": label, "member": "", "error": str(e)[:300]})
            if logger:
                logger.error(f"[ACCESS GROUP FAILED] {label}: {e}")
    return result
