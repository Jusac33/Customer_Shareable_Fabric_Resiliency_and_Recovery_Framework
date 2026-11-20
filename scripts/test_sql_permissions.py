"""Tests for sql_permissions.py (T-SQL permission replication)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sql_permissions as sp


def runner(perms=(), roles=(), members=(), objects=(("dbo", "claims"),), schemas=("dbo", "sales")):
    data = {
        sp.PERMISSIONS_SQL: [dict(zip(("state_desc", "permission_name", "class_desc", "schema_name",
                                       "object_name", "column_name", "principal_name", "principal_type"), p))
                             for p in perms],
        sp.ROLES_SQL: [{"name": r} for r in roles],
        sp.ROLE_MEMBERS_SQL: [{"role_name": r, "member_name": m, "member_type": "EXTERNAL_USER"} for r, m in members],
        sp.OBJECTS_SQL: [{"schema_name": s, "object_name": o} for s, o in objects],
        sp.SCHEMAS_SQL: [{"name": s} for s in schemas],
    }
    return lambda sql: data[sql]


SEL = ("GRANT", "SELECT", "OBJECT_OR_COLUMN", "dbo", "claims", None, "analyst@x.com", "EXTERNAL_USER")


class TestSnapshot(unittest.TestCase):
    def test_system_principals_and_item_connect_are_skipped(self):
        snap = sp.read_snapshot(runner(perms=[
            SEL,
            ("GRANT", "CONNECT", "DATABASE", None, None, None, "analyst@x.com", "EXTERNAL_USER"),
            ("GRANT", "SELECT", "SCHEMA", "dbo", None, None, "public", "DATABASE_ROLE"),
            ("GRANT", "VIEW DEFINITION", "DATABASE", None, None, None, "dbo", "SQL_USER"),
        ], members=[("db_datareader", "dbo"), ("claims_readers", "analyst@x.com")]))
        self.assertEqual(snap["perms"], {("GRANT", "SELECT", "OBJECT_OR_COLUMN", "dbo", "claims", "", "analyst@x.com")})
        self.assertEqual(snap["members"], {("claims_readers", "analyst@x.com")})


class TestStatements(unittest.TestCase):
    def test_scopes_deny_grant_option_and_quoting(self):
        self.assertEqual(sp.statement_for(("GRANT", "SELECT", "OBJECT_OR_COLUMN", "dbo", "claims", "", "a@x.com")),
                         "GRANT SELECT ON [dbo].[claims] TO [a@x.com];")
        self.assertEqual(sp.statement_for(("DENY", "SELECT", "OBJECT_OR_COLUMN", "dbo", "claims", "ssn", "r")),
                         "DENY SELECT ON [dbo].[claims] ([ssn]) TO [r];")
        self.assertEqual(sp.statement_for(("GRANT_WITH_GRANT_OPTION", "SELECT", "SCHEMA", "sales", "", "", "g")),
                         "GRANT SELECT ON SCHEMA::[sales] TO [g] WITH GRANT OPTION;")
        self.assertEqual(sp.statement_for(("GRANT", "VIEW DATABASE STATE", "DATABASE", "", "", "", "ops")),
                         "GRANT VIEW DATABASE STATE TO [ops];")
        self.assertEqual(sp.q("we]ird"), "[we]]ird]")


class TestPlan(unittest.TestCase):
    def test_roles_then_members_then_grants_and_never_revoke(self):
        primary = sp.read_snapshot(runner(
            perms=[SEL, ("GRANT", "SELECT", "SCHEMA", "sales", None, None, "claims_readers", "DATABASE_ROLE")],
            roles=["claims_readers"], members=[("claims_readers", "analyst@x.com")]))
        secondary = sp.read_snapshot(runner(
            perms=[("GRANT", "SELECT", "OBJECT_OR_COLUMN", "dbo", "claims", None, "old@x.com", "EXTERNAL_USER")]))
        plan = sp.plan_sql_permissions(primary, secondary)
        self.assertEqual([s["kind"] for s in plan["statements"]], ["create_role", "role_member", "grant", "grant"])
        self.assertEqual(plan["statements"][0]["statement"], "CREATE ROLE [claims_readers];")
        self.assertEqual(plan["extra_on_secondary"], ["GRANT SELECT ON [dbo].[claims] TO [old@x.com];"])

    def test_column_scope_runs_after_object_scope(self):
        # Object-level GRANT clears column-level permissions, so a column DENY
        # issued first is lost. DENY < GRANT alphabetically: order must be by scope.
        primary = sp.read_snapshot(runner(perms=[
            ("DENY", "SELECT", "OBJECT_OR_COLUMN", "dbo", "claims", "ssn", "r", "DATABASE_ROLE"),
            ("GRANT", "SELECT", "OBJECT_OR_COLUMN", "dbo", "claims", None, "r", "DATABASE_ROLE"),
            ("GRANT", "SELECT", "SCHEMA", "dbo", None, None, "r", "DATABASE_ROLE"),
            ("GRANT", "VIEW DATABASE STATE", "DATABASE", None, None, None, "r", "DATABASE_ROLE"),
        ]))
        plan = sp.plan_sql_permissions(primary, sp.read_snapshot(runner()))
        self.assertEqual([s["statement"] for s in plan["statements"]], [
            "GRANT VIEW DATABASE STATE TO [r];",
            "GRANT SELECT ON SCHEMA::[dbo] TO [r];",
            "GRANT SELECT ON [dbo].[claims] TO [r];",
            "DENY SELECT ON [dbo].[claims] ([ssn]) TO [r];",
        ])

    def test_grant_on_object_missing_in_secondary_waits(self):
        primary = sp.read_snapshot(runner(perms=[SEL]))
        secondary = sp.read_snapshot(runner(objects=()))
        plan = sp.plan_sql_permissions(primary, secondary)
        self.assertEqual(plan["statements"], [])
        self.assertEqual(plan["skipped"][0]["kind"], "skipped")
        self.assertIn("dbo.claims", plan["skipped"][0]["note"])

    def test_identical_databases_need_nothing(self):
        snap = sp.read_snapshot(runner(perms=[SEL], roles=["r"], members=[("r", "a@x.com")]))
        plan = sp.plan_sql_permissions(snap, snap)
        self.assertEqual((plan["statements"], plan["skipped"], plan["extra_on_secondary"]), ([], [], []))


class TestApply(unittest.TestCase):
    def test_one_failure_does_not_hide_the_rest_and_skipped_not_sent(self):
        sent = []

        def execute(sql):
            sent.append(sql)
            if "bad" in sql:
                raise RuntimeError("Cannot find the user 'bad'")
        res = sp.apply_statements(execute, [
            {"kind": "grant", "statement": "GRANT SELECT ON [dbo].[t] TO [bad];"},
            {"kind": "grant", "statement": "GRANT SELECT ON [dbo].[t] TO [ok];"},
            {"kind": "skipped", "statement": "GRANT SELECT ON [dbo].[later] TO [ok];"},
        ])
        self.assertEqual((res["applied"], res["failed"]), (1, 1))
        self.assertEqual(len(sent), 2)


class TestBuildPlan(unittest.TestCase):
    def test_matches_databases_by_type_and_name_and_reports_errors(self):
        items = {"pw": [{"id": "p1", "type": "Lakehouse", "displayName": "gold"},
                        {"id": "p2", "type": "Warehouse", "displayName": "dw"},
                        {"id": "p3", "type": "Lakehouse", "displayName": "only_primary"}],
                 "sw": [{"id": "s1", "type": "Lakehouse", "displayName": "gold"},
                        {"id": "s2", "type": "Warehouse", "displayName": "dw"}]}

        def fabric(method, ep):
            ws, kind, iid = ep.split("/")[2], ep.split("/")[3], ep.split("/")[4]
            conn = f"{ws}-{iid}.datawarehouse.fabric.microsoft.com"
            return {"properties": {"sqlEndpointProperties": {"connectionString": conn}} if kind == "lakehouses"
                    else {"connectionString": conn}}

        class Conn:
            def __init__(self, host):
                self.host = host

            def close(self):
                pass

        def connector(host, db, token):
            if db == "dw" and host.startswith("sw"):
                raise RuntimeError("Login timeout")
            return Conn(host)

        orig = sp.query_runner
        sp.query_runner = lambda c: runner(perms=[SEL] if c.host.startswith("pw") else [])
        try:
            plan = sp.build_sql_plan(fabric, lambda s: s, lambda: "tok", "pw", "sw",
                                     items["pw"], items["sw"], connector=connector)
        finally:
            sp.query_runner = orig
        self.assertEqual(plan["coverage"]["databases_scanned"], 1)
        self.assertEqual(plan["coverage"]["unmatched_in_secondary"], 1)
        self.assertIn("Login timeout", plan["coverage"]["errors"][0])
        self.assertEqual(plan["rows"][0]["statement"], "GRANT SELECT ON [dbo].[claims] TO [analyst@x.com];")
        self.assertEqual(plan["rows"][0]["secondary_host"], "sw-s1.datawarehouse.fabric.microsoft.com")


if __name__ == "__main__":
    unittest.main(verbosity=2)
