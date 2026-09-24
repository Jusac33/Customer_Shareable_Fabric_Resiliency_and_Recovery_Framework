"""
Item Permissions Accelerator  (UNSUPPORTED INTERNAL API)

=============================================================================
READ THIS BEFORE USING
=============================================================================
This module talks to the Power BI / Fabric *internal portal backend*, not the
public Fabric REST API:

    POST  {cluster}/metadata/access                 (grant)
    GET   {cluster}/metadata/access/artifacts/{id}  (read)

These endpoints are what the Fabric portal's own "Share" dialog calls. They are
NOT documented, NOT versioned, carry no deprecation policy, and Microsoft
Support will not assist with them. They can change or disappear without notice.

Fabric exposes no public API for item-level permissions (see
IMPLEMENTATION_GUIDE.md section 28.2), which is why this exists.

SANCTIONED USE: an operator-run accelerator that produces a reviewed plan and
applies it with a human verifying the result.

NOT SANCTIONED: wiring this into automated failover, or treating it as a DR
control the recovery plan silently depends on. It is deliberately not imported
by sync_permissions.py or failover.py.

=============================================================================
PAYLOAD CONTRACT IS UNVERIFIED
=============================================================================
The grant request body below is the community-reported shape. It has NOT been
verified against a captured request. Field names and the permission enum may
differ in your tenant.

Capture ground truth in ~2 minutes:
  1. Fabric portal -> F12 -> Network tab
  2. Share any test item with a user
  3. Filter for "access", select the POST
  4. Copy the request JSON

Then either edit DEFAULT_WRITE_CONTRACT or pass --contract-file <file.json>.
Run --verify-contract first; it performs reads only and reports what the
tenant actually returns.

Usage:
  python item_permissions_accelerator.py --verify-contract
  python item_permissions_accelerator.py --plan
  python item_permissions_accelerator.py --apply --i-understand-this-is-unsupported
"""

import argparse
import csv
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests

import common


# Resolved lazily; override with FABRIC_INTERNAL_CLUSTER to skip discovery.
CLUSTER_OVERRIDE = os.getenv("FABRIC_INTERNAL_CLUSTER", "").rstrip("/")

CLUSTER_DISCOVERY_URL = (
    "https://api.powerbi.com/powerbi/globalservice/v201606/clusterdetails"
)

# The portal has used more than one path for the read. Tried in order.
READ_PATH_VARIANTS = [
    "/metadata/access/artifacts/{item_id}",
    "/m/access/artifacts/{item_id}",
]

# UNVERIFIED - see module docstring. Override with --contract-file.
DEFAULT_WRITE_CONTRACT: Dict[str, Any] = {
    "path": "/metadata/access",
    "body_template": {
        "artifactObjectIds": ["{item_id}"],
        "permissions": [
            {
                "principalId": "{principal_id}",
                "principalType": "{principal_type}",
                "permissionType": "{role}",
                "grant": True,
            }
        ],
    },
    # Maps a Fabric role name -> whatever this endpoint calls it. Identity by
    # default; replace once you have seen a real request.
    "role_map": {},
    "verified": False,
}

PLAN_CSV = "data/item_permissions_plan.csv"
RAW_DUMP = "data/item_permissions_raw.json"


class InternalApiError(Exception):
    """Raised for any non-2xx from the internal backend."""

    def __init__(self, status_code: int, url: str, detail: Any):
        self.status_code = status_code
        self.url = url
        self.detail = detail
        super().__init__(f"Internal API error {status_code} for {url}: {detail}")


# ---------------------------------------------------------------------------
# Cluster resolution
# ---------------------------------------------------------------------------

