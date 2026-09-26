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
  # review data/item_permissions_plan.csv; delete rows you do not want
  python item_permissions_accelerator.py --apply --i-understand-this-is-unsupported \
      --contract-file contract.json

--apply executes the reviewed CSV exactly as written. It does not re-plan.
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
# Live-tested 2026-09-26 with a user token: the first path returns data for
# Lakehouse, Warehouse and Notebook items; the second returned 404 everywhere.
# Report and SemanticModel items returned 404 on both.
READ_PATH_VARIANTS = [
    "/metadata/access/artifacts/{item_id}",
    "/m/access/artifacts/{item_id}",
]

# UNVERIFIED - see module docstring. Override with --contract-file.
# The read side is live-verified: permissions are integer bitmasks
# ("permissions", "artifactPermissions"), which {role} and
# {artifact_permissions} carry. The grant body shape itself is still a guess.
DEFAULT_WRITE_CONTRACT: Dict[str, Any] = {
    "path": "/metadata/access",
    "body_template": {
        "artifactObjectIds": ["{item_id}"],
        "permissions": [
            {
                "principalId": "{principal_id}",
                "principalType": "{principal_type}",
                "permissionType": "{role}",
                "artifactPermissions": "{artifact_permissions}",
                "grant": True,
            }
        ],
    },
    # Optional translation of the read-side value to what the grant expects.
    # Identity by default; replace once you have seen a real request.
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


def _principal_type_of(entry: Dict[str, Any], principal: Dict[str, Any]) -> str:
    """
    Resolve principal kind. Never defaults: guessing "User" for a group would
    grant to the wrong principal kind. Untyped entries are skipped by the planner.
    """
    explicit = principal.get("principalType") or principal.get("type")
    if explicit:
        return str(explicit)
    # Observed live shape (/metadata/access/artifacts): groups carry groupId,
    # users carry userType == 0. Anything else is left untyped on purpose.
    if entry.get("groupId"):
        return "Group"
    if entry.get("userType") == 0:
        return "User"
    return ""


def _is_inherited(entry: Dict[str, Any]) -> bool:
    """
    True when access comes from a workspace role, not a direct share.

    Replicating these as direct item grants would be wrong: workspace roles are
    already synced via roleAssignments, and a direct copy would outlive the
    role if it were later removed.
    """
    src = entry.get("accessSource")
    if not isinstance(src, dict):
        return False
    return src.get("folderRoleId") is not None or bool(src.get("folderRole"))


def parse_access(access_payload: Any) -> Dict[str, Any]:
    """
    Parse an access payload into direct shares, plus counts of entries that
    were inherited from workspace roles or could not be interpreted.

    Returns {"direct": [...], "inherited": int, "unparsed": int}. Each direct
    entry is {principal_id, principal_type, role, artifact_permissions}.

    "unparsed" is reported rather than silently dropped. A previous revision
    recognised none of the live response's entries and would have reported
    "0 grants planned" as if nothing needed syncing.
    """
    result = {"direct": [], "inherited": 0, "unparsed": 0}
    if not access_payload:
        return result

    entries = None
    live_shape = False
    if isinstance(access_payload, list):
        entries = access_payload
    elif isinstance(access_payload, dict):
        # "detail" is the shape observed live from /metadata/access/artifacts.
        for key in ("detail", "permissions", "accessDetails", "value", "entries", "artifactAccess"):
            if isinstance(access_payload.get(key), list):
                entries = access_payload[key]
                live_shape = key == "detail"
                break

    if not entries:
        return result

    for e in entries:
        if not isinstance(e, dict):
            result["unparsed"] += 1
            continue

        if _is_inherited(e):
            result["inherited"] += 1
            continue

        principal = e.get("principal") if isinstance(e.get("principal"), dict) else e
        if live_shape:
            # In the live shape "id" is the artifact's numeric id, not the
            # principal, so it must never be used as a fallback here.
            pid = principal.get("objectId")
        else:
            pid = (
                principal.get("principalId")
                or principal.get("objectId")
                or principal.get("id")
                or principal.get("identifier")
            )

        role = (
            e.get("permissionType")
            or e.get("role")
            or e.get("permission")
            or e.get("accessRight")
        )
        if role is None and e.get("permissions") is not None:
            # Live shape: integer permission bitmask, e.g. 1 or 327.
            role = e.get("permissions")

        if not pid or role is None or role == "":
            result["unparsed"] += 1
            continue

        artifact_perms = e.get("artifactPermissions")
        result["direct"].append(
            {
                "principal_id": str(pid),
                "principal_type": _principal_type_of(e, principal),
                "role": str(role),
                "artifact_permissions": "" if artifact_perms is None else str(artifact_perms),
            }
        )
    return result


def extract_principals(access_payload: Any) -> List[Dict[str, Any]]:
    """Direct (non-inherited) principals only. See parse_access for counts."""
    return parse_access(access_payload)["direct"]


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


def _coerce(value: Any) -> Any:
    """Digit strings (e.g. permission bitmasks read back from the CSV) -> int."""
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if value == "":
        return None
    return value


def _substitute(node: Any, values: Dict[str, Any]) -> Any:
    """
    Recursively replace {placeholder} tokens in a body template.

    A string that is exactly one placeholder (e.g. "{role}") is replaced by the
    typed value, so numeric bitmasks are sent as JSON numbers, not strings.
    Placeholders embedded in longer strings are replaced textually.
    """
    if isinstance(node, str):
        for k, v in values.items():
            if node == "{" + k + "}":
                return _coerce(v)
        out = node
        for k, v in values.items():
            out = out.replace("{" + k + "}", "" if v is None else str(v))
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
    artifact_permissions: str = "",
) -> Dict[str, Any]:
    mapped_role = contract.get("role_map", {}).get(role, role)
    return _substitute(
        contract["body_template"],
        {
            "item_id": item_id,
            "principal_id": principal_id,
            "principal_type": principal_type,
            "role": mapped_role,
            "artifact_permissions": artifact_permissions,
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
    artifact_permissions: str = "",
) -> Any:
    payload = build_grant_payload(
        contract, item_id, principal_id, principal_type, role, artifact_permissions
    )
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
        "secondary_unreadable": 0,
        "principals_untyped": 0,
        "inherited_skipped": 0,
        "entries_unparsed": 0,
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

        p_parsed = parse_access(p_access)
        stats["inherited_skipped"] += p_parsed["inherited"]
        if p_parsed["unparsed"]:
            stats["entries_unparsed"] += p_parsed["unparsed"]
            logger.warning(
                f"{item.get('displayName')}: {p_parsed['unparsed']} access entries "
                f"could not be interpreted - see {RAW_DUMP}"
            )
        p_principals = p_parsed["direct"]
        if not p_principals:
            continue

        s_access, _ = read_item_access(cluster, s_item_id, logger)
        if s_access is None:
            # Unknown secondary state: planning here would treat every primary
            # principal as missing and propose duplicate grants.
            stats["secondary_unreadable"] += 1
            logger.warning(
                f"Skipping {item.get('displayName')}: secondary item {s_item_id} unreadable"
            )
            continue

        s_keys = {
            (x["principal_id"], x["role"], x["artifact_permissions"])
            for x in extract_principals(s_access)
        }

        for pr in p_principals:
            if not pr["principal_type"]:
                stats["principals_untyped"] += 1
                logger.warning(
                    f"Skipping principal {pr['principal_id']} on "
                    f"{item.get('displayName')}: response gave no principal type"
                )
                continue
            if (pr["principal_id"], pr["role"], pr["artifact_permissions"]) in s_keys:
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
                    "artifact_permissions": pr["artifact_permissions"],
                }
            )
            stats["grants_planned"] += 1

    common.save_json(raw_dump, RAW_DUMP)
    logger.info(f"Raw access payloads written to {RAW_DUMP}")
    return plan, stats


