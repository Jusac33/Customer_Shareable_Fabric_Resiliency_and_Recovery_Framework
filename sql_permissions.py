"""
Granular SQL permission replication for Fabric Warehouses and Lakehouse SQL
analytics endpoints, using T-SQL only (supported, documented).

Item sharing decides who can CONNECT to a Warehouse / SQL endpoint; inside
it, object-level access is governed by T-SQL GRANT / DENY, custom database
roles and role membership. None of that lives in the item definition, so a
secondary workspace starts with none of it. This module reads it from the
primary through the catalog views and replays the delta on the secondary:

  * CREATE ROLE                      custom database roles
  * ALTER ROLE ... ADD MEMBER        role membership
  * GRANT / DENY [WITH GRANT OPTION] database, schema, object and column scope

Never revokes: grants found only on the secondary are reported, not removed.
Item-derived CONNECT is skipped (it comes from item sharing / workspace roles).
RLS security policies and dynamic data masking are DDL and travel with the
warehouse schema, not here.
"""

import struct
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

SKIP_PRINCIPALS = {"dbo", "public", "guest", "sys", "information_schema"}

PERMISSIONS_SQL = """
SELECT pe.state_desc, pe.permission_name, pe.class_desc,
       sch.name AS schema_name, obj.name AS object_name, col.name AS column_name,
       pr.name AS principal_name, pr.type_desc AS principal_type
FROM sys.database_permissions AS pe
JOIN sys.database_principals AS pr ON pr.principal_id = pe.grantee_principal_id
LEFT JOIN sys.objects AS obj ON pe.class = 1 AND obj.object_id = pe.major_id
LEFT JOIN sys.schemas AS sch ON sch.schema_id =
     CASE WHEN pe.class = 1 THEN obj.schema_id WHEN pe.class = 3 THEN pe.major_id END
LEFT JOIN sys.columns AS col ON pe.class = 1 AND pe.minor_id > 0
     AND col.object_id = pe.major_id AND col.column_id = pe.minor_id
WHERE pe.class IN (0, 1, 3)
"""

ROLES_SQL = """
SELECT name FROM sys.database_principals
WHERE type = 'R' AND is_fixed_role = 0 AND name <> 'public'
"""

ROLE_MEMBERS_SQL = """
SELECT r.name AS role_name, m.name AS member_name, m.type_desc AS member_type
FROM sys.database_role_members AS rm
JOIN sys.database_principals AS r ON r.principal_id = rm.role_principal_id
JOIN sys.database_principals AS m ON m.principal_id = rm.member_principal_id
"""

OBJECTS_SQL = """
SELECT s.name AS schema_name, o.name AS object_name
FROM sys.objects AS o JOIN sys.schemas AS s ON s.schema_id = o.schema_id
WHERE o.is_ms_shipped = 0
"""

SCHEMAS_SQL = "SELECT name FROM sys.schemas"

# (state, permission, class, schema, object, column, principal)
Perm = Tuple[str, str, str, str, str, str, str]


def q(name: str) -> str:
    """Quote a T-SQL identifier."""
    return "[" + (name or "").replace("]", "]]") + "]"


def _skip(principal: str) -> bool:
    return (principal or "").lower() in SKIP_PRINCIPALS or (principal or "").startswith("##")