def resolve_cluster(logger) -> str:
    """
    Resolve the tenant's backend cluster URI (e.g. https://wabi-us-east2-a-primary-redirect.analysis.windows.net).

    The internal endpoints live on the tenant's home cluster, not on
    api.powerbi.com, so this must be resolved before any call.
    """
    if CLUSTER_OVERRIDE:
        logger.info(f"Using cluster override: {CLUSTER_OVERRIDE}")
        return CLUSTER_OVERRIDE

    headers = common.get_powerbi_headers()
    resp = requests.get(CLUSTER_DISCOVERY_URL, headers=headers, timeout=30)

    if resp.status_code >= 400:
        raise InternalApiError(resp.status_code, CLUSTER_DISCOVERY_URL, resp.text)

    data = resp.json()
    uri = (
        data.get("clusterUrl")
        or data.get("backendUrl")
        or data.get("fixedClusterUri")
    )
    if not uri:
        raise InternalApiError(
            resp.status_code,
            CLUSTER_DISCOVERY_URL,
            f"No cluster URI in discovery response. Keys: {sorted(data.keys())}. "
            f"Set FABRIC_INTERNAL_CLUSTER to bypass discovery.",
        )

    uri = uri.rstrip("/")
    logger.info(f"Resolved backend cluster: {uri}")
    return uri


# ---------------------------------------------------------------------------
# Internal transport
# ---------------------------------------------------------------------------

def internal_call(
    method: str,
    cluster: str,
    path: str,
    logger,
    payload: Optional[Dict] = None,
    timeout: int = 30,
) -> Any:
    """
    Call the internal backend.

    Deliberately NOT silent: every failure raises with the full response body.
    A previous revision of this framework swallowed 404s from a nonexistent
    permissions endpoint at debug level and reported success, hiding the fact
    that nothing worked. Do not reintroduce that pattern here.
    """
    url = f"{cluster}{path}"
    headers = common.get_powerbi_headers()

    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, timeout=timeout)
    elif method.upper() == "POST":
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    else:
        raise ValueError(f"Unsupported method for internal API: {method}")

    if resp.status_code >= 400:
        detail = resp.text
        try:
            detail = resp.json()
        except ValueError:
            pass
        raise InternalApiError(resp.status_code, url, detail)

    if not (resp.content or b"").strip():
        return {}

    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text}


def read_item_access(cluster: str, item_id: str, logger) -> Tuple[Optional[Any], Optional[str]]:
    """
    Read current access for one item.

    Returns (payload, path_that_worked). Returns (None, None) when every known
    path variant fails, so the caller can distinguish "no permissions" from
    "could not read".
    """
    last_error = None
    for variant in READ_PATH_VARIANTS:
        path = variant.format(item_id=item_id)
        try:
            return internal_call("GET", cluster, path, logger), path
        except InternalApiError as e:
            last_error = e
            if e.status_code in (401, 403):
                raise  # auth problem, not a wrong path - surface immediately
            continue

    logger.warning(f"Could not read access for item {item_id}: {last_error}")
    return None, None


def extract_principals(access_payload: Any) -> List[Dict[str, Any]]:
    """
    Normalize an access payload into [{principal_id, principal_type, role}].

    The internal response shape is not contractual, so this probes the field
    names the portal has been observed to use and skips anything it cannot
    interpret rather than guessing.
    """
    if not access_payload:
        return []

    entries = None
    if isinstance(access_payload, list):
        entries = access_payload
    elif isinstance(access_payload, dict):
        for key in ("permissions", "accessDetails", "value", "entries", "artifactAccess"):
            if isinstance(access_payload.get(key), list):
                entries = access_payload[key]
                break

    if not entries:
        return []

    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        principal = e.get("principal") if isinstance(e.get("principal"), dict) else e
        pid = (
            principal.get("principalId")
            or principal.get("objectId")
            or principal.get("id")
            or principal.get("identifier")
        )
        ptype = (
            principal.get("principalType")
            or principal.get("type")
            or "User"
        )
        role = (
            e.get("permissionType")
            or e.get("role")
            or e.get("permission")
            or e.get("accessRight")
        )
        if pid and role:
            out.append(
                {
                    "principal_id": str(pid),
                    "principal_type": str(ptype),
                    "role": str(role),
                }
            )
    return out


