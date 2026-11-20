"""Tests for bcdr_schedules.py and failover/failback schedule handling."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import bcdr_schedules as bs

DAILY = {"type": "Daily", "times": ["06:00", "18:00"], "localTimeZoneId": "UTC",
         "startDateTime": "2026-01-01T00:00:00", "endDateTime": "2027-01-01T00:00:00"}
WEEKLY = {"type": "Weekly", "times": ["07:00"], "weekdays": ["Monday", "Friday"], "localTimeZoneId": "UTC",
          "startDateTime": "2026-01-01T00:00:00", "endDateTime": "2027-01-01T00:00:00"}


class FakeFabric:
    """In-memory Job Scheduler API; unknown job types answer 400 like the service."""

    VALID = {"DataPipeline": "Pipeline", "Notebook": "RunNotebook", "Dataflow": "Refresh"}

    def __init__(self, items, schedules=None, fail_patch=False):
        self.items = items  # id -> item
        self.schedules = schedules or {}  # (item_id, jt) -> [schedule]
        self.calls = []
        self.fail_patch = fail_patch
        self.n = 0

    def _err(self, code):
        e = Exception(f"API error {code}")
        e.status_code = code
        return e

    def __call__(self, method, endpoint, payload=None):
        self.calls.append((method, endpoint, payload))
        parts = endpoint.split("?")[0].split("/")
        item_id, jt = parts[4], parts[6]
        if self.VALID.get(self.items[item_id]["type"]) != jt:
            raise self._err(400)
        lst = self.schedules.setdefault((item_id, jt), [])
        if method == "GET":
            return {"value": [dict(s) for s in lst]}
        if method == "POST":
            self.n += 1
            s = {"id": f"new-{self.n}", "enabled": payload["enabled"], "configuration": payload["configuration"]}
            lst.append(s)
            return dict(s)
        if method == "PATCH":
            if self.fail_patch:
                raise self._err(403)
            s = next(x for x in lst if x["id"] == parts[8])
            assert "configuration" in payload, "PATCH must carry configuration"
            s.update(enabled=payload["enabled"], configuration=payload["configuration"])
            return dict(s)
        raise AssertionError(method)


def items(*specs):
    return [{"id": i, "type": t, "displayName": n} for i, t, n in specs]


class TestJobTypeProbe(unittest.TestCase):
    def test_probes_candidates_and_caches_per_type(self):
        its = items(("d1", "Dataflow", "df1"), ("d2", "Dataflow", "df2"))
        fab = FakeFabric({i["id"]: i for i in its})
        c = bs.ScheduleClient(fab)
        self.assertEqual(c.job_type("ws", its[0]), "Refresh")  # "Execute" rejected with 400 first
        n = len(fab.calls)
        self.assertEqual(c.job_type("ws", its[1]), "Refresh")
        self.assertEqual(len(fab.calls), n)

    def test_unknown_type_reported_not_guessed(self):
        its = items(("s1", "SparkJobDefinition", "sjd"))
        res = bs.collect(bs.ScheduleClient(FakeFabric({"s1": its[0]})), "ws", its)
        self.assertEqual(res["unsupported_types"], {"SparkJobDefinition": 1})

    def test_other_errors_are_not_mistaken_for_wrong_type(self):
        its = items(("p1", "DataPipeline", "pl"))
        e = Exception("API error 500")
        e.status_code = 500
        res = bs.collect(bs.ScheduleClient(mock.Mock(side_effect=e)), "ws", its)
        self.assertEqual(len(res["errors"]), 1)


class TestReplicationPlan(unittest.TestCase):
    def setUp(self):
        self.p_items = items(("p1", "DataPipeline", "load"), ("p2", "Notebook", "clean"), ("p3", "Notebook", "only_p"))
        self.s_items = items(("s1", "DataPipeline", "load"), ("s2", "Notebook", "clean"))
        self.p = FakeFabric({i["id"]: i for i in self.p_items}, {
            ("p1", "Pipeline"): [{"id": "a", "enabled": True, "configuration": DAILY}],
            ("p2", "RunNotebook"): [{"id": "b", "enabled": False, "configuration": WEEKLY}],
            ("p3", "RunNotebook"): [{"id": "c", "enabled": True, "configuration": DAILY}],
        })
        self.s = FakeFabric({i["id"]: i for i in self.s_items}, {
            ("s2", "RunNotebook"): [{"id": "x", "enabled": False,
                                     "configuration": {**WEEKLY, "weekdays": ["Friday", "Monday"], "interval": None}}],
        })

    def test_missing_schedules_replicated_paused_identical_ones_left(self):
        pc, sc = bs.ScheduleClient(self.p), bs.ScheduleClient(self.s)
        p = bs.collect(pc, "pw", self.p_items)["entries"]
        s = bs.collect(sc, "sw", self.s_items)["entries"]
        plan = bs.plan_replication(p, s, self.s_items)
        actions = {r["item_name"]: r["action"] for r in plan["rows"]}
        self.assertEqual(actions, {"load": bs.ACTION_CREATE, "clean": bs.ACTION_PRESENT,
                                   "only_p": bs.ACTION_MISSING_ITEM})
        res = bs.apply_replication(sc, "sw", plan["rows"])
        self.assertEqual(res["created"], 1)
        created = self.s.schedules[("s1", "Pipeline")][0]
        self.assertFalse(created["enabled"])
        self.assertEqual(created["configuration"], DAILY)

    def test_extra_secondary_schedule_reported(self):
        self.s.schedules[("s1", "Pipeline")] = [{"id": "z", "enabled": False, "configuration": WEEKLY}]
        p = bs.collect(bs.ScheduleClient(self.p), "pw", self.p_items)["entries"]
        s = bs.collect(bs.ScheduleClient(self.s), "sw", self.s_items)["entries"]
        plan = bs.plan_replication(p, s, self.s_items)
        self.assertEqual([e["schedule_id"] for e in plan["extra_on_secondary"]], ["z"])

    def test_dry_run_writes_nothing(self):
        p = bs.collect(bs.ScheduleClient(self.p), "pw", self.p_items)["entries"]
        plan = bs.plan_replication(p, [], self.s_items)
        bs.apply_replication(bs.ScheduleClient(self.s), "sw", plan["rows"], dry_run=True)
        self.assertEqual([c for c in self.s.calls if c[0] != "GET"], [])


class TestPauseAndActivate(unittest.TestCase):
    def setUp(self):
        self.its = items(("i1", "DataPipeline", "load"), ("i2", "Notebook", "clean"))
        self.fab = FakeFabric({i["id"]: i for i in self.its}, {
            ("i1", "Pipeline"): [{"id": "a", "enabled": True, "configuration": DAILY}],
            ("i2", "RunNotebook"): [{"id": "b", "enabled": False, "configuration": WEEKLY}],
        })
        self.c = bs.ScheduleClient(self.fab)

    def test_pause_only_touches_enabled_and_sends_configuration(self):
        res = bs.pause_schedules(self.c, "ws", self.its)
        self.assertEqual([e["item_name"] for e in res["paused"]], ["load"])
        self.assertFalse(self.fab.schedules[("i1", "Pipeline")][0]["enabled"])
        patch = [c for c in self.fab.calls if c[0] == "PATCH"][0]
        self.assertEqual(patch[2], {"enabled": False, "configuration": DAILY})

    def test_activate_enables_replica_creates_missing_and_reports_missing_items(self):
        self.fab.schedules[("i1", "Pipeline")][0]["enabled"] = False
        manifest = [
            {"item_type": "DataPipeline", "item_name": "load", "job_type": "Pipeline", "enabled": True,
             "configuration": DAILY},
            {"item_type": "Notebook", "item_name": "clean", "job_type": "RunNotebook", "enabled": True,
             "configuration": DAILY},
            {"item_type": "Notebook", "item_name": "gone", "job_type": "RunNotebook", "enabled": True,
             "configuration": DAILY},
            {"item_type": "Notebook", "item_name": "clean", "job_type": "RunNotebook", "enabled": False,
             "configuration": WEEKLY},
        ]
        res = bs.activate_schedules(self.c, "ws", self.its, manifest)
        self.assertEqual((res["enabled"], res["created"], res["missing_items"]), (["load"], ["clean"], ["gone"]))
        self.assertTrue(self.fab.schedules[("i1", "Pipeline")][0]["enabled"])
        self.assertFalse(self.fab.schedules[("i2", "RunNotebook")][0]["enabled"])  # was disabled on primary
        again = bs.activate_schedules(self.c, "ws", self.its, manifest[:2])
        self.assertEqual(sorted(again["already_enabled"]), ["clean", "load"])

    def test_activation_failure_is_reported(self):
        self.fab.fail_patch = True
        self.fab.schedules[("i1", "Pipeline")][0]["enabled"] = False
        res = bs.activate_schedules(self.c, "ws", self.its, [
            {"item_type": "DataPipeline", "item_name": "load", "job_type": "Pipeline", "enabled": True,
             "configuration": DAILY}])
        self.assertEqual(len(res["errors"]), 1)


class TestFailoverWiring(unittest.TestCase):
    def setUp(self):
        import failover
        self.fo = failover
        self.logger = mock.Mock()

    def test_unreachable_primary_flags_fallback(self):
        e = Exception("API error 503")
        e.status_code = 503
        with mock.patch.object(self.fo.common, "api_call", side_effect=e):
            res = self.fo.pause_pipelines("pw", self.logger)
        self.assertTrue(res["primary_unreachable"])

    def test_activation_merges_last_sync_manifest_only_when_primary_unreachable(self):
        saved = [{"item_type": "DataPipeline", "item_name": "load", "job_type": "Pipeline", "enabled": True,
                  "configuration": DAILY},
                 {"item_type": "Notebook", "item_name": "off", "job_type": "RunNotebook", "enabled": False,
                  "configuration": DAILY}]
        seen = []
        with mock.patch.object(self.fo.bcdr_schedules, "load_manifest", return_value=saved), \
             mock.patch.object(self.fo, "enable_schedules",
                               side_effect=lambda ws, m, lg, dr: seen.append(m) or
                               {"schedules_enabled": [x["item_name"] for x in m], "errors": []}):
            self.assertTrue(self.fo.activate_secondary_workspace("sw", [], self.logger))
            self.assertTrue(self.fo.activate_secondary_workspace("sw", [], self.logger, primary_unreachable=True))
        self.assertEqual(seen[0], [])
        self.assertEqual([x["item_name"] for x in seen[1]], ["load"])

    def test_activation_errors_downgrade_result(self):
        with mock.patch.object(self.fo, "enable_schedules",
                               return_value={"schedules_enabled": [], "errors": ["load: 403"]}):
            self.assertFalse(self.fo.activate_secondary_workspace("sw", [{"item_name": "load"}], self.logger))

    def test_no_dead_scheduler_endpoint_left(self):
        for name in ("failover.py", "failback.py"):
            src = open(os.path.join(HERE, name), encoding="utf-8").read()
            self.assertNotIn("/jobScheduler", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
