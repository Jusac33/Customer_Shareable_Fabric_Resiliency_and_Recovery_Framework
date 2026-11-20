"""
Job-schedule replication and failover activation (Fabric Job Scheduler API, GA).

  GET/POST  /workspaces/{ws}/items/{item}/jobs/{jobType}/schedules
  PATCH     /workspaces/{ws}/items/{item}/jobs/{jobType}/schedules/{scheduleId}

Follows the managed-DR pattern used by other platforms (for example Azure
Databricks managed DR replicates jobs with their schedules paused):

  * Sync      every primary schedule is replicated to the secondary item
              PAUSED (enabled=false), and a manifest of the primary schedules
              (including which were enabled) is written to
              data/schedule_manifest.json.
  * Failover  the schedules that were enabled on primary are enabled on the
              secondary, matched by item type + name + job type + config, and
              paused on the former primary when it is reachable. Because the
              manifest was captured during sync, activation does not need the
              primary region to be up.

Job types are probed per item type from a candidate list and cached, so new
or renamed job types are reported as unsupported rather than guessed.
"""

import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

JOB_TYPE_CANDIDATES: Dict[str, List[str]] = {
    "DataPipeline": ["Pipeline"],
    "Notebook": ["RunNotebook"],
    "SparkJobDefinition": ["sparkjob"],
    "Dataflow": ["Execute", "Refresh"],
    "CopyJob": ["CopyJob", "Execute"],
    "Lakehouse": ["TableMaintenance"],
}
SCHEDULABLE_TYPES = list(JOB_TYPE_CANDIDATES)
MANIFEST_PATH = "data/schedule_manifest.json"

ACTION_CREATE = "create_paused"
ACTION_UPDATE = "update_config_paused"
ACTION_PRESENT = "present"
ACTION_MISSING_ITEM = "missing_item_in_secondary"


def _status(e: Exception) -> Optional[int]:
    return getattr(e, "status_code", None)


def normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Comparable schedule configuration (drops nulls, sorts list fields)."""
    out = {}
    for k, v in (cfg or {}).items():
        if v is None or v == []:
            continue
        out[k] = sorted(v) if isinstance(v, list) and all(isinstance(x, str) for x in v) else v
    return out


class ScheduleClient:
    """Thin, cached wrapper over the Job Scheduler API."""

    def __init__(self, call: Callable):
        self.call = call
        self._job_type: Dict[str, Optional[str]] = {}

    def job_type(self, workspace_id: str, item: Dict[str, Any]) -> Optional[str]:
        itype = item.get("type", "")
        if itype in self._job_type:
            return self._job_type[itype]
        found = None
        for jt in JOB_TYPE_CANDIDATES.get(itype, []):
            try:
                self.call("GET", f"/workspaces/{workspace_id}/items/{item['id']}/jobs/{jt}/schedules")
                found = jt
                break
            except Exception as e:
                if _status(e) in (400, 404):
                    continue
                raise
        self._job_type[itype] = found
        return found

    def list(self, workspace_id: str, item_id: str, job_type: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        endpoint = f"/workspaces/{workspace_id}/items/{item_id}/jobs/{job_type}/schedules"
        resp = self.call("GET", endpoint)
        while True:
            out.extend(resp.get("value", []))
            token = resp.get("continuationToken")
            if not token:
                return out
            resp = self.call("GET", f"{endpoint}?continuationToken={token}")

    def create(self, workspace_id: str, item_id: str, job_type: str, config: Dict[str, Any], enabled: bool) -> Dict:
        return self.call("POST", f"/workspaces/{workspace_id}/items/{item_id}/jobs/{job_type}/schedules",
                         {"enabled": enabled, "configuration": config})

    def set_enabled(self, workspace_id: str, item_id: str, job_type: str, schedule: Dict[str, Any],
                    enabled: bool) -> Dict:
        # PATCH requires the full configuration alongside enabled.
        return self.call("PATCH",
                         f"/workspaces/{workspace_id}/items/{item_id}/jobs/{job_type}/schedules/{schedule['id']}",
                         {"enabled": enabled, "configuration": schedule.get("configuration") or {}})


def collect(client: ScheduleClient, workspace_id: str, items: List[Dict[str, Any]],
            workers: int = 8) -> Dict[str, Any]:
    """Every schedule on every schedulable item. Read-only; items are read in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    targets = [i for i in items if i.get("type") in JOB_TYPE_CANDIDATES]
    # Resolve each type's job type once (sequentially) so parallel reads don't race the probe.
    for t in {i["type"]: i for i in targets}.values():
        try:
            client.job_type(workspace_id, t)
        except Exception:
            pass

    def one(item: Dict[str, Any]):
        try:
            jt = client.job_type(workspace_id, item)
            if not jt:
                return ("unsupported", item["type"], None)
            return ("ok", item, jt, client.list(workspace_id, item["id"], jt))
        except Exception as e:
            return ("error", f"{item['type']}:{item.get('displayName')}: {str(e)[:200]}", None)

    entries: List[Dict[str, Any]] = []
    unsupported: Dict[str, int] = {}
    errors: List[str] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for res in pool.map(one, targets):
            if res[0] == "unsupported":
                unsupported[res[1]] = unsupported.get(res[1], 0) + 1
            elif res[0] == "error":
                errors.append(res[1])
            else:
                _, item, jt, schedules = res
                for s in schedules:
                    entries.append({
                        "item_id": item["id"], "item_name": item["displayName"], "item_type": item["type"],
                        "job_type": jt, "schedule_id": s.get("id", ""), "enabled": bool(s.get("enabled")),
                        "configuration": s.get("configuration") or {},
                    })
    return {"entries": entries, "unsupported_types": unsupported, "errors": errors}