# ---------------------------------------------------------------------------
# Write contract
# ---------------------------------------------------------------------------

def load_contract(contract_file: Optional[str], logger) -> Dict[str, Any]:
    if not contract_file:
        logger.warning(
            "Using DEFAULT_WRITE_CONTRACT, which is UNVERIFIED. Capture a real "
            "request from portal dev-tools and pass --contract-file to be safe."
        )
        return DEFAULT_WRITE_CONTRACT

    with open(contract_file, "r", encoding="utf-8") as f:
        contract = json.load(f)

    if "body_template" not in contract:
        raise ValueError(f"Contract file {contract_file} has no 'body_template' key")

    contract.setdefault("path", "/metadata/access")
    contract.setdefault("role_map", {})
    contract.setdefault("verified", True)
    logger.info(f"Loaded write contract from {contract_file}")
    return contract


def _substitute(node: Any, values: Dict[str, str]) -> Any:
    """Recursively replace {placeholder} tokens in a body template."""
    if isinstance(node, str):
        out = node
        for k, v in values.items():
            out = out.replace("{" + k + "}", v)
        return out
    if isinstance(node, list):
        return [_substitute(n, values) for n in node]
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    return node


def build_grant_payload(
    contract: Dict[str, Any],
    item_id: str,
    principal_id: str,
    principal_type: str,
    role: str,
) -> Dict[str, Any]:
    mapped_role = contract.get("role_map", {}).get(role, role)
    return _substitute(
        contract["body_template"],
        {
            "item_id": item_id,
            "principal_id": principal_id,
            "principal_type": principal_type,
            "role": mapped_role,
        },
    )


def grant_item_access(
    cluster: str,
    contract: Dict[str, Any],
    item_id: str,
    principal_id: str,
    principal_type: str,
    role: str,
    logger,
) -> Any:
    payload = build_grant_payload(contract, item_id, principal_id, principal_type, role)
    return internal_call("POST", cluster, contract["path"], logger, payload=payload)


# ---------------------------------------------------------------------------
# Plan / apply
# ---------------------------------------------------------------------------