PLAN_FIELDS = [
    "item_name",
    "item_type",
    "primary_item_id",
    "secondary_item_id",
    "principal_id",
    "principal_type",
    "role",
    "artifact_permissions",
]

REQUIRED_PLAN_FIELDS = ("secondary_item_id", "principal_id", "principal_type", "role")


def write_plan_csv(plan: List[Dict[str, Any]], path: str, logger) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_FIELDS)
        w.writeheader()
        w.writerows(plan)
    logger.info(f"Plan written to {path} ({len(plan)} rows)")


def load_plan_csv(path: str) -> List[Dict[str, Any]]:
    """
    Load the operator-reviewed plan. --apply executes exactly these rows, so a
    reviewer can delete rows they do not want applied.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Plan file {path} not found. Run --plan first and review it."
        )

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_PLAN_FIELDS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"Plan file {path} is missing columns: {missing}")
        rows = list(reader)

    for i, row in enumerate(rows, start=2):  # row 1 is the header
        blank = [c for c in REQUIRED_PLAN_FIELDS if not (row.get(c) or "").strip()]
        if blank:
            raise ValueError(f"Plan file {path} line {i} has blank fields: {blank}")
    return rows


def apply_plan(
    cluster: str,
    contract: Dict[str, Any],
    plan: List[Dict[str, Any]],
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
                artifact_permissions=row.get("artifact_permissions", ""),
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
    sample = items[:5]
    for item in sample:
        access, path = read_item_access(cluster, item["id"], logger)
        print(f"\nItem: {item.get('displayName')} ({item.get('type')})")
        print(f"  id:   {item['id']}")
        if access is None:
            print("  read: FAILED on all known path variants")
            continue
        probed += 1
        print(f"  read: OK via {path}")
        parsed = parse_access(access)
        print(
            f"  direct shares: {len(parsed['direct'])}  "
            f"inherited from workspace roles: {parsed['inherited']}  "
            f"unparsed: {parsed['unparsed']}"
        )
        for p in parsed["direct"][:5]:
            print(
                f"    - {p['principal_id']} ({p['principal_type'] or 'UNTYPED'}) "
                f": permissions={p['role']} artifactPermissions={p['artifact_permissions'] or '-'}"
            )
        if parsed["unparsed"]:
            print(f"  raw:  {json.dumps(access)[:600]}")

    print("\n" + "-" * 70)
    if probed == 0:
        print("RESULT: could not read any item. Either the paths have changed,")
        print("or the service principal cannot use these internal endpoints.")
        print("Try an interactive user token before investing further.")
        return 1

    print(f"RESULT: read path works ({probed}/{len(sample)} items probed).")
    print("Items that failed to read (e.g. Reports, semantic models) are not")
    print("supported by this endpoint and must be handled manually.")
    print("The WRITE side is still unverified: capture a real grant request from")
    print("portal dev-tools and pass it as --contract-file before --apply.")
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
        help=(
            "Apply the reviewed plan CSV exactly as written (does NOT re-plan). "
            "Requires --i-understand-this-is-unsupported."
        ),
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

    if sum([args.verify_contract, args.plan, args.apply]) > 1:
        parser.error(
            "--verify-contract, --plan, and --apply are separate steps. Run "
            "--plan, review the CSV, then run --apply on its own."
        )

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

    try:
        if args.verify_contract:
            return verify_contract(cluster, args.primary_workspace, logger)
        if args.plan:
            return _run_plan(cluster, args, logger)
        return _run_apply(cluster, args, logger)
    except InternalApiError as e:
        logger.error(f"Internal API rejected the request: {e}")
        if e.status_code in (401, 403):
            logger.error(
                "These endpoints may not accept service principal tokens. "
                "Nothing further was attempted."
            )
        return 1


def _run_plan(cluster: str, args, logger) -> int:
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
    print(f"  Items examined:        {stats['items_examined']}")
    print(f"  Items unmapped:        {stats['items_unmapped']}")
    print(f"  Primary unreadable:    {stats['items_unreadable']}")
    print(f"  Secondary unreadable:  {stats['secondary_unreadable']}")
    print(f"  Principals untyped:    {stats['principals_untyped']}")
    print(f"  Entries unparsed:      {stats['entries_unparsed']}")
    print(f"  Inherited (skipped):   {stats['inherited_skipped']}  (from workspace roles; synced separately)")
    print(f"  Already present:       {stats['already_present']}")
    print(f"  Grants planned:        {stats['grants_planned']}")
    print(f"  Plan CSV:              {args.plan_file}")
    print("=" * 70 + "\n")

    incomplete = (
        stats["items_unreadable"]
        + stats["secondary_unreadable"]
        + stats["principals_untyped"]
        + stats["entries_unparsed"]
    )
    if incomplete:
        print(f"WARNING: {incomplete} item(s)/principal(s) could not be evaluated and")
        print("are NOT in the plan. Handle them manually in the portal.\n")

    print("Review the plan CSV (delete any rows you do not want), then run:")
    print("  --apply --i-understand-this-is-unsupported --contract-file <file>\n")
    return 1 if incomplete else 0


def _run_apply(cluster: str, args, logger) -> int:
    try:
        plan = load_plan_csv(args.plan_file)
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        return 1

    if not plan:
        logger.info(f"Plan file {args.plan_file} has no rows - nothing to apply.")
        return 0

    contract = load_contract(args.contract_file, logger)
    if not contract.get("verified"):
        logger.warning(
            "Applying with an UNVERIFIED contract. Expect HTTP 400 if the body "
            "shape is wrong. Verify results in the portal afterwards."
        )

    logger.info(f"Applying {len(plan)} reviewed rows from {args.plan_file}")
    apply_result = apply_plan(cluster, contract, plan, logger)

    not_attempted = len(plan) - apply_result["applied"] - apply_result["failed"]

    print("\n" + "=" * 70)
    print("APPLY RESULT")
    print("=" * 70)
    print(f"  Applied:        {apply_result['applied']}")
    print(f"  Failed:         {apply_result['failed']}")
    print(f"  Not attempted:  {not_attempted}")
    for f in apply_result["failures"][:10]:
        print(f"    - {f['item']} / {f['principal']}: HTTP {f['status']}")
    print("=" * 70)
    print("\nVERIFY THESE GRANTS IN THE FABRIC PORTAL. This tool used an")
    print("unsupported API and its success responses are not authoritative.\n")

    return 0 if apply_result["failed"] == 0 and not_attempted == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
