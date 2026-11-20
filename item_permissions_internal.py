"""
OPT-IN, UNSUPPORTED: item-share replication through the Fabric portal's
internal sharing endpoints.

    GET  {cluster}/metadata/access/artifacts/{itemId}   read direct + inherited access
    POST {cluster}/metadata/access                      grant (the portal Share dialog)

These endpoints are what the Fabric portal itself calls. They are not
documented, versioned or supported: Microsoft can change them without notice
and Microsoft Support will not help with them. Use them only when the
supported path (item_permissions.py: dataset users API + access-group
bootstrap) is not enough, and keep a human in the loop.

Guard rails:
  * Disabled unless BCDR_ENABLE_INTERNAL_SHARING_API=true (or the dashboard
    opt-in switch is on), and every apply needs an explicit acknowledgement.
  * Plans are read-only. Apply sends exactly the reviewed rows.
  * Every grant is read back; a grant that doesn't appear is reported failed.
  * Never revokes or downgrades. Extra secondary shares are only reported.
  * Workspace-role (inherited) access is skipped; roles replicate separately.

Request shape captured from the portal's Share dialog on 2026-11-20 (MSIT):
    {"dashboards": [], "reports": [], "workbooks": [], "models": [], "datamarts": [],
     "artifacts": [{"id": <numeric item id>, "artifactObjectId": "<item guid>",
                    "userId": null, "groupId": null,
                    "permissions": <int>, "artifactPermissions": <int>,
                    "userObjectId": "<guid>" | null, "groupObjectId": "<guid>" | null,
                    "isServicePrincipal": <bool>}]}
Sharing a lakehouse sends two artifacts: the lakehouse and its SQL analytics
endpoint. Both are separate items here and are replicated independently, so
the permission integers are copied verbatim per item type and never decoded.
"""

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

ENABLE_ENV = "BCDR_ENABLE_INTERNAL_SHARING_API"
ACK_PHRASE = "I understand this uses an unsupported internal API"
CLUSTER_DISCOVERY_URL = "https://api.powerbi.com/powerbi/globalservice/v201606/clusterdetails"
READ_PATH = "/metadata/access/artifacts/{item_id}"
GRANT_PATH = "/metadata/access"

ACTION_GRANT = "grant"
ACTION_UPGRADE = "upgrade"
ACTION_PRESENT = "present"


class InternalApiError(Exception):
    def __init__(self, status: int, url: str, detail: str):
        self.status_code = status
        super().__init__(f"Internal API {status} for {url}: {detail[:300]}")


def enabled(settings: Optional[Dict[str, Any]] = None) -> bool:
    if os.getenv(ENABLE_ENV, "").lower() in ("1", "true", "yes"):
        return True
    return bool((settings or {}).get("internal_sharing_api_enabled"))