def build_plan(
    cluster: str,
    primary_workspace_id: str,
    secondary_workspace_id: str,
    artifact_mapping: Dict[str, str],
    logger,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Diff item permissions primary vs secondary and return (plan_rows, stats).

    Read-only. Rows describe grants that WOULD be applied.
    """
    stats = {
        "items_examined": 0,
        "items_unmapped": 0,
        "items_unreadable": 0,
        "grants_planned": 0,
        "already_present": 0,
    }
    plan: List[Dict[str, Any]] = []
    raw_dump: Dict[str, Any] = {}

    primary_items = common.get_items(primary_workspace_id)
    logger.info(f"Examining {len(primary_items)} primary items")

    for item in primary_items:
        p_item_id = item["id"]
        s_item_id = artifact_mapping.get(p_item_id)

        if not s_item_id:
            stats["items_unmapped"] += 1
            continue

        stats["items_examined"] += 1

        p_access, p_path = read_item_access(cluster, p_item_id, logger)
        if p_access is None:
            stats["items_unreadable"] += 1
            continue

        raw_dump[p_item_id] = {"path": p_path, "payload": p_access}

        p_principals = extract_principals(p_access)
        if not p_principals:
            continue

        s_access, _ = read_item_access(cluster, s_item_id, logger)
        s_keys = {
            (x["principal_id"], x["role"]) for x in extract_principals(s_access)
        }

        for pr in p_principals:
            if (pr["principal_id"], pr["role"]) in s_keys:
                stats["already_present"] += 1
                continue
            plan.append(
                {
                    "item_name": item.get("displayName", ""),
                    "item_type": item.get("type", ""),
                    "primary_item_id": p_item_id,
                    "secondary_item_id": s_item_id,
                    "principal_id": pr["principal_id"],
                    "principal_type": pr["principal_type"],
                    "role": pr["role"],
                }
            )
            stats["grants_planned"] += 1

    common.save_json(raw_dump, RAW_DUMP)
    logger.info(f"Raw access payloads written to {RAW_DUMP}")
    return plan, stats


def write_plan_csv(plan: List[Dict[str, Any]], path: str, logger) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = [
        "item_name",
        "item_type",
        "primary_item_id",
        "secondary_item_id",
        "principal_id",
        "principal_type",
        "role",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(plan)
    logger.info(f"Plan written to {path} ({len(plan)} rows)")


def apply_plan(
    cluster: str,
    contract: Dict[str, Any],
    plan: List[Dict[str, Any]],
    secondary_workspace_id: str,
    logger,
) -> Dict[str, Any]:
    result = {"applied": 0, "failed": 0, "failures": []}

    for row in plan:
        try:
            grant_item_access(
                cluster,
                contract,
                row["secondary_item_id"],
                row["principal_id"],
                row["principal_type"],
                row["role"],
                logger,
            )
            result["applied"] += 1
            logger.info(
                f"[GRANT] {row['item_name']} -> {row['principal_id']} ({row['role']})"
            )
        except InternalApiError as e:
            result["failed"] += 1
            result["failures"].append(
                {
                    "item": row["item_name"],
                    "principal": row["principal_id"],
                    "status": e.status_code,
                    "detail": str(e.detail)[:500],
                }
            )
            logger.error(
                f"[GRANT FAILED] {row['item_name']} -> {row['principal_id']}: "
                f"HTTP {e.status_code} {str(e.detail)[:300]}"
            )
            if e.status_code == 400 and not contract.get("verified"):
                logger.error(
                    "HTTP 400 with an UNVERIFIED payload contract. This most "
                    "likely means the request body shape is wrong. Capture a "
                    "real request from portal dev-tools and rerun with "
                    "--contract-file."
                )
                break
            if e.status_code in (401, 403):
                logger.error(
                    "Authentication/authorization rejected. These internal "
                    "endpoints are built around interactive user sessions and "
                    "may not accept service principal tokens at all. Stopping."
                )
                break

    return result


def verify_contract(cluster: str, workspace_id: str, logger) -> int:
    """
    Read-only probe. Reports whether the internal endpoints are reachable with
    the current credentials and what shape they return. Writes nothing.
    """
    print("\n" + "=" * 70)
    print("CONTRACT VERIFICATION (read-only)")
    print("=" * 70)

    items = common.get_items(workspace_id)
    if not items:
        print("No items found in workspace - cannot probe.")
        return 1

    probed = 0
    for item in items[:5]:
        access, path = read_item_access(cluster, item["id"], logger)
        print(f"\nItem: {item.get('displayName')} ({item.get('type')})")
        print(f"  id:   {item['id']}")
        if access is None:
            print("  read: FAILED on all known path variants")
            continue
        probed += 1
        print(f"  read: OK via {path}")
        print(f"  raw:  {json.dumps(access)[:600]}")
        parsed = extract_principals(access)
        print(f"  parsed principals: {len(parsed)}")
        for p in parsed[:5]:
            print(f"    - {p['principal_id']} ({p['principal_type']}) : {p['role']}")

    print("\n" + "-" * 70)
    if probed == 0:
        print("RESULT: could not read any item. Either the paths have changed,")
        print("or the service principal cannot use these internal endpoints.")
        print("Try an interactive user token before investing further.")
        return 1

    print(f"RESULT: read path works ({probed}/5 items probed).")
    print("Compare 'raw' above against a captured portal request to confirm the")
    print("field names and permission values, then build a --contract-file for")
    print("the write side. Do not apply with an unverified contract.")
    print("-" * 70 + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Item-level permission accelerator (UNSUPPORTED internal API)",
    )
    parser.add_argument("--primary-workspace", default=common.PRIMARY_WORKSPACE_ID)
    parser.add_argument("--secondary-workspace", default=common.SECONDARY_WORKSPACE_ID)
    parser.add_argument(
        "--verify-contract",
        action="store_true",
        help="Read-only probe of the internal endpoints. Run this first.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Build and write the grant plan CSV. Read-only.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the plan. Requires --i-understand-this-is-unsupported.",
    )
    parser.add_argument(
        "--i-understand-this-is-unsupported",
        action="store_true",
        dest="ack",
        help="Required acknowledgement for --apply.",
    )
    parser.add_argument(
        "--contract-file",
        help="JSON file with the verified write contract captured from dev-tools.",
    )
    parser.add_argument("--plan-file", default=PLAN_CSV)
    args = parser.parse_args()

    logger = common.setup_logger("item_permissions_accelerator")

    if not any([args.verify_contract, args.plan, args.apply]):
        parser.error("Specify one of --verify-contract, --plan, or --apply")

    if args.apply and not args.ack:
        parser.error(
            "--apply requires --i-understand-this-is-unsupported. This calls an "
            "undocumented internal API that Microsoft does not support and may "
            "change without notice."
        )

    logger.warning(
        "This tool calls the Fabric/Power BI INTERNAL portal backend. It is "
        "unsupported, unversioned, and may break without notice. Operator-run "
        "accelerator only - do not wire into automated failover."
    )

    try:
        cluster = resolve_cluster(logger)
    except Exception as e:
        logger.error(f"Cluster resolution failed: {e}")
        logger.error("Set FABRIC_INTERNAL_CLUSTER to bypass discovery.")
        return 1

    if args.verify_contract:
        return verify_contract(cluster, args.primary_workspace, logger)

    artifact_mapping = common.load_artifact_mapping()
    if not artifact_mapping:
        logger.error("No artifact mapping found - cannot map primary to secondary items.")
        return 1

    plan, stats = build_plan(
        cluster,
        args.primary_workspace,
        args.secondary_workspace,
        artifact_mapping,
        logger,
    )
    write_plan_csv(plan, args.plan_file, logger)

    print("\n" + "=" * 70)
    print("ITEM PERMISSION PLAN")
    print("=" * 70)
    print(f"  Items examined:      {stats['items_examined']}")
    print(f"  Items unmapped:      {stats['items_unmapped']}")
    print(f"  Items unreadable:    {stats['items_unreadable']}")
    print(f"  Already present:     {stats['already_present']}")
    print(f"  Grants planned:      {stats['grants_planned']}")
    print(f"  Plan CSV:            {args.plan_file}")
    print("=" * 70 + "\n")

    if not args.apply:
        print("Read-only. Review the plan CSV, then rerun with --apply "
              "--i-understand-this-is-unsupported.\n")
        return 0

    contract = load_contract(args.contract_file, logger)
    if not contract.get("verified"):
        logger.warning(
            "Applying with an UNVERIFIED contract. Expect HTTP 400 if the body "
            "shape is wrong. Verify results in the portal afterwards."
        )

    apply_result = apply_plan(
        cluster, contract, plan, args.secondary_workspace, logger
    )

    print("\n" + "=" * 70)
    print("APPLY RESULT")
    print("=" * 70)
    print(f"  Applied:  {apply_result['applied']}")
    print(f"  Failed:   {apply_result['failed']}")
    for f in apply_result["failures"][:10]:
        print(f"    - {f['item']} / {f['principal']}: HTTP {f['status']}")
    print("=" * 70)
    print("\nVERIFY THESE GRANTS IN THE FABRIC PORTAL. This tool used an")
    print("unsupported API and its success responses are not authoritative.\n")

    return 0 if apply_result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
