"""
Measured data freshness (RPO) between a primary and secondary lakehouse.

Compares the latest Delta commit of every table on both sides using the OneLake
ADLS Gen2 DFS endpoint (documented, data plane, list operations only):
    GET https://onelake.dfs.fabric.microsoft.com/{workspaceId}?resource=filesystem&directory=...

This works for schema-enabled lakehouses (Tables/<schema>/<table>), where the
Fabric "List Tables" API returns UnsupportedOperationForSchemasEnabledLakehouse.
It is also usable when the Fabric control plane is degraded, because OneLake
data-plane access is independent of the item APIs.

Item presence (what the dashboard's "% mirrored" measures) says nothing about
data currency; this module is the source of truth for "how far behind is DR".
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
_COMMIT_RE = re.compile(r"/(\d{20})\.json$")
# A secondary commit within this window of the primary counts as in sync.
IN_SYNC_TOLERANCE_HOURS = 0.1


class FreshnessError(Exception):
    """Raised when OneLake cannot be listed. Never swallowed into 'in sync'."""


def _session() -> requests.Session:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=5, connect=5, read=5, backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]), respect_retry_after_header=True,
    )))
    return s


def list_paths(session, token: str, workspace_id: str, directory: str) -> Optional[List[Dict[str, Any]]]:
    """List one directory level. Returns None when the directory doesn't exist."""
    headers = {"Authorization": f"Bearer {token}", "x-ms-version": "2023-11-03"}
    out: List[Dict[str, Any]] = []
    continuation = None
    while True:
        params = {"resource": "filesystem", "recursive": "false", "directory": directory}
        if continuation:
            params["continuation"] = continuation
        r = session.get(f"{ONELAKE_DFS}/{workspace_id}", headers=headers, params=params, timeout=60)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise FreshnessError(f"OneLake list {directory} failed: HTTP {r.status_code} {r.text[:200]}")
        out += r.json().get("paths", [])
        continuation = r.headers.get("x-ms-continuation")
        if not continuation:
            return out


def latest_commit(entries: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Pick the highest-numbered Delta commit file from a _delta_log listing."""
    if not entries:
        return None
    commits = []
    for e in entries:
        m = _COMMIT_RE.search(e.get("name", ""))
        if m:
            commits.append((int(m.group(1)), e.get("lastModified")))
    if not commits:
        return None
    version, modified = max(commits, key=lambda c: c[0])
    return {"version": version, "last_commit": modified}


def table_commits(session, token: str, workspace_id: str, lakehouse_id: str) -> Dict[str, Dict[str, Any]]:
    """{'schema/table' or 'table': {version, last_commit}} for one lakehouse."""
    root = list_paths(session, token, workspace_id, f"{lakehouse_id}/Tables")
    if root is None:
        return {}
    dirs = [p["name"] for p in root if p.get("isDirectory") == "true"]

    def probe(path: str) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
        log = list_paths(session, token, workspace_id, f"{path}/_delta_log")
        if log is not None:
            return [(path, latest_commit(log))]
        # Not a table: a schema folder. Descend one level.
        children = list_paths(session, token, workspace_id, path) or []
        found = []
        for c in children:
            if c.get("isDirectory") == "true":
                found.append((c["name"], latest_commit(list_paths(session, token, workspace_id, f"{c['name']}/_delta_log"))))
        return found

    result: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for found in pool.map(probe, dirs):
            for path, commit in found:
                if commit:
                    result[path.split("/Tables/", 1)[-1]] = commit
    return result


def _parse_http_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compare(primary: Dict[str, Dict[str, Any]], secondary: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-table comparison. Pure function (unit-tested)."""
    rows = []
    for name in sorted(set(primary) | set(secondary)):
        p, s = primary.get(name), secondary.get(name)
        lag = None
        pt = _parse_http_date(p and p.get("last_commit"))
        st = _parse_http_date(s and s.get("last_commit"))
        if pt and st:
            lag = round(max(0.0, (pt - st).total_seconds() / 3600), 2)
        if p and not s:
            state = "missing_in_secondary"
        elif s and not p:
            state = "extra_in_secondary"
        elif lag is None:
            state = "unknown"
        elif lag > IN_SYNC_TOLERANCE_HOURS:
            state = "behind"
        else:
            state = "in_sync"
        rows.append({
            "table": name, "state": state, "behind_hours": lag,
            "primary_version": p and p.get("version"), "secondary_version": s and s.get("version"),
            "primary_last_commit": p and p.get("last_commit"), "secondary_last_commit": s and s.get("last_commit"),
        })
    return rows


def summarize(lakehouses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Overall measured RPO = worst lag across every compared table."""
    rows = [r for lh in lakehouses for r in lh.get("tables", [])]
    lags = [r["behind_hours"] for r in rows if r["behind_hours"] is not None]
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    errors = [lh["error"] for lh in lakehouses if lh.get("error")]
    return {
        "tables_compared": len(rows),
        "states": counts,
        "measured_rpo_hours": max(lags) if lags else None,
        "median_lag_hours": sorted(lags)[len(lags) // 2] if lags else None,
        "complete": not errors and counts.get("unknown", 0) == 0,
        "errors": errors,
    }


def measure_pair(token: str, primary_ws: str, secondary_ws: str,
                 primary_lakehouses: Dict[str, str], secondary_lakehouses: Dict[str, str],
                 session_factory: Callable[[], Any] = _session) -> Dict[str, Any]:
    """Measure every primary lakehouse against its same-named secondary lakehouse."""
    session = session_factory()

    def one(item):
        name, p_id = item
        entry: Dict[str, Any] = {"lakehouse": name, "tables": []}
        s_id = secondary_lakehouses.get(name)
        if not s_id:
            entry["error"] = f"{name}: no lakehouse with this name in secondary"
            return entry
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                fp = pool.submit(table_commits, session, token, primary_ws, p_id)
                fs = pool.submit(table_commits, session, token, secondary_ws, s_id)
                entry["tables"] = compare(fp.result(), fs.result())
        except FreshnessError as e:
            entry["error"] = f"{name}: {e}"
        return entry

    with ThreadPoolExecutor(max_workers=4) as pool:
        lakehouses = list(pool.map(one, sorted(primary_lakehouses.items())))
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "method": "OneLake DFS _delta_log latest commit (data plane)",
        "lakehouses": lakehouses,
        "summary": summarize(lakehouses),
    }