def read_snapshot(run_query: Callable[[str], List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Catalog snapshot of one database (read-only)."""
    perms: Set[Perm] = set()
    for r in run_query(PERMISSIONS_SQL):
        principal = r.get("principal_name") or ""
        if _skip(principal):
            continue
        cls = r.get("class_desc") or ""
        perm = r.get("permission_name") or ""
        if cls == "DATABASE" and perm == "CONNECT":
            continue
        perms.add((r.get("state_desc") or "", perm, cls, r.get("schema_name") or "",
                   r.get("object_name") or "", r.get("column_name") or "", principal))
    roles = {r["name"] for r in run_query(ROLES_SQL)}
    members = {(r["role_name"], r["member_name"]) for r in run_query(ROLE_MEMBERS_SQL)
               if not _skip(r["member_name"])}
    objects = {(r["schema_name"], r["object_name"]) for r in run_query(OBJECTS_SQL)}
    schemas = {r["name"] for r in run_query(SCHEMAS_SQL)}
    return {"perms": perms, "roles": roles, "members": members, "objects": objects, "schemas": schemas}


def statement_for(p: Perm) -> str:
    state, perm, cls, schema, obj, col, principal = p
    verb = "DENY" if state == "DENY" else "GRANT"
    suffix = " WITH GRANT OPTION" if state == "GRANT_WITH_GRANT_OPTION" else ""
    if cls == "DATABASE":
        target = ""
    elif cls == "SCHEMA":
        target = f" ON SCHEMA::{q(schema)}"
    else:
        target = f" ON {q(schema)}.{q(obj)}" + (f" ({q(col)})" if col else "")
    return f"{verb} {perm}{target} TO {q(principal)}{suffix};"


def _scope_order(p: Perm) -> Tuple:
    """Database, schema, object, then column scope.

    An object-level GRANT/REVOKE clears column-level permissions on that
    object, so column-level statements must run after object-level ones or a
    column DENY is silently lost (caught by the live test on Fabric).
    """
    _, _, cls, schema, obj, col, principal = p
    rank = {"DATABASE": 0, "SCHEMA": 1}.get(cls, 3 if col else 2)
    return (rank,) + p


def plan_sql_permissions(primary: Dict[str, Any], secondary: Dict[str, Any]) -> Dict[str, Any]:
    """Statements that bring secondary up to primary. Pure (unit-tested).

    Order matters: roles before membership before grants (grants may target roles).
    """
    statements: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []

    for role in sorted(primary["roles"] - secondary["roles"]):
        statements.append({"kind": "create_role", "statement": f"CREATE ROLE {q(role)};", "note": ""})
    for role, member in sorted(primary["members"] - secondary["members"]):
        statements.append({"kind": "role_member",
                           "statement": f"ALTER ROLE {q(role)} ADD MEMBER {q(member)};", "note": ""})
    for p in sorted(primary["perms"] - secondary["perms"], key=_scope_order):
        _, _, cls, schema, obj, _, _ = p
        stmt = statement_for(p)
        if cls == "SCHEMA" and schema not in secondary["schemas"]:
            skipped.append({"kind": "skipped", "statement": stmt,
                            "note": f"schema {schema} not in secondary yet (sync schema/data first)"})
        elif cls == "OBJECT_OR_COLUMN" and (schema, obj) not in secondary["objects"]:
            skipped.append({"kind": "skipped", "statement": stmt,
                            "note": f"object {schema}.{obj} not in secondary yet (sync schema/data first)"})
        else:
            statements.append({"kind": "grant", "statement": stmt, "note": ""})

    extra = [statement_for(p) for p in sorted(secondary["perms"] - primary["perms"])]
    extra += [f"ALTER ROLE {q(r)} ADD MEMBER {q(m)};" for r, m in sorted(secondary["members"] - primary["members"])]
    return {"statements": statements, "skipped": skipped, "extra_on_secondary": extra}


def sql_targets(fabric_call: Callable, workspace_id: str, items: List[Dict[str, Any]],
                host_from: Callable[[str], str]) -> Dict[Tuple[str, str], Dict[str, str]]:
    """(type, name) -> {host, database} for Warehouses and Lakehouse SQL endpoints."""
    out: Dict[Tuple[str, str], Dict[str, str]] = {}
    for item in items:
        kind = {"Lakehouse": "lakehouses", "Warehouse": "warehouses"}.get(item.get("type"))
        if not kind or item.get("displayName", "").startswith("BCDR_"):
            continue
        props = fabric_call("GET", f"/workspaces/{workspace_id}/{kind}/{item['id']}").get("properties", {})
        conn = (props.get("sqlEndpointProperties") or {}).get("connectionString") if kind == "lakehouses" \
            else props.get("connectionString")
        host = host_from(conn or "")
        if host:
            out[(item["type"], item["displayName"])] = {"host": host, "database": item["displayName"]}
    return out


def connect(host: str, database: str, token: str):
    """pyodbc connection to a Fabric SQL endpoint with an Entra access token."""
    import pyodbc
    raw = token.encode("UTF-16-LE")
    token_struct = struct.pack(f"<I{len(raw)}s", len(raw), raw)
    drivers = pyodbc.drivers()
    driver = next((d for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server") if d in drivers),
                  "SQL Server")
    conn = pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={host},1433;DATABASE={database};Encrypt=Yes;TrustServerCertificate=No;",
        attrs_before={1256: token_struct}, timeout=60,
    )
    conn.autocommit = True
    return conn


def query_runner(conn) -> Callable[[str], List[Dict[str, Any]]]:
    def run(sql: str) -> List[Dict[str, Any]]:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    return run


def apply_statements(execute: Callable[[str], Any], statements: List[Dict[str, str]], logger=None) -> Dict[str, Any]:
    """Run each statement; one failure never hides another."""
    result = {"applied": 0, "failed": 0, "failures": []}
    for s in statements:
        if s.get("kind") == "skipped":
            continue
        try:
            execute(s["statement"])
            result["applied"] += 1
            if logger:
                logger.info(f"[SQL] {s['statement']}")
        except Exception as e:
            result["failed"] += 1
            result["failures"].append({"statement": s["statement"], "error": str(e)[:300]})
            if logger:
                logger.error(f"[SQL FAILED] {s['statement']}: {e}")
    return result


def build_sql_plan(fabric_call: Callable, host_from: Callable[[str], str], token_provider: Callable[[], str],
                   primary_ws: str, secondary_ws: str,
                   primary_items: List[Dict[str, Any]], secondary_items: List[Dict[str, Any]],
                   connector: Callable = connect, logger=None) -> Dict[str, Any]:
    """Per matched Warehouse / SQL endpoint: plan rows. Read-only."""
    p_targets = sql_targets(fabric_call, primary_ws, primary_items, host_from)
    s_targets = sql_targets(fabric_call, secondary_ws, secondary_items, host_from)
    token = token_provider()
    rows: List[Dict[str, Any]] = []
    coverage = {"databases_scanned": 0, "unmatched_in_secondary": 0, "errors": [], "extra_on_secondary": 0}
    for key, p in sorted(p_targets.items()):
        s = s_targets.get(key)
        if not s:
            coverage["unmatched_in_secondary"] += 1
            continue
        try:
            pc, sc = connector(p["host"], p["database"], token), connector(s["host"], s["database"], token)
            try:
                plan = plan_sql_permissions(read_snapshot(query_runner(pc)), read_snapshot(query_runner(sc)))
            finally:
                pc.close()
                sc.close()
        except Exception as e:
            coverage["errors"].append(f"{key[0]}:{key[1]}: {str(e)[:200]}")
            if logger:
                logger.error(f"SQL permission read failed for {key[1]}: {e}")
            continue
        coverage["databases_scanned"] += 1
        coverage["extra_on_secondary"] += len(plan["extra_on_secondary"])
        for st in plan["statements"] + plan["skipped"]:
            rows.append({"item_type": key[0], "item_name": key[1],
                         "secondary_host": s["host"], "database": s["database"], **st})
    return {"rows": rows, "coverage": coverage}