def _key(e: Dict[str, Any]) -> Tuple[str, str, str]:
    return (e["item_type"], e["item_name"], e["job_type"])


def plan_replication(primary: List[Dict[str, Any]], secondary: List[Dict[str, Any]],
                     secondary_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rows that make each primary schedule exist (paused) on the secondary. Pure."""
    s_items = {(i["type"], i["displayName"]): i for i in secondary_items}
    s_by_key: Dict[Tuple, List[Dict[str, Any]]] = {}
    for s in secondary:
        s_by_key.setdefault(_key(s), []).append(s)
    rows: List[Dict[str, Any]] = []
    used: set = set()
    for p in primary:
        target = s_items.get((p["item_type"], p["item_name"]))
        base = {"item_type": p["item_type"], "item_name": p["item_name"], "job_type": p["job_type"],
                "primary_enabled": p["enabled"], "configuration": p["configuration"],
                "secondary_item_id": target["id"] if target else "", "secondary_schedule_id": ""}
        if not target:
            rows.append({**base, "action": ACTION_MISSING_ITEM})
            continue
        want = normalize_config(p["configuration"])
        candidates = [s for s in s_by_key.get(_key(p), []) if s["schedule_id"] not in used]
        same = next((s for s in candidates if normalize_config(s["configuration"]) == want), None)
        if same:
            used.add(same["schedule_id"])
            rows.append({**base, "action": ACTION_PRESENT, "secondary_schedule_id": same["schedule_id"]})
            continue
        rows.append({**base, "action": ACTION_CREATE})
    extra = [s for ss in s_by_key.values() for s in ss if s["schedule_id"] not in used]
    return {"rows": rows, "extra_on_secondary": extra}


def apply_replication(client: ScheduleClient, secondary_ws: str, rows: List[Dict[str, Any]],
                      logger=None, dry_run: bool = False) -> Dict[str, Any]:
    """Create missing schedules on the secondary, always PAUSED."""
    res = {"created": 0, "failed": 0, "failures": []}
    for r in rows:
        if r["action"] != ACTION_CREATE:
            continue
        if dry_run:
            res["created"] += 1
            continue
        try:
            client.create(secondary_ws, r["secondary_item_id"], r["job_type"], r["configuration"], enabled=False)
            res["created"] += 1
            if logger:
                logger.info(f"[SCHEDULE] {r['item_type']}:{r['item_name']} ({r['job_type']}) replicated paused")
        except Exception as e:
            res["failed"] += 1
            res["failures"].append({"item": r["item_name"], "error": str(e)[:300]})
            if logger:
                logger.error(f"[SCHEDULE FAILED] {r['item_name']}: {e}")
    return res


def write_manifest(entries: List[Dict[str, Any]], primary_ws: str, path: str = MANIFEST_PATH) -> None:
    from datetime import datetime, timezone
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"captured_at": datetime.now(timezone.utc).isoformat(), "primary_workspace": primary_ws,
                   "schedules": entries}, f, indent=2)


def load_manifest(path: str = MANIFEST_PATH) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("schedules", [])


def pause_schedules(client: ScheduleClient, workspace_id: str, items: List[Dict[str, Any]],
                    logger=None, dry_run: bool = False) -> Dict[str, Any]:
    """Disable every enabled schedule; return what was enabled (for later re-enable)."""
    snap = collect(client, workspace_id, items)
    res = {"paused": [], "errors": list(snap["errors"]), "unsupported_types": snap["unsupported_types"]}
    for e in snap["entries"]:
        if not e["enabled"]:
            continue
        if not dry_run:
            try:
                client.set_enabled(workspace_id, e["item_id"], e["job_type"],
                                   {"id": e["schedule_id"], "configuration": e["configuration"]}, False)
            except Exception as ex:
                res["errors"].append(f"{e['item_name']}: {str(ex)[:200]}")
                continue
        res["paused"].append(e)
        if logger:
            logger.info(f"{'[DRY RUN] ' if dry_run else ''}Paused schedule on '{e['item_name']}' ({e['job_type']})")
    return res


def activate_schedules(client: ScheduleClient, workspace_id: str, items: List[Dict[str, Any]],
                       manifest: List[Dict[str, Any]], logger=None, dry_run: bool = False) -> Dict[str, Any]:
    """Enable, in workspace_id, the schedules the manifest marks enabled.

    Matches by item type + name + job type + configuration; creates the
    schedule (enabled) when the paused replica is missing.
    """
    by_name = {(i["type"], i["displayName"]): i for i in items}
    res = {"enabled": [], "created": [], "already_enabled": [], "missing_items": [], "errors": []}
    listed: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for m in manifest:
        if not m.get("enabled"):
            continue
        item = by_name.get((m["item_type"], m["item_name"]))
        if not item:
            res["missing_items"].append(m["item_name"])
            continue
        try:
            k = (item["id"], m["job_type"])
            if k not in listed:
                listed[k] = client.list(workspace_id, item["id"], m["job_type"])
            want = normalize_config(m["configuration"])
            match = next((s for s in listed[k] if normalize_config(s.get("configuration") or {}) == want), None)
            if match and match.get("enabled"):
                res["already_enabled"].append(m["item_name"])
                continue
            if dry_run:
                res["enabled" if match else "created"].append(m["item_name"])
                continue
            if match:
                client.set_enabled(workspace_id, item["id"], m["job_type"], match, True)
                match["enabled"] = True
                res["enabled"].append(m["item_name"])
            else:
                created = client.create(workspace_id, item["id"], m["job_type"], m["configuration"], enabled=True)
                listed[k].append(created or {"configuration": m["configuration"], "enabled": True})
                res["created"].append(m["item_name"])
            if logger:
                logger.info(f"Enabled schedule on '{m['item_name']}' ({m['job_type']})")
        except Exception as e:
            res["errors"].append(f"{m['item_name']}: {str(e)[:200]}")
            if logger:
                logger.error(f"Failed to enable schedule on '{m['item_name']}': {e}")
    return res