class InternalSharingClient:
    """Thin transport. headers() must return a Power BI-audience bearer header (user token)."""

    def __init__(self, headers: Callable[[], Dict[str, str]], cluster: Optional[str] = None,
                 session: Optional[requests.Session] = None):
        self.headers = headers
        self.session = session or requests.Session()
        self.cluster = (cluster or os.getenv("FABRIC_INTERNAL_CLUSTER", "")).rstrip("/")

    def _cluster(self) -> str:
        if not self.cluster:
            r = self.session.get(CLUSTER_DISCOVERY_URL, headers=self.headers(), timeout=30)
            if r.status_code >= 400:
                raise InternalApiError(r.status_code, CLUSTER_DISCOVERY_URL, r.text)
            data = r.json()
            uri = data.get("clusterUrl") or data.get("backendUrl") or data.get("fixedClusterUri")
            if not uri:
                raise InternalApiError(200, CLUSTER_DISCOVERY_URL, f"no cluster in {sorted(data)}")
            self.cluster = uri.rstrip("/")
        return self.cluster

    def read(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Access payload, or None when the item type isn't served by this endpoint (404)."""
        url = self._cluster() + READ_PATH.format(item_id=item_id)
        r = self.session.get(url, headers=self.headers(), timeout=30)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise InternalApiError(r.status_code, url, r.text)
        return r.json()

    def grant(self, body: Dict[str, Any]) -> None:
        url = self._cluster() + GRANT_PATH
        r = self.session.post(url, json=body, headers={**self.headers(), "Content-Type": "application/json"},
                              timeout=60)
        if r.status_code >= 400:
            raise InternalApiError(r.status_code, url, r.text)


def principal_kind(entry: Dict[str, Any]) -> str:
    if entry.get("groupId") or entry.get("groupObjectId"):
        return "Group"
    if entry.get("aadAppId") or entry.get("isServicePrincipal"):
        return "ServicePrincipal"
    if entry.get("userPrincipalName") or entry.get("userType") == 0 or entry.get("emailAddress"):
        return "User"
    return ""


def is_inherited(entry: Dict[str, Any]) -> bool:
    src = entry.get("accessSource")
    return isinstance(src, dict) and (src.get("folderRoleId") is not None or bool(src.get("folderRole")))


def parse(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """{numeric_id, direct: [{object_id, kind, permissions, artifact_permissions, label}], inherited, unparsed}."""
    out = {"numeric_id": None, "direct": [], "inherited": 0, "unparsed": 0}
    if not payload:
        return out
    out["numeric_id"] = payload.get("id")
    for e in payload.get("detail") or []:
        if not isinstance(e, dict):
            out["unparsed"] += 1
            continue
        if is_inherited(e):
            out["inherited"] += 1
            continue
        oid, kind = e.get("objectId"), principal_kind(e)
        if not oid or not kind or e.get("permissions") is None:
            out["unparsed"] += 1
            continue
        out["direct"].append({
            "object_id": str(oid).lower(), "kind": kind,
            "permissions": int(e.get("permissions") or 0),
            "artifact_permissions": int(e.get("artifactPermissions") or 0),
            "label": e.get("userPrincipalName") or e.get("aadAppId") or e.get("displayName") or "",
        })
    return out


def _covers(have: Dict[str, int], want: Dict[str, int]) -> bool:
    return (have["permissions"] & want["permissions"]) == want["permissions"] and \
        (have["artifact_permissions"] & want["artifact_permissions"]) == want["artifact_permissions"]


def plan_item(item: Dict[str, Any], s_item: Dict[str, Any], primary: Dict[str, Any],
              secondary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rows for one matched item pair. Pure (unit-tested)."""
    have = {d["object_id"]: d for d in secondary["direct"]}
    rows = []
    for d in primary["direct"]:
        base = {"item_type": item["type"], "item_name": item["displayName"],
                "primary_item_id": item["id"], "secondary_item_id": s_item["id"],
                "secondary_numeric_id": secondary["numeric_id"], "principal_object_id": d["object_id"],
                "principal_kind": d["kind"], "principal_label": d["label"],
                "permissions": d["permissions"], "artifact_permissions": d["artifact_permissions"]}
        cur = have.get(d["object_id"])
        if cur and _covers(cur, d):
            rows.append({**base, "action": ACTION_PRESENT})
        elif cur:
            # Grant the union so nothing the secondary already has is downgraded.
            rows.append({**base, "action": ACTION_UPGRADE,
                         "permissions": cur["permissions"] | d["permissions"],
                         "artifact_permissions": cur["artifact_permissions"] | d["artifact_permissions"]})
        else:
            rows.append({**base, "action": ACTION_GRANT})
    return rows


def grant_body(row: Dict[str, Any]) -> Dict[str, Any]:
    group = row["principal_kind"] == "Group"
    return {
        "dashboards": [], "reports": [], "workbooks": [], "models": [], "datamarts": [],
        "artifacts": [{
            "id": row.get("secondary_numeric_id"),
            "artifactObjectId": row["secondary_item_id"],
            "userId": None, "groupId": None,
            "permissions": int(row["permissions"]),
            "artifactPermissions": int(row["artifact_permissions"]),
            "userObjectId": None if group else row["principal_object_id"],
            "groupObjectId": row["principal_object_id"] if group else None,
            "isServicePrincipal": row["principal_kind"] == "ServicePrincipal",
        }],
    }


def build_plan(client: InternalSharingClient, primary_items: List[Dict[str, Any]],
               secondary_items: List[Dict[str, Any]], logger=None) -> Dict[str, Any]:
    """Read-only. Matches items by type + name (SQL endpoints included)."""
    s_by = {(i["type"], i["displayName"]): i for i in secondary_items}
    rows: List[Dict[str, Any]] = []
    cov = {"items_read": 0, "not_served": {}, "unmatched": 0, "unparsed": 0, "errors": []}
    for item in primary_items:
        if item.get("displayName", "").startswith("BCDR_"):
            continue
        s_item = s_by.get((item["type"], item["displayName"]))
        if not s_item:
            cov["unmatched"] += 1
            continue
        try:
            p_raw = client.read(item["id"])
            if p_raw is None:
                cov["not_served"][item["type"]] = cov["not_served"].get(item["type"], 0) + 1
                continue
            p = parse(p_raw)
            cov["items_read"] += 1
            cov["unparsed"] += p["unparsed"]
            if not p["direct"]:
                continue
            s_raw = client.read(s_item["id"])
            if s_raw is None:
                cov["errors"].append(f"{item['displayName']}: secondary not readable")
                continue
            rows.extend(plan_item(item, s_item, p, parse(s_raw)))
        except Exception as e:
            cov["errors"].append(f"{item['type']}:{item['displayName']}: {str(e)[:200]}")
            if logger:
                logger.error(f"Internal access read failed for {item['displayName']}: {e}")
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {"rows": rows, "counts": counts, "coverage": cov}


def apply_rows(client: InternalSharingClient, rows: List[Dict[str, Any]], acknowledgement: str,
               settings: Optional[Dict[str, Any]] = None, logger=None) -> Dict[str, Any]:
    """Grant reviewed rows, then read each back. Refuses unless opted in and acknowledged."""
    if not enabled(settings):
        raise PermissionError(f"Internal sharing API is disabled. Set {ENABLE_ENV}=true or enable it in Settings.")
    if (acknowledgement or "").strip() != ACK_PHRASE:
        raise PermissionError(f"Acknowledgement required: '{ACK_PHRASE}'")
    res = {"applied": 0, "verified": 0, "failed": 0, "failures": []}
    for r in rows:
        if r.get("action") not in (ACTION_GRANT, ACTION_UPGRADE):
            continue
        label = f"{r['item_type']}:{r['item_name']} -> {r['principal_kind']} {r['principal_object_id']}"
        try:
            row = dict(r)
            if not row.get("secondary_numeric_id"):
                row["secondary_numeric_id"] = parse(client.read(row["secondary_item_id"]))["numeric_id"]
            client.grant(grant_body(row))
            res["applied"] += 1
            back = parse(client.read(row["secondary_item_id"]))
            got = next((d for d in back["direct"] if d["object_id"] == row["principal_object_id"]), None)
            if got and _covers(got, {"permissions": int(row["permissions"]),
                                     "artifact_permissions": int(row["artifact_permissions"])}):
                res["verified"] += 1
                if logger:
                    logger.info(f"[INTERNAL GRANT VERIFIED] {label}")
            else:
                res["failed"] += 1
                res["failures"].append({"row": label, "error": "grant accepted but not visible on read-back"})
        except Exception as e:
            res["failed"] += 1
            res["failures"].append({"row": label, "error": str(e)[:300]})
            if logger:
                logger.error(f"[INTERNAL GRANT FAILED] {label}: {e}")
            if getattr(e, "status_code", None) in (401, 403):
                break
    return res
