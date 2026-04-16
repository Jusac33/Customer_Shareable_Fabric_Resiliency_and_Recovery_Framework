"""
Fabric Resiliency & Recovery Dashboard - Flask Web Application

Real-time monitoring and command center for Fabric DR operations.
Provides status visualization, capacity monitoring, event logs, and controls.

Supports interactive Microsoft login and Service Principal authentication.

Run: python app.py
Access: http://localhost:5000
"""

import os
import sys
import csv
import json
import hashlib
import logging
import threading
import time
import uuid
import subprocess
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

import msal
import requests
from flask import Flask, render_template, jsonify, request, session, redirect, url_for, make_response
from flask_cors import CORS

# Initialize Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.urandom(24)  # Session encryption key
CORS(app)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# Fabric API configuration
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPES = ["https://api.fabric.microsoft.com/.default"]
ONELAKE_SCOPES = ["https://storage.azure.com/.default"]
KUSTO_SCOPES = ["https://kusto.kusto.windows.net/.default"]
SQL_SCOPES = ["https://database.windows.net/.default"]

# Microsoft first-party public client (Azure PowerShell — available in all tenants)
# Override with PUBLIC_CLIENT_ID env var to use your own registered app
PUBLIC_CLIENT_ID = os.environ.get("PUBLIC_CLIENT_ID", "1950a258-227b-4e31-a9cf-717495945fc2")

# Service Principal configuration (loaded from env vars or .sp_config.json)
_SP_CONFIG_FILE = os.path.join(os.path.dirname(__file__), ".sp_config.json")

def _load_sp_config() -> Dict[str, str]:
    """Load Service Principal configuration from env vars or config file."""
    cfg: Dict[str, str] = {}
    # Env vars take precedence
    if os.environ.get("FABRIC_SP_TENANT_ID"):
        cfg["tenant_id"] = os.environ["FABRIC_SP_TENANT_ID"]
        cfg["client_id"] = os.environ.get("FABRIC_SP_CLIENT_ID", "")
        cfg["client_secret"] = os.environ.get("FABRIC_SP_CLIENT_SECRET", "")
    elif os.path.exists(_SP_CONFIG_FILE):
        try:
            with open(_SP_CONFIG_FILE, "r") as f:
                cfg = json.load(f)
        except Exception:
            pass
    return cfg

def _save_sp_config(cfg: Dict[str, str]):
    """Persist SP config to disk (secrets stored locally only)."""
    try:
        with open(_SP_CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

_sp_config: Dict[str, str] = _load_sp_config()

# In-memory auth state (single-user local dashboard)
_auth_state: Dict[str, Any] = {
    "access_token": None,
    "token_expiry": 0,
    "user_name": None,
    "user_email": None,
    "login_in_progress": False,
    "login_error": None,
    "msal_app": None,
    "accounts": None,
    "auth_mode": None,  # "interactive" or "service_principal"
}

# Workspace selection state — persisted to .workspace_state.json
# Supports MULTIPLE workspace pairs with an active-pair selector.
_WS_STATE_FILE = os.path.join(os.path.dirname(__file__), ".workspace_state.json")

def _load_workspace_state() -> Dict[str, Any]:
    default: Dict[str, Any] = {"pairs": [], "active_pair": None, "all_workspaces": []}
    try:
        if os.path.exists(_WS_STATE_FILE):
            with open(_WS_STATE_FILE, "r") as f:
                saved = json.load(f)
            # --- Migrate old flat format to pairs array ---
            if "primary_id" in saved and "pairs" not in saved:
                pair_id = str(uuid.uuid4())[:8]
                label = saved.get("primary_name") or "Default"
                pair = {
                    "id": pair_id,
                    "label": label,
                    "primary_id": saved.get("primary_id"),
                    "primary_name": saved.get("primary_name"),
                    "secondary_id": saved.get("secondary_id"),
                    "secondary_name": saved.get("secondary_name"),
                }
                default["pairs"] = [pair]
                default["active_pair"] = pair_id
                logger.info(f"Migrated workspace state to multi-pair format: {label}")
            else:
                default["pairs"] = saved.get("pairs", [])
                default["active_pair"] = saved.get("active_pair")
                default["all_workspaces"] = saved.get("all_workspaces", [])
            # Log loaded pairs
            for p in default["pairs"]:
                logger.info(f"  Pair '{p.get('label')}': {p.get('primary_name')} ↔ {p.get('secondary_name')}")
    except Exception:
        pass
    return default

def _save_workspace_state():
    try:
        data = {
            "pairs": _workspace_state["pairs"],
            "active_pair": _workspace_state.get("active_pair"),
        }
        with open(_WS_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

_workspace_state: Dict[str, Any] = _load_workspace_state()


# ============================================================================
# AUTHENTICATION — Interactive Browser Login (acquire_token_interactive)
# ============================================================================

_TOKEN_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".msal_token_cache.bin")

def _get_token_cache() -> msal.SerializableTokenCache:
    """Get a persistent MSAL token cache."""
    cache = msal.SerializableTokenCache()
    try:
        if os.path.exists(_TOKEN_CACHE_FILE):
            with open(_TOKEN_CACHE_FILE, "r") as f:
                cache.deserialize(f.read())
    except Exception:
        pass
    return cache

def _save_token_cache():
    """Persist the MSAL token cache to disk."""
    try:
        app_client = _auth_state.get("msal_app")
        if app_client and app_client.token_cache.has_state_changed:
            with open(_TOKEN_CACHE_FILE, "w") as f:
                f.write(app_client.token_cache.serialize())
    except Exception:
        pass

def _get_msal_app() -> msal.PublicClientApplication:
    """Get or create the MSAL public client application with persistent cache."""
    if _auth_state["msal_app"] is None:
        cache = _get_token_cache()
        _auth_state["msal_app"] = msal.PublicClientApplication(
            PUBLIC_CLIENT_ID,
            authority="https://login.microsoftonline.com/organizations",
            token_cache=cache,
        )
        # Try to restore auth from cached tokens
        accounts = _auth_state["msal_app"].get_accounts()
        if accounts:
            result = _auth_state["msal_app"].acquire_token_silent(FABRIC_SCOPES, account=accounts[0])
            if result and "access_token" in result:
                _auth_state["access_token"] = result["access_token"]
                _auth_state["token_expiry"] = time.time() + result.get("expires_in", 3600) - 120
                _auth_state["accounts"] = accounts
                _auth_state["auth_mode"] = "interactive"
                claims = result.get("id_token_claims", {})
                _auth_state["user_name"] = claims.get("name", "User")
                _auth_state["user_email"] = claims.get("preferred_username", accounts[0].get("username", ""))
                logger.info(f"Restored session for {_auth_state['user_email']} from token cache")
                _save_token_cache()
    return _auth_state["msal_app"]


def _do_interactive_login():
    """Run acquire_token_interactive in a background thread.
    Opens the system browser for login; MSAL handles its own redirect."""
    _auth_state["login_in_progress"] = True
    _auth_state["login_error"] = None
    try:
        app_client = _get_msal_app()
        result = app_client.acquire_token_interactive(
            scopes=FABRIC_SCOPES,
            prompt="select_account",
        )
        if "access_token" in result:
            _auth_state["access_token"] = result["access_token"]
            _auth_state["token_expiry"] = time.time() + result.get("expires_in", 3600) - 120
            _auth_state["accounts"] = app_client.get_accounts()
            _auth_state["auth_mode"] = "interactive"
            claims = result.get("id_token_claims", {})
            _auth_state["user_name"] = claims.get("name", "User")
            _auth_state["user_email"] = claims.get("preferred_username", "")
            logger.info(f"Login successful for {_auth_state['user_email']}")
            _save_token_cache()
        else:
            _auth_state["login_error"] = result.get("error_description", result.get("error", "Authentication failed"))
            logger.error(f"Login failed: {_auth_state['login_error']}")
    except Exception as e:
        _auth_state["login_error"] = str(e)
        logger.exception("Interactive login failed")
    finally:
        _auth_state["login_in_progress"] = False


def _do_sp_login(tenant_id: str, client_id: str, client_secret: str):
    """Authenticate using a Service Principal (client credentials flow)."""
    _auth_state["login_in_progress"] = True
    _auth_state["login_error"] = None
    try:
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        sp_app = msal.ConfidentialClientApplication(
            client_id,
            authority=authority,
            client_credential=client_secret,
        )
        result = sp_app.acquire_token_for_client(scopes=FABRIC_SCOPES)
        if "access_token" in result:
            _auth_state["access_token"] = result["access_token"]
            _auth_state["token_expiry"] = time.time() + result.get("expires_in", 3600) - 120
            _auth_state["msal_app"] = sp_app
            _auth_state["accounts"] = None
            _auth_state["auth_mode"] = "service_principal"
            _auth_state["user_name"] = f"SP: {client_id[:8]}..."
            _auth_state["user_email"] = f"service-principal@{tenant_id[:8]}"
            # Save SP config for next startup
            _sp_config.update({"tenant_id": tenant_id, "client_id": client_id, "client_secret": client_secret})
            _save_sp_config(_sp_config)
            logger.info(f"Service Principal login successful: {client_id}")
        else:
            _auth_state["login_error"] = result.get("error_description", result.get("error", "SP auth failed"))
            logger.error(f"SP login failed: {_auth_state['login_error']}")
    except Exception as e:
        _auth_state["login_error"] = str(e)
        logger.exception("Service Principal login failed")
    finally:
        _auth_state["login_in_progress"] = False


def _ensure_token() -> Optional[str]:
    """Silently refresh the token if we have a cached account, or return existing valid token."""
    if _auth_state["access_token"] and time.time() < _auth_state["token_expiry"]:
        return _auth_state["access_token"]

    # Service Principal: re-acquire using client credentials
    if _auth_state.get("auth_mode") == "service_principal":
        sp_app = _auth_state.get("msal_app")
        if sp_app and isinstance(sp_app, msal.ConfidentialClientApplication):
            result = sp_app.acquire_token_for_client(scopes=FABRIC_SCOPES)
            if result and "access_token" in result:
                _auth_state["access_token"] = result["access_token"]
                _auth_state["token_expiry"] = time.time() + result.get("expires_in", 3600) - 120
                return result["access_token"]
        return _auth_state.get("access_token")

    # Interactive: try silent acquisition
    app_client = _get_msal_app()
    accounts = _auth_state.get("accounts") or app_client.get_accounts()
    if accounts:
        result = app_client.acquire_token_silent(FABRIC_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _auth_state["access_token"] = result["access_token"]
            _auth_state["token_expiry"] = time.time() + result.get("expires_in", 3600) - 120
            _save_token_cache()
            return result["access_token"]

    return _auth_state.get("access_token")


def is_authenticated() -> bool:
    """Check whether we have a valid token."""
    token = _ensure_token()
    return token is not None and time.time() < _auth_state.get("token_expiry", 0)


def _get_onelake_token() -> Optional[str]:
    """Get a storage token for OneLake DFS API (https://storage.azure.com)."""
    # Service Principal: acquire storage token via client credentials
    if _auth_state.get("auth_mode") == "service_principal":
        sp_app = _auth_state.get("msal_app")
        if sp_app and isinstance(sp_app, msal.ConfidentialClientApplication):
            result = sp_app.acquire_token_for_client(scopes=ONELAKE_SCOPES)
            if result and "access_token" in result:
                logger.info("OneLake storage token acquired (SP)")
                return result["access_token"]
        logger.warning("SP OneLake token acquisition failed")
        return None

    # Interactive user flow
    app_client = _get_msal_app()
    accounts = _auth_state.get("accounts") or app_client.get_accounts()
    if accounts:
        result = app_client.acquire_token_silent(ONELAKE_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            logger.info("OneLake storage token acquired")
            return result["access_token"]
        else:
            err = result.get("error", "unknown") if result else "no result"
            logger.warning(f"OneLake token failed: {err}")
    else:
        logger.warning("No MSAL accounts for OneLake token")
    return None


def _get_kusto_token() -> Optional[str]:
    """Get a Kusto/KQL token (audience https://kusto.kusto.windows.net)."""
    if _auth_state.get("auth_mode") == "service_principal":
        sp_app = _auth_state.get("msal_app")
        if sp_app and isinstance(sp_app, msal.ConfidentialClientApplication):
            result = sp_app.acquire_token_for_client(scopes=KUSTO_SCOPES)
            if result and "access_token" in result:
                return result["access_token"]
        return None
    app_client = _get_msal_app()
    accounts = _auth_state.get("accounts") or app_client.get_accounts()
    if accounts:
        result = app_client.acquire_token_silent(KUSTO_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    return None


def _get_sql_token() -> Optional[str]:
    """Get a token for Fabric SQL analytics endpoint (https://database.windows.net)."""
    if _auth_state.get("auth_mode") == "service_principal":
        sp_app = _auth_state.get("msal_app")
        if sp_app and isinstance(sp_app, msal.ConfidentialClientApplication):
            result = sp_app.acquire_token_for_client(scopes=SQL_SCOPES)
            if result and "access_token" in result:
                return result["access_token"]
        return None
    app_client = _get_msal_app()
    accounts = _auth_state.get("accounts") or app_client.get_accounts()
    if accounts:
        result = app_client.acquire_token_silent(SQL_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    return None


def _resolve_sql_server(workspace_id: str, database_name: str) -> str:
    """Resolve the actual SQL analytics endpoint hostname for a lakehouse/warehouse."""
    items = get_workspace_items(workspace_id)

    # For Lakehouses, try the Fabric Lakehouses API for sqlEndpointProperties
    for item in items:
        if item.get("displayName") == database_name and item.get("type") == "Lakehouse":
            item_id = item.get("id", "")
            try:
                resp = fabric_api("GET", f"/workspaces/{workspace_id}/lakehouses/{item_id}")
                props = resp.get("properties", {})
                sql_props = props.get("sqlEndpointProperties", {})
                conn_str = sql_props.get("connectionString", "")
                if conn_str:
                    logger.info(f"SQL endpoint for {database_name}: {conn_str}")
                    return conn_str
                # Also check for provisioningStatus and id
                sql_id = sql_props.get("id", "")
                if sql_id:
                    server = f"{sql_id}.datawarehouse.fabric.microsoft.com"
                    logger.info(f"SQL endpoint by ID for {database_name}: {server}")
                    return server
            except Exception as e:
                logger.warning(f"Could not get lakehouse SQL endpoint via API: {e}")
            break

    # For Warehouses, try the Fabric Warehouses API
    for item in items:
        if item.get("displayName") == database_name and item.get("type") == "Warehouse":
            item_id = item.get("id", "")
            try:
                resp = fabric_api("GET", f"/workspaces/{workspace_id}/warehouses/{item_id}")
                props = resp.get("properties", {})
                conn_str = props.get("connectionString", "")
                if conn_str:
                    return conn_str
            except Exception as e:
                logger.warning(f"Could not get warehouse SQL endpoint: {e}")
            break

    # Try to find the SQLEndpoint item with the same name — use its ID as server
    for item in items:
        if item.get("type") == "SQLEndpoint" and item.get("displayName") == database_name:
            sql_ep_id = item.get("id", "")
            if sql_ep_id:
                server = f"{sql_ep_id}.datawarehouse.fabric.microsoft.com"
                logger.info(f"SQL endpoint by SQLEndpoint item for {database_name}: {server}")
                return server
            break

    # Last resort fallback
    logger.warning(f"Could not resolve SQL endpoint for {database_name}, using workspace ID fallback")
    return f"{workspace_id}.datawarehouse.fabric.microsoft.com"


# Cache resolved SQL endpoint servers
_sql_server_cache: Dict[str, str] = {}


def _get_sql_connection(workspace_id: str, database_name: str):
    """Get a pyodbc connection to a Fabric SQL analytics endpoint."""
    import pyodbc
    import struct
    token = _get_sql_token()
    if not token:
        raise RuntimeError("Could not acquire SQL token")

    cache_key = f"{workspace_id}:{database_name}"
    if cache_key in _sql_server_cache:
        server = _sql_server_cache[cache_key]
    else:
        server = _resolve_sql_server(workspace_id, database_name)
        _sql_server_cache[cache_key] = server

    # Encode token as bytes for pyodbc
    token_bytes = token.encode("UTF-16-LE")
    token_struct = struct.pack(f'<I{len(token_bytes)}s', len(token_bytes), token_bytes)

    # Pick best available driver
    drivers = pyodbc.drivers()
    if "ODBC Driver 18 for SQL Server" in drivers:
        driver = "ODBC Driver 18 for SQL Server"
    elif "ODBC Driver 17 for SQL Server" in drivers:
        driver = "ODBC Driver 17 for SQL Server"
    else:
        driver = "SQL Server"

    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database_name};"
        f"Encrypt=Yes;"
        f"TrustServerCertificate=No;"
    )
    conn = pyodbc.connect(conn_str, attrs_before={1256: token_struct})
    return conn


def _run_sql(workspace_id: str, database_name: str, sql: str) -> List[Dict]:
    """Execute a T-SQL statement against a Fabric SQL analytics endpoint and return results."""
    conn = _get_sql_connection(workspace_id, database_name)
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        if cursor.description:
            columns = [col[0] for col in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            return rows
        conn.commit()
        return []
    finally:
        conn.close()


def _get_kql_db_query_uri(workspace_id: str, kql_db_id: str) -> Optional[str]:
    """Discover the queryServiceUri for a KQL Database."""
    try:
        resp = fabric_api("GET", f"/workspaces/{workspace_id}/kqlDatabases/{kql_db_id}")
        return resp.get("properties", {}).get("queryServiceUri")
    except Exception as e:
        logger.warning(f"Could not get query URI for KQL DB {kql_db_id}: {e}")
        return None


def _run_kql_command(cluster_uri: str, db_name: str, command: str) -> Dict[str, Any]:
    """Execute a KQL management command (.command) via the Kusto REST API."""
    token = _get_kusto_token()
    if not token:
        raise RuntimeError("No Kusto token available")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"db": db_name, "csl": command}
    resp = requests.post(f"{cluster_uri}/v1/rest/mgmt", headers=headers, json=body, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"KQL command failed {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _run_kql_query(cluster_uri: str, db_name: str, query: str) -> Dict[str, Any]:
    """Execute a KQL query (read-only) via the Kusto REST API."""
    token = _get_kusto_token()
    if not token:
        raise RuntimeError("No Kusto token available")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"db": db_name, "csl": query}
    resp = requests.post(f"{cluster_uri}/v1/rest/query", headers=headers, json=body, timeout=120)
    if resp.status_code >= 400:
        raise RuntimeError(f"KQL query failed {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def fabric_api(method: str, endpoint: str, payload: dict = None, timeout: int = 30, params: dict = None) -> Any:
    """Call the Fabric REST API with the current token.  Handles 202 long-running operations."""
    token = _ensure_token()
    if not token:
        raise RuntimeError("Not authenticated")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{FABRIC_API_BASE}{endpoint}"
    resp = requests.request(method, url, headers=headers, json=payload, params=params, timeout=timeout)
    if resp.status_code == 204:
        return {}
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 30))
        logger.warning(f"Rate limited on {endpoint}, retry after {retry_after}s")
        raise RuntimeError(f"Rate limited — retry after {retry_after}s")

    # Handle 202 Accepted — long-running operation
    if resp.status_code == 202:
        location = resp.headers.get("Location")
        retry_after = int(resp.headers.get("Retry-After", 5))
        if location:
            logger.info(f"Long-running operation for {endpoint}, polling {location}")
            for attempt in range(60):  # poll up to ~300 seconds
                time.sleep(retry_after)
                poll_resp = requests.get(location, headers=headers, timeout=timeout)
                if poll_resp.status_code == 200:
                    # Operation completed — check if we need to fetch the result
                    try:
                        op_body = poll_resp.json()
                    except Exception:
                        op_body = {}
                    op_status = op_body.get("status", "")
                    # Check for failed operations first
                    if op_status == "Failed":
                        err = op_body.get("error", {})
                        err_msg = err.get("message", "Unknown error") if isinstance(err, dict) else str(err)
                        raise RuntimeError(f"LRO failed for {endpoint}: {err_msg}")
                    # If this is an operation status response with Succeeded,
                    # fetch the actual result from {location}/result
                    if op_status == "Succeeded":
                        result_url = f"{location}/result"
                        logger.info(f"LRO succeeded, fetching result from {result_url}")
                        result_resp = requests.get(result_url, headers=headers, timeout=timeout)
                        if result_resp.status_code == 200:
                            try:
                                return result_resp.json()
                            except Exception:
                                return {}
                        else:
                            logger.warning(f"LRO result fetch returned {result_resp.status_code}: {result_resp.text[:300]}")
                            return op_body
                    # If no "status" field, this is already the final response
                    if "status" not in op_body:
                        return op_body
                    # Other statuses (e.g. Running) — keep polling
                    continue
                if poll_resp.status_code == 202:
                    retry_after = int(poll_resp.headers.get("Retry-After", 5))
                    continue
                if poll_resp.status_code >= 400:
                    raise RuntimeError(f"LRO poll failed {poll_resp.status_code}: {poll_resp.text[:500]}")
            raise RuntimeError(f"Long-running operation timed out for {endpoint}")
        # 202 with no Location — try to return body or empty dict
        try:
            return resp.json() if resp.text.strip() else {}
        except Exception:
            return {}

    if resp.status_code >= 400:
        raise RuntimeError(f"Fabric API {resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()
    except Exception:
        return {}


# ============================================================================
# CACHING — avoid hammering the Fabric API
# ============================================================================

_cache: Dict[str, Any] = {}
_cache_ttl: Dict[str, float] = {}
CACHE_SECONDS = 60  # Cache API results for 60 seconds


def _cached_call(cache_key: str, fn, *args):
    """Return cached result if fresh, otherwise call fn and cache."""
    now = time.time()
    if cache_key in _cache and now < _cache_ttl.get(cache_key, 0):
        return _cache[cache_key]
    result = fn(*args)
    _cache[cache_key] = result
    _cache_ttl[cache_key] = now + CACHE_SECONDS
    return result


# ============================================================================
# CONNECTION STRING REWRITING for artifact definitions
# ============================================================================

import base64
import re as _re


def _get_sql_endpoints(workspace_id: str) -> Dict[str, Dict]:
    """Return a map of displayName → {id, connectionString, server, database} for SQL endpoints in a workspace."""
    items = get_workspace_items(workspace_id)
    endpoints = {}
    for item in items:
        if item.get("type") == "SQLEndpoint":
            name = item.get("displayName", "")
            item_id = item.get("id", "")
            # SQL endpoint connection info lives in item properties
            props = item.get("properties", {})
            conn_str = props.get("connectionString", "")
            # Try to get server/database info from the SQLEndpoint properties
            endpoints[name] = {
                "id": item_id,
                "connectionString": conn_str,
            }
    return endpoints


def _build_dynamic_mappings(p_id: str, s_id: str) -> Dict[str, Any]:
    """Build artifact pairs and reference mappings dynamically from live workspace items.

    Matches primary ↔ secondary items by (displayName, type).
    Returns {
        "pairs": [...],          # artifact pairs with live existence status
        "ref_pairs": [...],      # reference mapping entries
        "primary_to_secondary": {...},  # id→id map
        "secondary_to_primary": {...},  # id→id map
        "unmatched": [...],      # primary items not in secondary
        "p_items": [...],        # filtered primary items
        "s_items": [...],        # filtered secondary items
    }
    """
    p_items = _filter_business_items(get_workspace_items(p_id))
    s_items = _filter_business_items(get_workspace_items(s_id))

    p_by_name: Dict[tuple, Dict] = {}
    for i in p_items:
        p_by_name[(i.get("type", ""), i["displayName"])] = i

    s_by_name: Dict[tuple, Dict] = {}
    for i in s_items:
        s_by_name[(i.get("type", ""), i["displayName"])] = i

    # Build pairs and id maps from live matches
    pairs = []
    primary_to_secondary = {p_id: s_id}  # always map workspace IDs
    secondary_to_primary = {s_id: p_id}

    for key, pi in p_by_name.items():
        si = s_by_name.get(key)
        pid = pi["id"]
        sid = si["id"] if si else ""
        pairs.append({
            "name": pi["displayName"],
            "type": pi.get("type", "?"),
            "primary_id": pid,
            "secondary_id": sid,
            "primary_exists": True,
            "secondary_exists": si is not None,
        })
        if si:
            primary_to_secondary[pid] = sid
            secondary_to_primary[sid] = pid

    # Reference mapping: workspace IDs + all matched pairs
    ref_pairs = [{"reference_type": "WorkspaceId", "primary_ref": p_id, "secondary_ref": s_id}]
    for p in pairs:
        if p["secondary_exists"]:
            ref_pairs.append({
                "reference_type": p["type"],
                "primary_ref": p["primary_id"],
                "secondary_ref": p["secondary_id"],
            })

    # Unmatched: primary items not in secondary
    unmatched = []
    for pi in p_items:
        key = (pi.get("type", ""), pi["displayName"])
        if key not in s_by_name:
            unmatched.append({
                "name": pi["displayName"],
                "type": pi.get("type", "?"),
                "primary_id": pi["id"],
            })

    return {
        "pairs": pairs,
        "ref_pairs": ref_pairs,
        "primary_to_secondary": primary_to_secondary,
        "secondary_to_primary": secondary_to_primary,
        "unmatched": unmatched,
        "p_items": p_items,
        "s_items": s_items,
    }


def _update_artifact_csv(item_name: str, item_type: str,
                         primary_id: str, secondary_id: str) -> None:
    """Update or add an entry in artifact_mapping.csv."""
    csv_path = os.path.join(os.path.dirname(__file__), "data", "artifact_mapping.csv")
    if not os.path.exists(csv_path):
        return
    try:
        rows = []
        updated = False
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            for row in reader:
                if row.get("artifact_type") == item_type and row.get("primary_artifact_id") == primary_id:
                    row["secondary_artifact_id"] = secondary_id
                    updated = True
                rows.append(row)
        if not updated:
            # Add new row
            rows.append({
                "primary_artifact_id": primary_id, "secondary_artifact_id": secondary_id,
                "artifact_type": item_type, "primary_name": item_name,
                "secondary_name": item_name,
            })
        if fieldnames:
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            logger.info(f"Updated artifact_mapping.csv: {item_name} ({item_type}) → {secondary_id[:8]}…")
    except Exception as e:
        logger.warning(f"Could not update artifact_mapping.csv for {item_name}: {e}")


def _build_connection_map(primary_ws_id: str, secondary_ws_id: str) -> Dict[str, str]:
    """Build a string replacement map: primary references → secondary references.
    Maps primary workspace ID, lakehouse IDs, and SQL endpoint server names
    to their secondary equivalents."""
    replacements = {}
    # Map workspace IDs
    replacements[primary_ws_id] = secondary_ws_id

    # Map item IDs (by matching displayName)
    p_items = get_workspace_items(primary_ws_id)
    s_items = get_workspace_items(secondary_ws_id)
    s_by_name: Dict[str, Dict] = {}
    for s in s_items:
        s_by_name[s.get("displayName", "")] = s

    for p_item in p_items:
        p_name = p_item.get("displayName", "")
        p_id = p_item.get("id", "")
        s_item = s_by_name.get(p_name)
        if s_item and p_id:
            s_id = s_item.get("id", "")
            if s_id and p_id != s_id:
                replacements[p_id] = s_id

    return replacements


def _rewrite_definition_parts(parts: List[Dict], replacements: Dict[str, str]) -> List[Dict]:
    """Rewrite definition parts by replacing primary references with secondary.
    Parts have 'path' and 'payload' (base64-encoded content).
    Strips '.platform' parts — these contain the source item identity and
    must not be sent when creating a new item in a different workspace."""
    rewritten = []
    for part in parts:
        # Skip .platform parts — they contain source-item identity
        part_path = part.get("path", "")
        if part_path == ".platform" or part_path.endswith("/.platform"):
            logger.debug(f"Stripping .platform part from definition")
            continue
        part_copy = dict(part)
        payload_b64 = part_copy.get("payload", "")
        if not payload_b64:
            rewritten.append(part_copy)
            continue
        try:
            payload_bytes = base64.b64decode(payload_b64)
            payload_text = payload_bytes.decode("utf-8")

            # Apply all replacements
            for old_val, new_val in replacements.items():
                payload_text = payload_text.replace(old_val, new_val)

            part_copy["payload"] = base64.b64encode(payload_text.encode("utf-8")).decode("ascii")
        except Exception as e:
            logger.warning(f"Could not rewrite part {part.get('path', '?')}: {e}")
        rewritten.append(part_copy)
    return rewritten


# ============================================================================
# LAKEHOUSE DATA REPLICATION — Notebook + Pipeline generation
# ============================================================================

def _replicate_lakehouse_tables(primary_ws_id: str, secondary_ws_id: str,
                                primary_lh_id: str, secondary_lh_id: str,
                                lakehouse_name: str) -> Dict[str, Any]:
    """List tables in the primary lakehouse (info-only for now)."""
    result = {"tables_found": 0, "tables_loaded": 0, "errors": []}
    try:
        tables_resp = fabric_api("GET", f"/workspaces/{primary_ws_id}/lakehouses/{primary_lh_id}/tables")
        tables = tables_resp.get("data", [])
        result["tables_found"] = len(tables)
        if not tables:
            logger.info(f"No tables found in primary lakehouse {lakehouse_name}")
            return result
        logger.info(f"Found {len(tables)} tables in primary lakehouse {lakehouse_name}: "
                     f"{[t.get('name') for t in tables]}")
    except Exception as e:
        logger.warning(f"Could not list tables for lakehouse {lakehouse_name}: {e}")
        result["errors"].append(f"List tables failed: {e}")
    return result


def _get_lakehouse_mappings() -> List[Dict[str, str]]:
    """Build a list of {name, primary_id, secondary_id} for each lakehouse pair."""
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return []
    p_items = get_workspace_items(p_id)
    s_items = get_workspace_items(s_id)
    p_lh = {i["displayName"]: i["id"] for i in p_items if i.get("type") == "Lakehouse"}
    s_lh = {i["displayName"]: i["id"] for i in s_items if i.get("type") == "Lakehouse"}
    mappings = []
    for name, pid in p_lh.items():
        sid = s_lh.get(name)
        if sid:
            mappings.append({"name": name, "primary_id": pid, "secondary_id": sid})
    return mappings


def _get_ml_mappings(include_missing: bool = False) -> List[Dict[str, str]]:
    """Build a list of {name, type, primary_id, secondary_id} for ML Model & Experiment pairs.

    Args:
        include_missing: If True, also include items that exist in primary but not secondary
                         (secondary_id will be None).
    """
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return []
    p_items = get_workspace_items(p_id)
    s_items = get_workspace_items(s_id)
    ml_types = ("MLModel", "MLExperiment")
    p_ml = {(i["type"], i["displayName"]): i["id"] for i in p_items if i.get("type") in ml_types}
    s_ml = {(i["type"], i["displayName"]): i["id"] for i in s_items if i.get("type") in ml_types}
    mappings = []
    for (item_type, name), pid in p_ml.items():
        sid = s_ml.get((item_type, name))
        if sid or include_missing:
            mappings.append({"name": name, "type": item_type, "primary_id": pid,
                             "secondary_id": sid, "missing": sid is None})
    return mappings


def _generate_registration_notebook_ipynb(primary_ws_id: str, secondary_ws_id: str,
                                           primary_lh_id: str, secondary_lh_id: str,
                                           lh_name: str) -> dict:
    """Generate a lightweight notebook that registers Delta folders as catalog tables.

    This notebook runs with its lakehouse as the DEFAULT so that
    CREATE SCHEMA / CREATE TABLE target the right catalog.
    It discovers which table folders exist under Tables/ and registers them.
    """
    cells = [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                f"# Resiliency & Recovery Table Registration: {lh_name}\n",
                "\n",
                "Auto-generated. Registers Delta folders as catalog tables after data copy.\n",
            ],
        },
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# ================================================================\n",
                f"# Register tables for {lh_name}\n",
                "# Runs with this lakehouse as default — CREATE TABLE targets it\n",
                "# ================================================================\n",
                "from notebookutils import mssparkutils\n",
                "import json\n",
                "\n",
                f'PRIMARY_WORKSPACE_ID = "{primary_ws_id}"\n',
                f'SECONDARY_WORKSPACE_ID = "{secondary_ws_id}"\n',
                f'PRIMARY_LH_ID = "{primary_lh_id}"\n',
                f'SECONDARY_LH_ID = "{secondary_lh_id}"\n',
                f'LH_NAME = "{lh_name}"\n',
                "\n",
                "ONELAKE = 'abfss://{ws}@onelake.dfs.fabric.microsoft.com/{lh}'\n",
                "\n",
                "def safe_ls(path):\n",
                "    try:\n",
                "        return mssparkutils.fs.ls(path)\n",
                "    except Exception:\n",
                "        return []\n",
                "\n",
                "# Discover schemas and tables from primary\n",
                "tables_base = ONELAKE.format(ws=PRIMARY_WORKSPACE_ID, lh=PRIMARY_LH_ID) + '/Tables'\n",
                "registered = 0\n",
                "errors = []\n",
                "\n",
                "for schema_item in safe_ls(tables_base):\n",
                "    schema_name = schema_item.name.rstrip('/')\n",
                "    if not schema_item.isDir or schema_name.startswith('_'):\n",
                "        continue\n",
                "    schema_path = f'{tables_base}/{schema_name}'\n",
                "    \n",
                "    # Check if this is a Delta table (flat) or a schema directory\n",
                "    children = safe_ls(schema_path)\n",
                "    is_delta = any(c.name.rstrip('/') == '_delta_log' for c in children)\n",
                "    \n",
                "    if is_delta:\n",
                "        # Flat table — register directly under dbo\n",
                "        table_name = schema_name\n",
                "        loc = f'Tables/{table_name}'\n",
                "        try:\n",
                "            spark.sql(f'CREATE TABLE IF NOT EXISTS `{table_name}` USING DELTA LOCATION \"{loc}\"')\n",
                "            print(f'  Registered: {table_name}')\n",
                "            registered += 1\n",
                "        except Exception as e:\n",
                "            print(f'  Error registering {table_name}: {e}')\n",
                "            errors.append(f'{table_name}: {e}')\n",
                "    else:\n",
                "        # Schema directory — create schema, then register each child table\n",
                "        try:\n",
                "            spark.sql(f'CREATE SCHEMA IF NOT EXISTS `{schema_name}`')\n",
                "            print(f'  Schema: {schema_name}')\n",
                "        except Exception as e:\n",
                "            print(f'  Schema {schema_name} error: {e}')\n",
                "        \n",
                "        for tbl_item in children:\n",
                "            tbl_name = tbl_item.name.rstrip('/')\n",
                "            if not tbl_item.isDir or tbl_name.startswith('_'):\n",
                "                continue\n",
                "            tbl_path = f'{schema_path}/{tbl_name}'\n",
                "            tbl_children = safe_ls(tbl_path)\n",
                "            if not any(c.name.rstrip('/') == '_delta_log' for c in tbl_children):\n",
                "                continue\n",
                "            loc = f'Tables/{schema_name}/{tbl_name}'\n",
                "            try:\n",
                "                spark.sql(f'CREATE TABLE IF NOT EXISTS `{schema_name}`.`{tbl_name}` USING DELTA LOCATION \"{loc}\"')\n",
                "                print(f'  Registered: {schema_name}.{tbl_name}')\n",
                "                registered += 1\n",
                "            except Exception as e:\n",
                "                print(f'  Error: {schema_name}.{tbl_name}: {e}')\n",
                "                errors.append(f'{schema_name}.{tbl_name}: {e}')\n",
                "\n",
                "print(f'\\nRegistered {registered} tables, {len(errors)} errors')\n",
                "mssparkutils.notebook.exit(json.dumps({'registered': registered, 'errors': len(errors)}))\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
    ]

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernel_info": {"name": "synapse_pyspark"},
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "language_info": {"name": "python"},
            "trident": {
                "lakehouse": {
                    "default_lakehouse": secondary_lh_id,
                    "default_lakehouse_name": lh_name,
                    "default_lakehouse_workspace_id": secondary_ws_id,
                    "known_lakehouses": [{"id": secondary_lh_id}],
                },
            },
        },
        "cells": cells,
    }


def _generate_per_lh_sync_notebook_ipynb(primary_ws_id: str, secondary_ws_id: str,
                                          primary_lh_id: str, secondary_lh_id: str,
                                          lh_name: str,
                                          sync_engine: str = "fast_copy") -> dict:
    """Generate a self-contained PySpark notebook that syncs ONE lakehouse pair.

    Supports two sync engines selectable via SYNC_ENGINE:

      fast_copy (default)
        Uses notebookutils.fs.cp for both full and incremental sync.
        - Full: cp entire table tree to secondary, reset _last_checkpoint
        - Incremental: walk tree, copy only files with modifyTime > last_sync_ms,
          copy only new _delta_log commit entries, reset _last_checkpoint.
        State stored as JSON in Files/_bcdr_sync_state/ on secondary (no Spark needed).
        No CDF requirement. Works on any Delta or non-Delta table layout.

      spark_cdf (legacy)
        Uses Spark + Delta Change Data Feed for incremental upsert/delete.
        Requires CDF to be enabled on primary tables.
        State stored in Files/_bcdr_sync_control Delta table.

    The notebook's default_lakehouse is set to the secondary lakehouse for this
    pair, so Spark catalog operations stay scoped to the correct lakehouse.
    """

    cells = [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                f"# Resiliency & Recovery Data Sync: {lh_name}\n",
                "\n",
                "**Auto-generated** — Syncs tables + files for this lakehouse pair.\n",
                f"Primary → Secondary via OneLake abfss paths.\n",
                f"Engine: **{sync_engine}**\n",
            ],
        },
        # ── Config ──────────────────────────────────────────────────────────
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "from notebookutils import mssparkutils\n",
                "from pyspark.sql import functions as F\n",
                "from delta.tables import DeltaTable\n",
                "from datetime import datetime\n",
                "import json, traceback, time\n",
                "\n",
                f'PRIMARY_WORKSPACE_ID = "{primary_ws_id}"\n',
                f'SECONDARY_WORKSPACE_ID = "{secondary_ws_id}"\n',
                f'PRIMARY_LH_ID = "{primary_lh_id}"\n',
                f'SECONDARY_LH_ID = "{secondary_lh_id}"\n',
                f'LH_NAME = "{lh_name}"\n',
                "\n",
                'ONELAKE_BASE = "abfss://{ws_id}@onelake.dfs.fabric.microsoft.com/{lh_id}"\n',
                f'SYNC_ENGINE = "{sync_engine}"  # "fast_copy" | "spark_cdf"\n',
                "SYNC_MODE = 'auto'   # 'auto' | 'full'  (auto = incremental if state exists)\n",
                "SYNC_FILES = True\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
        # ── Shared helpers ───────────────────────────────────────────────────
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# ── Shared path helpers ──────────────────────────────────────────────────\n",
                "def lh_base(ws_id, lh_id):\n",
                "    return ONELAKE_BASE.format(ws_id=ws_id, lh_id=lh_id)\n",
                "\n",
                "def tables_root(ws_id, lh_id):\n",
                '    return f"{lh_base(ws_id, lh_id)}/Tables"\n',
                "\n",
                "def files_root(ws_id, lh_id):\n",
                '    return f"{lh_base(ws_id, lh_id)}/Files"\n',
                "\n",
                "def safe_ls(path):\n",
                "    try:\n",
                "        return mssparkutils.fs.ls(path)\n",
                "    except Exception:\n",
                "        return []\n",
                "\n",
                "def is_delta_table(path):\n",
                "    try:\n",
                '        return any(f.name.rstrip("/") == "_delta_log" for f in mssparkutils.fs.ls(path))\n',
                "    except Exception:\n",
                "        return False\n",
                "\n",
                "def discover_tables(ws_id, lh_id):\n",
                "    root = tables_root(ws_id, lh_id)\n",
                "    found = []\n",
                "    for item in safe_ls(root):\n",
                '        name = item.name.rstrip("/")\n',
                '        if not item.isDir or name.startswith("_"):\n',
                "            continue\n",
                '        item_path = f"{root}/{name}"\n',
                "        if is_delta_table(item_path):\n",
                "            found.append((name, name))\n",
                "        else:\n",
                "            for sub in safe_ls(item_path):\n",
                '                sub_name = sub.name.rstrip("/")\n',
                '                if sub.isDir and not sub_name.startswith("_") \\\n',
                '                   and is_delta_table(f"{item_path}/{sub_name}"):\n',
                '                    found.append((f"{name}/{sub_name}", f"{name}.{sub_name}"))\n',
                "    return found\n",
                "\n",
                "print('Shared helpers loaded.')\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
        # ── Fast-copy engine ─────────────────────────────────────────────────
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# ════════════════════════════════════════════════════════════════════════\n",
                "# FAST-COPY ENGINE  (notebookutils.fs.cp — server-side, no Spark needed)\n",
                "# ════════════════════════════════════════════════════════════════════════\n",
                "\n",
                "# State file: Files/_bcdr_sync_state/<lh_name>.json on SECONDARY\n",
                "# Stores {table_key: {last_sync_ms, last_sync_iso}} — no Spark required.\n",
                "_FC_STATE_ROOT = f\"{files_root(SECONDARY_WORKSPACE_ID, SECONDARY_LH_ID)}/_bcdr_sync_state\"\n",
                "_FC_STATE_FILE  = f\"{_FC_STATE_ROOT}/{LH_NAME}.json\"\n",
                "\n",
                "def _fc_load_state():\n",
                "    try:\n",
                "        raw = mssparkutils.fs.head(_FC_STATE_FILE, 1048576)  # read up to 1 MB\n",
                "        return json.loads(raw) if raw else {}\n",
                "    except Exception:\n",
                "        return {}\n",
                "\n",
                "def _fc_save_state(state):\n",
                "    try:\n",
                "        mssparkutils.fs.put(_FC_STATE_FILE, json.dumps(state, indent=2), overwrite=True)\n",
                "    except Exception as e:\n",
                "        print(f'  Warning: could not save fast-copy state: {e}')\n",
                "\n",
                "def _fc_get_last_ms(state, table_key):\n",
                "    \"\"\"Return last sync epoch-ms for a table, or 0 if never synced.\"\"\"\n",
                "    return state.get(table_key, {}).get('last_sync_ms', 0)\n",
                "\n",
                "def _incremental_cp(src_dir, dst_dir, since_ms, stats):\n",
                "    \"\"\"Recursively copy files with modifyTime > since_ms from src_dir → dst_dir.\n",
                "    since_ms=0 copies everything (full sync).\"\"\"\n",
                "    for item in safe_ls(src_dir):\n",
                "        name = item.name.rstrip('/')\n",
                "        src  = f\"{src_dir}/{name}\"\n",
                "        dst  = f\"{dst_dir}/{name}\"\n",
                "        if item.isDir:\n",
                "            _incremental_cp(src, dst, since_ms, stats)\n",
                "        else:\n",
                "            if since_ms > 0 and item.modifyTime <= since_ms:\n",
                "                stats['skipped'] += 1  # V1 data already on secondary\n",
                "            else:\n",
                "                try:\n",
                "                    mssparkutils.fs.cp(src, dst, False)\n",
                "                    stats['copied'] += 1\n",
                "                except Exception as e:\n",
                "                    stats['errors'].append(f'{name}: {e}')\n",
                "\n",
                "def fast_copy_full(src_root, dst_root, table_key):\n",
                "    \"\"\"Full fast-copy: copy entire table tree then reset Delta checkpoint.\"\"\"\n",
                "    print(f'    FAST-COPY FULL: {table_key}')\n",
                "    stats = {'copied': 0, 'skipped': 0, 'errors': []}\n",
                "    # Copy data files (skip _delta_log — handled separately)\n",
                "    for item in safe_ls(src_root):\n",
                "        name = item.name.rstrip('/')\n",
                "        if name == '_delta_log':\n",
                "            continue\n",
                "        _incremental_cp(f\"{src_root}/{name}\", f\"{dst_root}/{name}\", 0, stats)\n",
                "    # Copy full delta log\n",
                "    for lf in safe_ls(f\"{src_root}/_delta_log\"):\n",
                "        fname = lf.name.rstrip('/')\n",
                "        if fname != '_last_checkpoint':\n",
                "            try:\n",
                "                mssparkutils.fs.cp(f\"{src_root}/_delta_log/{fname}\",\n",
                "                                   f\"{dst_root}/_delta_log/{fname}\", False)\n",
                "                stats['copied'] += 1\n",
                "            except Exception as e:\n",
                "                stats['errors'].append(f'_delta_log/{fname}: {e}')\n",
                "    mssparkutils.fs.put(f\"{dst_root}/_delta_log/_last_checkpoint\", '', overwrite=True)\n",
                "    stats['delta_version'] = _get_max_delta_version(src_root)\n",
                "    print(f'      {stats[\"copied\"]} files copied, {len(stats[\"errors\"])} errors')\n",
                "    return stats\n",
                "\n",
                "def _get_max_delta_version(table_root):\n",
                "    \"\"\"Return the highest Delta commit version from _delta_log/.\"\"\"\n",
                "    max_ver = -1\n",
                "    for lf in safe_ls(f\"{table_root}/_delta_log\"):\n",
                "        fname = lf.name.rstrip('/')\n",
                "        if fname.endswith('.json') and not fname.startswith('_'):\n",
                "            try:\n",
                "                ver = int(fname.replace('.json', ''))\n",
                "                max_ver = max(max_ver, ver)\n",
                "            except ValueError:\n",
                "                pass\n",
                "    return max_ver\n",
                "\n",
                "def fast_copy_incremental(src_root, dst_root, table_key, since_ms, last_version=-1):\n",
                "    \"\"\"Delta-log-driven incremental: O(new commits) not O(total files).\n",
                "\n",
                "    Instead of listing all parquet files (slow with 1000s of files),\n",
                "    reads only the new Delta commit JSONs, parses them for 'add' actions\n",
                "    to identify exactly which files are new, then copies only those.\n",
                "    Falls back to file-listing approach if Delta log parsing fails.\n",
                "    \"\"\"\n",
                "    print(f'    FAST-COPY INCR: {table_key} (delta-log v{last_version}→?)')\n",
                "    stats = {'copied': 0, 'skipped': 0, 'errors': [], 'method': 'delta_log'}\n",
                "    log_dir = f\"{src_root}/_delta_log\"\n",
                "\n",
                "    # ── Step 1: read new Delta log commits to find new data files ──\n",
                "    new_files = set()\n",
                "    max_version = last_version\n",
                "    log_new = 0\n",
                "    fallback = False\n",
                "\n",
                "    try:\n",
                "        log_entries = safe_ls(log_dir)\n",
                "    except Exception:\n",
                "        fallback = True\n",
                "        log_entries = []\n",
                "\n",
                "    if not fallback and last_version >= 0:\n",
                "        for lf in log_entries:\n",
                "            fname = lf.name.rstrip('/')\n",
                "            # Only numbered JSON commits (skip checkpoints, _last_checkpoint)\n",
                "            if not fname.endswith('.json') or fname.startswith('_'):\n",
                "                # Copy new checkpoint parquet files\n",
                "                if fname.endswith('.checkpoint.parquet') and lf.modifyTime > since_ms:\n",
                "                    try:\n",
                "                        mssparkutils.fs.cp(f\"{log_dir}/{fname}\",\n",
                "                            f\"{dst_root}/_delta_log/{fname}\", False)\n",
                "                        stats['copied'] += 1\n",
                "                    except Exception as e:\n",
                "                        stats['errors'].append(f'_delta_log/{fname}: {e}')\n",
                "                continue\n",
                "            try:\n",
                "                ver = int(fname.replace('.json', ''))\n",
                "            except ValueError:\n",
                "                continue\n",
                "            if ver <= last_version:\n",
                "                continue  # already synced\n",
                "            max_version = max(max_version, ver)\n",
                "\n",
                "            # Parse commit JSON for 'add' actions → exact new file paths\n",
                "            try:\n",
                "                raw = mssparkutils.fs.head(f\"{log_dir}/{fname}\", 10485760)\n",
                "                for line in raw.strip().split(chr(10)):\n",
                "                    if not line.strip():\n",
                "                        continue\n",
                "                    action = json.loads(line)\n",
                "                    if 'add' in action:\n",
                "                        new_files.add(action['add']['path'])\n",
                "            except Exception as e:\n",
                "                print(f'      Delta log parse failed ({fname}): {e}')\n",
                "                fallback = True\n",
                "                break\n",
                "\n",
                "            # Copy this log entry to secondary\n",
                "            try:\n",
                "                mssparkutils.fs.cp(f\"{log_dir}/{fname}\",\n",
                "                    f\"{dst_root}/_delta_log/{fname}\", False)\n",
                "                log_new += 1\n",
                "                stats['copied'] += 1\n",
                "            except Exception as e:\n",
                "                stats['errors'].append(f'_delta_log/{fname}: {e}')\n",
                "    else:\n",
                "        fallback = True  # first run or log unreadable\n",
                "\n",
                "    # ── Step 2: copy data files ──\n",
                "    if not fallback:\n",
                "        # Delta-log path: copy only files identified from new commits\n",
                "        for rel_path in sorted(new_files):\n",
                "            try:\n",
                "                mssparkutils.fs.cp(f\"{src_root}/{rel_path}\",\n",
                "                    f\"{dst_root}/{rel_path}\", False)\n",
                "                stats['copied'] += 1\n",
                "            except Exception as e:\n",
                "                stats['errors'].append(f'{rel_path}: {e}')\n",
                "        print(f'      [delta-log] {log_new} new commits (v{last_version}→v{max_version}), '\n",
                "              f'{len(new_files)} new data files, {stats[\"copied\"]} total copied')\n",
                "    else:\n",
                "        # Fallback: list all files and filter by modifyTime\n",
                "        stats['method'] = 'file_listing_fallback'\n",
                "        print(f'      [fallback] scanning all files by modifyTime...')\n",
                "        for item in safe_ls(src_root):\n",
                "            name = item.name.rstrip('/')\n",
                "            if name == '_delta_log':\n",
                "                continue\n",
                "            src = f\"{src_root}/{name}\"\n",
                "            dst = f\"{dst_root}/{name}\"\n",
                "            if item.isDir:\n",
                "                _incremental_cp(src, dst, since_ms, stats)\n",
                "            else:\n",
                "                if item.modifyTime <= since_ms:\n",
                "                    stats['skipped'] += 1\n",
                "                else:\n",
                "                    try:\n",
                "                        mssparkutils.fs.cp(src, dst, False)\n",
                "                        stats['copied'] += 1\n",
                "                    except Exception as e:\n",
                "                        stats['errors'].append(f'{name}: {e}')\n",
                "        # Also copy new delta log entries by modifyTime\n",
                "        for lf in safe_ls(f\"{src_root}/_delta_log\"):\n",
                "            fname = lf.name.rstrip('/')\n",
                "            if fname == '_last_checkpoint' or lf.modifyTime <= since_ms:\n",
                "                continue\n",
                "            try:\n",
                "                mssparkutils.fs.cp(f\"{src_root}/_delta_log/{fname}\",\n",
                "                                   f\"{dst_root}/_delta_log/{fname}\", False)\n",
                "                log_new += 1\n",
                "                stats['copied'] += 1\n",
                "            except Exception as e:\n",
                "                stats['errors'].append(f'_delta_log/{fname}: {e}')\n",
                "        max_version = _get_max_delta_version(src_root)\n",
                "        print(f'      [fallback] {stats[\"copied\"]} copied, {stats[\"skipped\"]} skipped, '\n",
                "              f'{log_new} log entries')\n",
                "\n",
                "    # Reset checkpoint so Delta discovers new commits\n",
                "    mssparkutils.fs.put(f\"{dst_root}/_delta_log/_last_checkpoint\", '', overwrite=True)\n",
                "    stats['delta_version'] = max_version\n",
                "    stats['new_data_files'] = len(new_files) if not fallback else -1\n",
                "    return stats\n",
                "\n",
                "print('Fast-copy engine loaded.')\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
        # ── Spark / CDF engine (legacy) ──────────────────────────────────────
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# ════════════════════════════════════════════════════════════════════════\n",
                "# SPARK / CDF ENGINE  (legacy — Delta CDF + Spark upsert/delete)\n",
                "# ════════════════════════════════════════════════════════════════════════\n",
                "\n",
                "def get_delta_version(path):\n",
                "    try:\n",
                '        detail = spark.sql(f"DESCRIBE HISTORY delta.`{path}` LIMIT 1")\n',
                '        return detail.collect()[0]["version"]\n',
                "    except Exception:\n",
                "        return -1\n",
                "\n",
                "def get_sync_state(control_path, lh_name, table_key):\n",
                "    try:\n",
                "        if DeltaTable.isDeltaTable(spark, control_path):\n",
                '            df = spark.read.format("delta").load(control_path)\n',
                "            row = df.filter(\n",
                '                (F.col("lakehouse") == lh_name) & (F.col("table_name") == table_key)\n',
                '            ).select("last_version").collect()\n',
                "            if row:\n",
                '                return row[0]["last_version"]\n',
                "    except Exception:\n",
                "        pass\n",
                "    return -1\n",
                "\n",
                "def update_sync_state(control_path, lh_name, table_key, version):\n",
                "    now = datetime.now().isoformat()\n",
                "    new_row = spark.createDataFrame([\n",
                "        (lh_name, table_key, int(version), now)\n",
                '    ], ["lakehouse", "table_name", "last_version", "last_sync"])\n',
                "    try:\n",
                "        if DeltaTable.isDeltaTable(spark, control_path):\n",
                "            dt = DeltaTable.forPath(spark, control_path)\n",
                '            dt.alias("t").merge(new_row.alias("s"),\n',
                '                "t.lakehouse = s.lakehouse AND t.table_name = s.table_name")\\\n',
                "                .whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()\n",
                "        else:\n",
                '            new_row.write.format("delta").mode("overwrite").save(control_path)\n',
                "    except Exception as e:\n",
                '        print(f"  Warning: sync state update failed: {e}")\n',
                "\n",
                "def full_sync_table(src_path, dst_path, display_name):\n",
                '    print(f"    SPARK FULL: {display_name}")\n',
                '    df = spark.read.format("delta").load(src_path)\n',
                "    row_count = df.count()\n",
                '    df.write.format("delta").mode("overwrite")\\\n',
                '        .option("overwriteSchema", "true").save(dst_path)\n',
                '    print(f"      Wrote {row_count} rows")\n',
                "    return row_count\n",
                "\n",
                "def incremental_sync_table(src_path, dst_path, display_name, last_version):\n",
                '    print(f"    SPARK INCR: {display_name} (from v{last_version + 1})")\n',
                "    try:\n",
                '        changes = spark.read.format("delta")\\\n',
                '            .option("readChangeFeed", "true")\\\n',
                '            .option("startingVersion", last_version + 1).load(src_path)\n',
                "        change_count = changes.count()\n",
                "        if change_count == 0:\n",
                '            print("      No changes"); return 0\n',
                '        upserts = changes.filter(F.col("_change_type").isin("insert","update_postimage"))\\\n',
                '            .drop("_change_type","_commit_version","_commit_timestamp")\n',
                '        deletes  = changes.filter(F.col("_change_type") == "delete")\\\n',
                '            .drop("_change_type","_commit_version","_commit_timestamp")\n',
                "        if not DeltaTable.isDeltaTable(spark, dst_path):\n",
                '            upserts.write.format("delta").mode("overwrite").save(dst_path)\n',
                "            return upserts.count()\n",
                "        dst_dt = DeltaTable.forPath(spark, dst_path)\n",
                "        cols = upserts.columns\n",
                '        id_cols = [c for c in cols if c.lower() in\n',
                '                   ("id","key","pk","row_id","claim_id","policy_id")]\n',
                "        if not id_cols: id_cols = [cols[0]] if cols else []\n",
                '        cond = " AND ".join([f"t.`{c}` = s.`{c}`" for c in id_cols])\n',
                "        if upserts.count() > 0:\n",
                '            dst_dt.alias("t").merge(upserts.alias("s"), cond)\\\n',
                "                .whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()\n",
                "        if deletes.count() > 0:\n",
                '            dst_dt.alias("t").merge(deletes.alias("s"), cond).whenMatchedDelete().execute()\n',
                "        return change_count\n",
                "    except Exception as e:\n",
                "        err = str(e)\n",
                '        if any(k in err for k in ["CHANGE_DATA_FEED","not enabled","VERSION_NOT_EXIST","not recreatable"]):\n',
                '            print("      CDF unavailable — falling back to full spark sync")\n',
                "            return full_sync_table(src_path, dst_path, display_name)\n",
                "        raise\n",
                "\n",
                "def enable_cdf(table_path, display_name):\n",
                "    try:\n",
                '        detail = spark.sql(f"DESCRIBE DETAIL delta.`{table_path}`").collect()\n',
                "        if detail:\n",
                "            props = detail[0]['properties'] or {}\n",
                "            if props.get('delta.enableChangeDataFeed') == 'true':\n",
                "                return False\n",
                '        spark.sql(f"ALTER TABLE delta.`{table_path}` SET TBLPROPERTIES (\'delta.enableChangeDataFeed\' = \'true\')")\n',
                '        print(f"      CDF enabled: {display_name}")\n',
                "        return True\n",
                "    except Exception as e:\n",
                '        print(f"      CDF enable warning for {display_name}: {e}")\n',
                "        return False\n",
                "\n",
                "print('Spark/CDF engine loaded.')\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
        # ── Files section (shared) ───────────────────────────────────────────
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# ── Files section — always uses fast-copy (no Spark delta semantics needed) ──\n",
                "def sync_files_section(ws_src, lh_src, ws_dst, lh_dst, lh_name, since_ms=0):\n",
                "    src_root = files_root(ws_src, lh_src)\n",
                "    dst_root = files_root(ws_dst, lh_dst)\n",
                "    top_items = safe_ls(src_root)\n",
                "    if not top_items:\n",
                '        print(f"  No files in {lh_name}/Files"); return 0\n',
                "    file_count = 0\n",
                "    for item in top_items:\n",
                "        name = item.name.rstrip('/')\n",
                "        if name.startswith('_bcdr_sync'):  # skip our own control dirs\n",
                "            continue\n",
                '        src_path = f"{src_root}/{name}"\n',
                '        dst_path = f"{dst_root}/{name}"\n',
                "        try:\n",
                '            stats = {"copied": 0, "skipped": 0, "errors": []}\n',
                "            _incremental_cp(src_path, dst_path, since_ms, stats)\n",
                '            print(f"    Files/{name}: {stats[\'copied\']} copied, {stats[\'skipped\']} skipped")\n',
                "            file_count += 1\n",
                "        except Exception as e:\n",
                '            print(f"    Error copying {name}: {e}")\n',
                "    return file_count\n",
                "\n",
                "print('Files sync loaded.')\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
        # ── Main execution ───────────────────────────────────────────────────
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# ════════════════════════════════════════════════════════════════════════\n",
                f"# MAIN — {lh_name}  (engine={sync_engine})\n",
                "# ════════════════════════════════════════════════════════════════════════\n",
                "print('=' * 60)\n",
                f"print('Lakehouse: {lh_name}  |  Engine: {sync_engine}')\n",
                "print('=' * 60)\n",
                "\n",
                "total_tables = total_files = total_cdf = 0\n",
                "total_rows = total_copied = total_skipped = 0\n",
                "errors = []\n",
                "\n",
                "tables = discover_tables(PRIMARY_WORKSPACE_ID, PRIMARY_LH_ID)\n",
                "print(f'  Discovered {len(tables)} tables in primary')\n",
                "\n",
                "# ── Fast-copy path ───────────────────────────────────────────────────────\n",
                "if SYNC_ENGINE == 'fast_copy':\n",
                "    fc_state = _fc_load_state()\n",
                "\n",
                "    # Corruption gate: refuse to sync if previous run failed integrity check\n",
                "    prev_integrity = fc_state.get('_integrity', {})\n",
                "    if prev_integrity.get('status') == 'fail' and SYNC_MODE != 'full':\n",
                '        msg = ("BLOCKED: previous sync failed integrity verification at "\n',
                "               f\"{prev_integrity.get('checked_at','?')}. \"\n",
                '               "Run with SYNC_MODE=full to force re-sync, or fix primary data first.")\n',
                "        print(f'\\n  ⛔ {msg}')\n",
                "        mssparkutils.notebook.exit(json.dumps({\n",
                "            'lakehouse': LH_NAME, 'engine': SYNC_ENGINE,\n",
                "            'status': 'blocked_integrity', 'message': msg,\n",
                "            'previous_integrity': prev_integrity,\n",
                "        }))\n",
                "\n",
                "    now_ms   = int(time.time() * 1000)\n",
                "\n",
                "    for rel_path, display_name in tables:\n",
                "        try:\n",
                '            src = f"{tables_root(PRIMARY_WORKSPACE_ID, PRIMARY_LH_ID)}/{rel_path}"\n',
                '            dst = f"{tables_root(SECONDARY_WORKSPACE_ID, SECONDARY_LH_ID)}/{rel_path}"\n',
                "            last_ms = _fc_get_last_ms(fc_state, display_name)\n",
                "            last_ver = fc_state.get(display_name, {}).get('last_delta_version', -1)\n",
                '            print(f"\\n  >> {display_name}  (last_sync_ms={last_ms}, delta_ver={last_ver}, mode={SYNC_MODE})")\n',
                "\n",
                "            if SYNC_MODE == 'full' or last_ms == 0:\n",
                "                stats = fast_copy_full(src, dst, display_name)\n",
                "            else:\n",
                "                stats = fast_copy_incremental(src, dst, display_name, last_ms, last_ver)\n",
                "\n",
                "            # Record successful sync timestamp + delta version\n",
                "            fc_state[display_name] = {\n",
                "                'last_sync_ms':  now_ms,\n",
                "                'last_sync_iso': datetime.utcnow().isoformat() + 'Z',\n",
                "                'last_delta_version': stats.get('delta_version', -1),\n",
                "            }\n",
                "            total_tables  += 1\n",
                "            total_copied  += stats.get('copied', 0)\n",
                "            total_skipped += stats.get('skipped', 0)\n",
                "            errors.extend(stats.get('errors', []))\n",
                "        except Exception as e:\n",
                '            err = f"{LH_NAME}/{display_name}: {e}"\n',
                '            print(f"    ERROR: {err}")\n',
                "            errors.append(err)\n",
                "\n",
                "    _fc_save_state(fc_state)  # persist watermarks\n",
                "    since_for_files = min((fc_state.get(d, {}).get('last_sync_ms', 0)\n",
                "                           for _, d in tables), default=0) if tables else 0\n",
                "\n",
                "# ── Spark / CDF path ─────────────────────────────────────────────────────\n",
                "else:\n",
                '    control_path = f"{files_root(SECONDARY_WORKSPACE_ID, SECONDARY_LH_ID)}/_bcdr_sync_control"\n',
                "\n",
                "    # Enable CDF on primary tables\n",
                '    print("\\n  Ensuring CDF enabled on primary tables...")\n',
                "    for rel_path, display_name in tables:\n",
                '        src = f"{tables_root(PRIMARY_WORKSPACE_ID, PRIMARY_LH_ID)}/{rel_path}"\n',
                "        if enable_cdf(src, display_name):\n",
                "            total_cdf += 1\n",
                "\n",
                "    for rel_path, display_name in tables:\n",
                "        try:\n",
                '            src = f"{tables_root(PRIMARY_WORKSPACE_ID, PRIMARY_LH_ID)}/{rel_path}"\n',
                '            dst = f"{tables_root(SECONDARY_WORKSPACE_ID, SECONDARY_LH_ID)}/{rel_path}"\n',
                "            current_ver = get_delta_version(src)\n",
                "            last_synced = get_sync_state(control_path, LH_NAME, display_name)\n",
                '            print(f"\\n  >> {display_name}  (src v{current_ver}, synced v{last_synced})")\n',
                "\n",
                "            if SYNC_MODE == 'full' or last_synced < 0:\n",
                "                rows = full_sync_table(src, dst, display_name)\n",
                "            elif current_ver <= last_synced:\n",
                '                print("      Up to date"); rows = 0\n',
                "            else:\n",
                "                rows = incremental_sync_table(src, dst, display_name, last_synced)\n",
                "\n",
                "            if current_ver >= 0:\n",
                "                update_sync_state(control_path, LH_NAME, display_name, current_ver)\n",
                "            total_tables += 1\n",
                "            total_rows   += (rows or 0)\n",
                "        except Exception as e:\n",
                '            err = f"{LH_NAME}/{display_name}: {e}"\n',
                '            print(f"    ERROR: {err}"); errors.append(err)\n',
                "\n",
                "    since_for_files = 0  # spark path always does full file copy\n",
                "\n",
                "# ── Files ────────────────────────────────────────────────────────────────\n",
                "if SYNC_FILES:\n",
                '    print("\\n  --- Files Section ---")\n',
                "    fc = sync_files_section(\n",
                "        PRIMARY_WORKSPACE_ID, PRIMARY_LH_ID,\n",
                "        SECONDARY_WORKSPACE_ID, SECONDARY_LH_ID,\n",
                "        LH_NAME, since_ms=since_for_files\n",
                "    )\n",
                "    total_files += fc\n",
                '    print(f"  File folders: {fc}")\n',
                "\n",
                "# ══════════════════════════════════════════════════════════════════════════\n",
                "# POST-SYNC INTEGRITY VERIFICATION\n",
                "# Uses Spark to read both primary & secondary tables, compares row counts,\n",
                "# and catches corrupt parquet (Spark throws on decode failure).\n",
                "# ══════════════════════════════════════════════════════════════════════════\n",
                "verification_results = []\n",
                "integrity_ok = True\n",
                "\n",
                "def _get_delta_row_count(path):\n",
                "    \"\"\"Get row count from Delta metadata (O(1)) instead of full table scan.\n",
                "    Uses DESCRIBE DETAIL for fast metadata lookup. Falls back to .count()\n",
                "    only if metadata stats are not available.\"\"\"\n",
                "    try:\n",
                "        detail = spark.sql(f'DESCRIBE DETAIL delta.`{path}`').collect()[0]\n",
                "        num_records = detail['numRecords']\n",
                "        if num_records is not None and num_records >= 0:\n",
                "            return int(num_records), 'metadata'\n",
                "    except Exception:\n",
                "        pass  # fall through to .count()\n",
                "    count = spark.read.format('delta').load(path).count()\n",
                "    return count, 'scan'\n",
                "\n",
                "if tables and total_tables > 0:\n",
                '    print("\\n  --- Post-Sync Integrity Verification ---")\n',
                "    for rel_path, display_name in tables:\n",
                '        src = f"{tables_root(PRIMARY_WORKSPACE_ID, PRIMARY_LH_ID)}/{rel_path}"\n',
                '        dst = f"{tables_root(SECONDARY_WORKSPACE_ID, SECONDARY_LH_ID)}/{rel_path}"\n',
                "        v = {'table': display_name, 'status': 'unknown'}\n",
                "\n",
                "        # ── Quick check: compare Delta versions (free metadata read) ──\n",
                "        src_ver = _get_max_delta_version(src)\n",
                "        dst_ver = _get_max_delta_version(dst)\n",
                "        v['primary_version'] = src_ver\n",
                "        v['secondary_version'] = dst_ver\n",
                "\n",
                "        # ── Row count: use metadata first, fall back to scan ──\n",
                "        try:\n",
                "            src_count, src_method = _get_delta_row_count(src)\n",
                "            v['primary_rows'] = src_count\n",
                "            v['primary_count_method'] = src_method\n",
                "        except Exception as e:\n",
                '            v["status"] = "primary_read_error"\n',
                '            v["error"]  = str(e)[:200]\n',
                '            print(f"    {display_name}: PRIMARY READ ERROR — {e}")\n',
                "            integrity_ok = False\n",
                "            verification_results.append(v)\n",
                "            continue\n",
                "        try:\n",
                "            dst_count, dst_method = _get_delta_row_count(dst)\n",
                "            v['secondary_rows'] = dst_count\n",
                "            v['secondary_count_method'] = dst_method\n",
                "        except Exception as e:\n",
                '            v["status"] = "secondary_read_error"\n',
                '            v["error"]  = str(e)[:200]\n',
                '            print(f"    {display_name}: SECONDARY READ ERROR (corrupt?) — {e}")\n',
                "            integrity_ok = False\n",
                "            verification_results.append(v)\n",
                "            continue\n",
                "\n",
                "        variance = abs(src_count - dst_count)\n",
                "        pct = (variance / max(src_count, 1)) * 100\n",
                "        ver_match = src_ver == dst_ver\n",
                "        if variance == 0 and ver_match:\n",
                '            v["status"] = "pass"\n',
                '            print(f"    {display_name}: {src_count} rows, v{src_ver} ✓ [{src_method}]")\n',
                "        elif variance == 0:\n",
                '            v["status"] = "info"\n',
                '            v["note"] = f"version mismatch: {src_ver} vs {dst_ver}"\n',
                '            print(f"    {display_name}: {src_count} rows OK but version mismatch (src=v{src_ver} dst=v{dst_ver})")\n',
                "        elif pct <= 1.0:\n",
                '            v["status"] = "info"\n',
                '            v["variance"] = variance\n',
                '            print(f"    {display_name}: primary={src_count} secondary={dst_count} (Δ{variance}, {pct:.1f}% — within tolerance) [{src_method}]")\n',
                "        else:\n",
                '            v["status"] = "fail"\n',
                '            v["variance"] = variance\n',
                '            print(f"    {display_name}: MISMATCH primary={src_count} secondary={dst_count} (Δ{variance}, {pct:.1f}%) [{src_method}]")\n',
                "            integrity_ok = False\n",
                "        verification_results.append(v)\n",
                "\n",
                "# Update sync state with integrity result\n",
                "if SYNC_ENGINE == 'fast_copy':\n",
                "    fc_state = _fc_load_state()\n",
                "    fc_state['_integrity'] = {\n",
                "        'status': 'pass' if integrity_ok else 'fail',\n",
                "        'checked_at': datetime.utcnow().isoformat() + 'Z',\n",
                "        'tables_checked': len(verification_results),\n",
                "        'tables_passed': sum(1 for v in verification_results if v['status'] in ('pass','info')),\n",
                "        'tables_failed': sum(1 for v in verification_results if v['status'] not in ('pass','info','unknown')),\n",
                "        'details': verification_results,\n",
                "    }\n",
                "    _fc_save_state(fc_state)\n",
                "\n",
                "if not integrity_ok:\n",
                '    print("\\n  ⚠ INTEGRITY CHECK FAILED — review verification_results above")\n',
                "    errors.append('integrity_verification_failed')\n",
                "\n",
                "# ── Summary & catalog registration ───────────────────────────────────────\n",
                "result = {\n",
                "    'lakehouse':   LH_NAME,\n",
                "    'engine':      SYNC_ENGINE,\n",
                "    'tables':      total_tables,\n",
                "    'rows':        total_rows,\n",
                "    'files_copied': total_copied,\n",
                "    'files_skipped': total_skipped,\n",
                "    'files':        total_files,\n",
                "    'cdf_enabled':  total_cdf,\n",
                "    'errors':       len(errors),\n",
                "    'integrity_ok': integrity_ok,\n",
                "    'verification': verification_results,\n",
                "}\n",
                "print(f'\\n  Done: {total_tables} tables, {total_rows} rows (spark) / '\n",
                "      f'{total_copied} copied + {total_skipped} skipped (fast-copy), '\n",
                "      f'{len(errors)} errors, integrity={\"PASS\" if integrity_ok else \"FAIL\"}')\n",
                "\n",
                f"reg_name = 'BCDR_Register_{lh_name}'\n",
                "print(f'\\n  Running {{reg_name}}...')\n",
                "try:\n",
                "    reg_exit = mssparkutils.notebook.run(reg_name, timeout_seconds=600)\n",
                "    reg_result = json.loads(reg_exit) if reg_exit else {}\n",
                "    print(f'    Registered: {{reg_result}}')\n",
                "    result['registered'] = reg_result.get('registered', 0)\n",
                "except Exception as e:\n",
                "    print(f'    Registration error: {{e}}')\n",
                "    result['reg_error'] = str(e)\n",
                "\n",
                "mssparkutils.notebook.exit(json.dumps(result))\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
    ]

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernel_info": {"name": "synapse_pyspark"},
            "kernelspec": {"name": "synapse_pyspark", "display_name": "Synapse PySpark"},
            "language_info": {"name": "python"},
            "trident": {
                "lakehouse": {
                    "default_lakehouse": secondary_lh_id,
                    "default_lakehouse_name": lh_name,
                    "default_lakehouse_workspace_id": secondary_ws_id,
                    "known_lakehouses": [{"id": secondary_lh_id}],
                },
            },
        },
        "cells": cells,
    }


def _generate_sync_notebook_ipynb(primary_ws_id: str, secondary_ws_id: str,
                                   lakehouse_mappings: List[Dict[str, str]],
                                   default_lakehouse_id: str,
                                   default_lakehouse_name: str = "") -> dict:
    """Generate the orchestrator notebook that calls per-lakehouse sync sub-notebooks.

    Each lakehouse is synced by its own dedicated notebook (BCDR_Sync_{name}) which
    has the correct default_lakehouse set, preventing cross-lakehouse contamination.
    After all syncs, it calls per-lakehouse registration notebooks.
    """

    # Build known_lakehouses list for notebook metadata (attach all secondary LHs)
    known_lh_list = [{"id": m["secondary_id"]} for m in lakehouse_mappings]
    if not any(k["id"] == default_lakehouse_id for k in known_lh_list):
        known_lh_list.insert(0, {"id": default_lakehouse_id})

    # Build the LAKEHOUSE_MAPPINGS literal for the notebook
    lh_map_lines = "LAKEHOUSE_MAPPINGS = [\n"
    for m in lakehouse_mappings:
        lh_map_lines += "    {\n"
        lh_map_lines += f'        "name": "{m["name"]}",\n'
        lh_map_lines += f'        "primary_id": "{m["primary_id"]}",\n'
        lh_map_lines += f'        "secondary_id": "{m["secondary_id"]}"\n'
        lh_map_lines += "    },\n"
    lh_map_lines += "]\n"

    cells = [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# Resiliency & Recovery Lakehouse Data Replication — Orchestrator\n",
                "\n",
                "**Auto-generated** by Fabric Resiliency & Recovery Dashboard.  \n",
                "Calls per-lakehouse sync notebooks, then registration notebooks.\n",
                "Each sub-notebook has its own default_lakehouse to prevent cross-contamination.\n",
            ],
        },
        # Config
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "from notebookutils import mssparkutils\n",
                "import json\n",
                "\n",
                f'PRIMARY_WORKSPACE_ID = "{primary_ws_id}"\n',
                f'SECONDARY_WORKSPACE_ID = "{secondary_ws_id}"\n',
                "\n",
                lh_map_lines,
            ],
            "outputs": [],
            "execution_count": None,
        },
        # Data sync — calls per-lakehouse sub-notebooks
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# ================================================================\n",
                "# DATA SYNC — per-lakehouse isolation\n",
                "# Each BCDR_Sync_{name} notebook has its own default_lakehouse\n",
                "# so Spark writes only to the correct lakehouse.\n",
                "# ================================================================\n",
                "print('=' * 70)\n",
                "print('Resiliency & Recovery LAKEHOUSE DATA REPLICATION')\n",
                "print(f'Primary:    {PRIMARY_WORKSPACE_ID}')\n",
                "print(f'Secondary:  {SECONDARY_WORKSPACE_ID}')\n",
                "print(f'Lakehouses: {len(LAKEHOUSE_MAPPINGS)}')\n",
                "print('=' * 70)\n",
                "\n",
                "sync_results = []\n",
                "for lh in LAKEHOUSE_MAPPINGS:\n",
                "    sync_name = f\"BCDR_Sync_{lh['name']}\"\n",
                '    print(f"\\n  Running {sync_name}...")\n',
                "    try:\n",
                "        exit_val = mssparkutils.notebook.run(sync_name, timeout_seconds=1800)\n",
                "        result = json.loads(exit_val) if exit_val else {}\n",
                '        print(f"    {result}")\n',
                "        sync_results.append(result)\n",
                "    except Exception as e:\n",
                '        print(f"    ERROR: {e}")\n',
                "        sync_results.append({'lakehouse': lh['name'], 'error': str(e)})\n",
                "\n",
                "total_tables = sum(r.get('tables', 0) for r in sync_results)\n",
                "total_rows = sum(r.get('rows', 0) for r in sync_results)\n",
                "total_files = sum(r.get('files', 0) for r in sync_results)\n",
                "total_cdf = sum(r.get('cdf_enabled', 0) for r in sync_results)\n",
                "total_errors = sum(r.get('errors', 0) for r in sync_results)\n",
                "\n",
                "print('\\n' + '=' * 70)\n",
                "print('SYNC COMPLETE')\n",
                "print(f'  Tables synced: {total_tables}')\n",
                "print(f'  Rows processed: {total_rows}')\n",
                "print(f'  File folders copied: {total_files}')\n",
                "print(f'  CDF enabled on: {total_cdf} tables')\n",
                "print(f'  Errors: {total_errors}')\n",
                "print('=' * 70)\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
        # Registration — calls per-lakehouse registration notebooks
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# ================================================================\n",
                "# TABLE REGISTRATION\n",
                "# Each registration notebook has its lakehouse as default so that\n",
                "# CREATE SCHEMA / CREATE TABLE target the correct catalog.\n",
                "# ================================================================\n",
                "print('\\n' + '=' * 70)\n",
                "print('TABLE REGISTRATION')\n",
                "print('=' * 70)\n",
                "\n",
                "reg_results = []\n",
                "for lh in LAKEHOUSE_MAPPINGS:\n",
                "    reg_name = f\"BCDR_Register_{lh['name']}\"\n",
                '    print(f"\\n  Running {reg_name}...")\n',
                "    try:\n",
                "        exit_val = mssparkutils.notebook.run(reg_name, timeout_seconds=600)\n",
                "        result = json.loads(exit_val) if exit_val else {}\n",
                '        print(f"    {result}")\n',
                "        reg_results.append(result)\n",
                "    except Exception as e:\n",
                '        print(f"    ERROR: {e}")\n',
                "        reg_results.append({'error': str(e)})\n",
                "\n",
                "total_reg = sum(r.get('registered', 0) for r in reg_results)\n",
                "print(f'\\nRegistered {total_reg} tables across {len(reg_results)} lakehouses')\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
    ]

    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernel_info": {"name": "synapse_pyspark"},
            "kernelspec": {
                "name": "synapse_pyspark",
                "display_name": "Synapse PySpark",
            },
            "language_info": {"name": "python"},
            "trident": {
                "lakehouse": {
                    "default_lakehouse": default_lakehouse_id,
                    "default_lakehouse_name": default_lakehouse_name,
                    "default_lakehouse_workspace_id": secondary_ws_id,
                    "known_lakehouses": known_lh_list,
                },
            },
        },
        "cells": cells,
    }
    return notebook


def _generate_pipeline_definition(notebook_id: str, workspace_id: str) -> dict:
    """Generate a Fabric Data Pipeline JSON that runs the replication notebook."""
    pipeline = {
        "properties": {
            "activities": [
                {
                    "name": "BCDR_Lakehouse_Replication",
                    "type": "TridentNotebook",
                    "dependsOn": [],
                    "policy": {
                        "timeout": "0.02:00:00",
                        "retry": 1,
                        "retryIntervalInSeconds": 30,
                    },
                    "typeProperties": {
                        "notebookId": notebook_id,
                        "workspaceId": workspace_id,
                    },
                }
            ],
        }
    }
    return pipeline


def deploy_sync_artifacts() -> Dict[str, Any]:
    """Deploy the replication Notebook + Data Pipeline to the secondary workspace.
    Returns info about the created artifacts."""
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return {"error": "Both workspaces must be configured"}

    # Invalidate cache so lakehouse mappings reflect the CURRENT state
    _cache.pop(f"items:{p_id}", None)
    _cache_ttl.pop(f"items:{p_id}", None)
    _cache.pop(f"items:{s_id}", None)
    _cache_ttl.pop(f"items:{s_id}", None)

    lh_mappings = _get_lakehouse_mappings()
    if not lh_mappings:
        return {"error": "No matching lakehouses found between primary and secondary"}

    # Warn if some primary lakehouses have no secondary match
    all_p_items = get_workspace_items(p_id)
    p_lh_count = sum(1 for i in all_p_items if i.get("type") == "Lakehouse")
    if len(lh_mappings) < p_lh_count:
        logger.warning(f"deploy_sync_artifacts: Only {len(lh_mappings)}/{p_lh_count} "
                       f"primary lakehouses have matching secondary lakehouses — "
                       f"replicate lakehouses first for a complete sync")

    # Use first secondary lakehouse as default for the master notebook
    default_lh_id = lh_mappings[0]["secondary_id"]
    default_lh_name = lh_mappings[0]["name"]

    # Check existing items in secondary
    s_items = get_workspace_items(s_id)
    existing_nb = next(
        (i for i in s_items if i.get("displayName") == "BCDR_Data_Replication" and i.get("type") == "Notebook"),
        None,
    )
    existing_pl = next(
        (i for i in s_items if i.get("displayName") == "BCDR_Sync_Pipeline" and i.get("type") == "DataPipeline"),
        None,
    )

    result = {"notebook": None, "pipeline": None, "registrations": []}

    # --- Deploy per-lakehouse REGISTRATION notebooks ---
    for lh_map in lh_mappings:
        reg_name = f"BCDR_Register_{lh_map['name']}"
        reg_ipynb = _generate_registration_notebook_ipynb(
            p_id, s_id,
            lh_map["primary_id"], lh_map["secondary_id"], lh_map["name"],
        )
        r_content_b64 = base64.b64encode(json.dumps(reg_ipynb).encode("utf-8")).decode("ascii")
        r_definition = {
            "format": "ipynb",
            "parts": [
                {"path": "artifact.content.ipynb", "payload": r_content_b64, "payloadType": "InlineBase64"}
            ],
        }
        existing_r = next(
            (i for i in s_items if i.get("displayName") == reg_name and i.get("type") == "Notebook"),
            None,
        )
        try:
            if existing_r:
                fabric_api("POST", f"/workspaces/{s_id}/items/{existing_r['id']}/updateDefinition",
                           payload={"definition": r_definition}, timeout=120)
                logger.info(f"Updated registration notebook {reg_name}")
                result["registrations"].append({"name": reg_name, "id": existing_r['id'], "action": "updated"})
            else:
                resp = fabric_api("POST", f"/workspaces/{s_id}/items",
                                  payload={"displayName": reg_name, "type": "Notebook", "definition": r_definition},
                                  timeout=120)
                r_id = resp.get("id", "")
                logger.info(f"Created registration notebook {reg_name} ({r_id})")
                result["registrations"].append({"name": reg_name, "id": r_id, "action": "created"})
        except Exception as e:
            logger.error(f"Failed to deploy {reg_name}: {e}")
            result["registrations"].append({"name": reg_name, "error": str(e)})

    # --- Deploy per-lakehouse SYNC notebooks ---
    # Each has its own default_lakehouse so Spark writes stay scoped correctly.
    result["sync_notebooks"] = []
    for lh_map in lh_mappings:
        sync_name = f"BCDR_Sync_{lh_map['name']}"
        sync_ipynb = _generate_per_lh_sync_notebook_ipynb(
            p_id, s_id,
            lh_map["primary_id"], lh_map["secondary_id"], lh_map["name"],
        )
        s_content_b64 = base64.b64encode(json.dumps(sync_ipynb).encode("utf-8")).decode("ascii")
        s_definition = {
            "format": "ipynb",
            "parts": [
                {"path": "artifact.content.ipynb", "payload": s_content_b64, "payloadType": "InlineBase64"}
            ],
        }
        existing_s = next(
            (i for i in s_items if i.get("displayName") == sync_name and i.get("type") == "Notebook"),
            None,
        )
        try:
            if existing_s:
                fabric_api("POST", f"/workspaces/{s_id}/items/{existing_s['id']}/updateDefinition",
                           payload={"definition": s_definition}, timeout=120)
                logger.info(f"Updated sync notebook {sync_name}")
                result["sync_notebooks"].append({"name": sync_name, "id": existing_s['id'], "action": "updated"})
            else:
                resp = fabric_api("POST", f"/workspaces/{s_id}/items",
                                  payload={"displayName": sync_name, "type": "Notebook", "definition": s_definition},
                                  timeout=120)
                s_nb_id = resp.get("id", "")
                logger.info(f"Created sync notebook {sync_name} ({s_nb_id})")
                result["sync_notebooks"].append({"name": sync_name, "id": s_nb_id, "action": "created"})
        except Exception as e:
            logger.error(f"Failed to deploy {sync_name}: {e}")
            result["sync_notebooks"].append({"name": sync_name, "error": str(e)})

    # --- Deploy main Notebook ---
    notebook_ipynb = _generate_sync_notebook_ipynb(p_id, s_id, lh_mappings, default_lh_id, default_lh_name)
    nb_content_b64 = base64.b64encode(json.dumps(notebook_ipynb).encode("utf-8")).decode("ascii")
    nb_definition = {
        "format": "ipynb",
        "parts": [
            {"path": "artifact.content.ipynb", "payload": nb_content_b64, "payloadType": "InlineBase64"}
        ],
    }

    if existing_nb:
        nb_id = existing_nb["id"]
        try:
            fabric_api("POST", f"/workspaces/{s_id}/items/{nb_id}/updateDefinition",
                       payload={"definition": nb_definition}, timeout=120)
            logger.info(f"Updated notebook BCDR_Data_Replication ({nb_id})")
            result["notebook"] = {"id": nb_id, "action": "updated"}
        except Exception as e:
            logger.error(f"Failed to update notebook: {e}")
            result["notebook"] = {"error": str(e), "action": "update_failed"}
    else:
        try:
            resp = fabric_api("POST", f"/workspaces/{s_id}/items",
                              payload={"displayName": "BCDR_Data_Replication", "type": "Notebook",
                                       "definition": nb_definition}, timeout=120)
            nb_id = resp.get("id", "")
            logger.info(f"Created notebook BCDR_Data_Replication ({nb_id})")
            result["notebook"] = {"id": nb_id, "action": "created"}
        except Exception as e:
            logger.error(f"Failed to create notebook: {e}")
            result["notebook"] = {"error": str(e), "action": "create_failed"}
            return result

    # --- Deploy Data Pipeline ---
    nb_id = result["notebook"].get("id")
    if not nb_id:
        result["pipeline"] = {"error": "Notebook not available, skipping pipeline"}
        return result

    pipeline_def = _generate_pipeline_definition(nb_id, s_id)
    pl_content_b64 = base64.b64encode(json.dumps(pipeline_def).encode("utf-8")).decode("ascii")
    pl_definition = {
        "parts": [
            {"path": "pipeline-content.json", "payload": pl_content_b64, "payloadType": "InlineBase64"}
        ],
    }

    if existing_pl:
        pl_id = existing_pl["id"]
        try:
            fabric_api("POST", f"/workspaces/{s_id}/items/{pl_id}/updateDefinition",
                       payload={"definition": pl_definition}, timeout=120)
            logger.info(f"Updated pipeline BCDR_Sync_Pipeline ({pl_id})")
            result["pipeline"] = {"id": pl_id, "action": "updated"}
        except Exception as e:
            logger.error(f"Failed to update pipeline: {e}")
            result["pipeline"] = {"error": str(e), "action": "update_failed"}
    else:
        try:
            resp = fabric_api("POST", f"/workspaces/{s_id}/items",
                              payload={"displayName": "BCDR_Sync_Pipeline", "type": "DataPipeline",
                                       "definition": pl_definition}, timeout=120)
            pl_id = resp.get("id", "")
            logger.info(f"Created pipeline BCDR_Sync_Pipeline ({pl_id})")
            result["pipeline"] = {"id": pl_id, "action": "created"}
        except Exception as e:
            logger.error(f"Failed to create pipeline: {e}")
            result["pipeline"] = {"error": str(e), "action": "create_failed"}

    # Clear cache
    _cache.pop(f"items:{s_id}", None)
    _cache_ttl.pop(f"items:{s_id}", None)

    return result


def run_sync_notebook() -> Dict[str, Any]:
    """Trigger per-lakehouse sync notebooks as separate Spark jobs.

    Each BCDR_Sync_{name} notebook gets its own Spark cluster and session,
    ensuring the default_lakehouse is correctly scoped. This prevents
    cross-lakehouse contamination that occurs when a single notebook
    writes to multiple lakehouses in schema-enabled mode.
    """
    s_id = _ws_id("secondary")
    if not s_id:
        return {"error": "Secondary workspace not configured"}

    s_items = get_workspace_items(s_id)

    # Find all per-lakehouse sync notebooks
    sync_notebooks = [
        i for i in s_items
        if i.get("type") == "Notebook" and i.get("displayName", "").startswith("BCDR_Sync_")
    ]
    if not sync_notebooks:
        # Fallback: try the old orchestrator notebook
        nb = next(
            (i for i in s_items if i.get("displayName") == "BCDR_Data_Replication" and i.get("type") == "Notebook"),
            None,
        )
        if not nb:
            return {"error": "No sync notebooks found. Deploy them first."}
        sync_notebooks = [nb]

    token = _ensure_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    triggered = []
    errors = []
    for nb in sync_notebooks:
        nb_id = nb["id"]
        nb_name = nb.get("displayName", nb_id)
        try:
            url = f"{FABRIC_API_BASE}/workspaces/{s_id}/items/{nb_id}/jobs/instances?jobType=RunNotebook"
            resp = requests.post(url, headers=headers, timeout=30)
            if resp.status_code in (200, 201, 202):
                location = resp.headers.get("Location", "")
                logger.info(f"Triggered {nb_name} ({nb_id}), location: {location}")
                triggered.append({"name": nb_name, "id": nb_id, "job_location": location})
            else:
                error_text = resp.text[:300]
                logger.error(f"Failed to trigger {nb_name}: {resp.status_code} {error_text}")
                errors.append({"name": nb_name, "error": f"API {resp.status_code}: {error_text}"})
        except Exception as e:
            logger.error(f"Failed to trigger {nb_name}: {e}")
            errors.append({"name": nb_name, "error": str(e)})

    return {
        "status": "ok" if triggered else "error",
        "message": f"Triggered {len(triggered)} sync notebooks as separate Spark jobs. Each runs with its own lakehouse context.",
        "triggered": triggered,
        "errors": errors,
    }


# ============================================================================
# WORKSPACE DISCOVERY
# ============================================================================

def list_all_workspaces() -> List[Dict[str, Any]]:
    """Fetch all workspaces the logged-in user has access to."""
    workspaces = []
    try:
        data = fabric_api("GET", "/workspaces")
        workspaces = data.get("value", [])
    except Exception as e:
        logger.error(f"Error listing workspaces: {e}")
    _workspace_state["all_workspaces"] = workspaces
    return workspaces


def get_workspace_items(workspace_id: str) -> List[Dict[str, Any]]:
    """Get ALL items in a workspace with pagination (cached)."""
    def _fetch(ws_id):
        all_items = []
        try:
            endpoint = f"/workspaces/{ws_id}/items"
            while endpoint:
                data = fabric_api("GET", endpoint)
                all_items.extend(data.get("value", []))
                # Handle pagination via continuationUri or continuationToken
                continuation = data.get("continuationUri") or data.get("continuationToken")
                if continuation:
                    if continuation.startswith("http"):
                        # continuationUri is a full URL — strip the base
                        endpoint = continuation.replace(FABRIC_API_BASE, "")
                    else:
                        endpoint = f"/workspaces/{ws_id}/items?continuationToken={continuation}"
                else:
                    endpoint = None
            logger.info(f"Workspace {ws_id}: fetched {len(all_items)} items")
        except Exception as e:
            logger.error(f"Error listing items for {ws_id}: {e}")
        return all_items
    return _cached_call(f"items:{workspace_id}", _fetch, workspace_id)


_BCDR_PREFIXES = ("BCDR_",)

def _is_bcdr_system_item(item: Dict[str, Any]) -> bool:
    """Check if an item is a Resiliency & Recovery system artifact (notebook/pipeline created by the dashboard)."""
    name = item.get("displayName", "")
    return any(name.startswith(p) for p in _BCDR_PREFIXES)


def _filter_business_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return only business items (exclude Resiliency & Recovery system artifacts)."""
    return [i for i in items if not _is_bcdr_system_item(i)]


def _get_bcdr_system_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return only Resiliency & Recovery system artifacts from a workspace item list."""
    return [i for i in items if _is_bcdr_system_item(i)]


# ============================================================================
# DATA RETRIEVAL (live workspace data)
# ============================================================================

def _active_pair() -> Optional[Dict[str, Any]]:
    """Return the currently active workspace pair dict (or the first pair, or None)."""
    pairs = _workspace_state.get("pairs", [])
    active_id = _workspace_state.get("active_pair")
    for p in pairs:
        if p.get("id") == active_id:
            return p
    return pairs[0] if pairs else None


def _get_pair(pair_id: str) -> Optional[Dict[str, Any]]:
    """Return a workspace pair by id."""
    for p in _workspace_state.get("pairs", []):
        if p.get("id") == pair_id:
            return p
    return None


def _ws_id(role: str) -> Optional[str]:
    pair = _active_pair()
    return pair.get(f"{role}_id") if pair else None


def _ws_name(role: str) -> str:
    pair = _active_pair()
    return (pair.get(f"{role}_name") or "(not selected)") if pair else "(not selected)"


def _ws_id_for_pair(pair_id: str, role: str) -> Optional[str]:
    """Return workspace id for a specific pair (used by timers)."""
    pair = _get_pair(pair_id)
    return pair.get(f"{role}_id") if pair else None


def get_workspace_health(workspace_id: str) -> Dict[str, Any]:
    """Get workspace health metrics from live API."""
    is_primary = workspace_id == _ws_id("primary")
    role = "primary" if is_primary else "secondary"
    try:
        items = _filter_business_items(get_workspace_items(workspace_id))
        item_count = len(items)
        return {
            'workspace_id': workspace_id,
            'name': _ws_name(role),
            'health': 100 if item_count > 0 else 0,
            'item_count': item_count,
            'last_heartbeat': datetime.now().isoformat(),
            'capacity_used': item_count,
            'status': 'HEALTHY' if item_count > 0 else 'EMPTY',
        }
    except Exception as e:
        logger.warning(f"Health check failed: {e}")
        return {
            'workspace_id': workspace_id,
            'name': _ws_name(role),
            'health': 0,
            'item_count': 0,
            'last_heartbeat': datetime.now().isoformat(),
            'capacity_used': 0,
            'status': 'ERROR',
        }


def _compute_replication_lag(s_id: str) -> Dict[str, Any]:
    """Compute real replication lag from last sync run, notebook job history, and artifact drift.

    Returns dict with:
      lag_minutes  — minutes since last successful data sync (None if never synced)
      lag_source   — what the lag is based on (schedule/autosync/notebook_job/never)
      last_sync_ts — ISO timestamp of the last known sync event
      artifact_drift — number of primary items missing in secondary
    """
    # Cache for 30 seconds to avoid repeated expensive API calls
    cache_key = f"repl_lag:{s_id}"
    now_t = time.time()
    if cache_key in _cache and now_t < _cache_ttl.get(cache_key, 0):
        return _cache[cache_key]

    now = datetime.now()
    best_ts: Optional[datetime] = None
    source = "never"

    # 1) Check schedule state
    last_run = _schedule_state.get("last_run")
    if last_run and _schedule_state.get("last_status", "").startswith("Triggered"):
        try:
            ts = datetime.fromisoformat(last_run)
            if best_ts is None or ts > best_ts:
                best_ts = ts
                source = "schedule"
        except Exception:
            pass

    # 2) Check auto-sync state
    last_check = _autosync_state.get("last_check")
    if last_check and _autosync_state.get("enabled"):
        try:
            ts = datetime.fromisoformat(last_check)
            if best_ts is None or ts > best_ts:
                best_ts = ts
                source = "autosync"
        except Exception:
            pass

    # 2b) Check azcopy state (manual or scheduled azcopy runs)
    azcopy_last = _azcopy_state.get("last_run")
    if azcopy_last:
        try:
            ts = datetime.fromisoformat(azcopy_last)
            if best_ts is None or ts > best_ts:
                best_ts = ts
                source = "azcopy"
        except Exception:
            pass

    # 2c) Check azcopy schedule state
    azcopy_sched_last = _azcopy_schedule_state.get("last_run")
    if azcopy_sched_last:
        try:
            ts = datetime.fromisoformat(azcopy_sched_last)
            if best_ts is None or ts > best_ts:
                best_ts = ts
                source = "azcopy_schedule"
        except Exception:
            pass

    # 3) Check notebook job history via Fabric API (all notebooks, both workspaces)
    #    Time-budgeted: stop after 12s total to avoid blocking the topology API.
    try:
        if is_authenticated():
            token = _ensure_token()
            headers = {"Authorization": f"Bearer {token}"}
            nb_scan_start = time.time()
            NB_TIME_BUDGET = 12  # seconds
            ws_ids_to_check = [w for w in [_ws_id("primary"), s_id] if w]
            for ws_id in ws_ids_to_check:
                if time.time() - nb_scan_start > NB_TIME_BUDGET:
                    break
                ws_items = get_workspace_items(ws_id)
                notebooks = [i for i in ws_items if i.get("type") == "Notebook"]
                for nb in notebooks:
                    if time.time() - nb_scan_start > NB_TIME_BUDGET:
                        break
                    try:
                        nb_id = nb["id"]
                        url = f"{FABRIC_API_BASE}/workspaces/{ws_id}/items/{nb_id}/jobs/instances"
                        resp = requests.get(url, headers=headers, timeout=5)
                        if resp.status_code == 200:
                            for inst in resp.json().get("value", []):
                                status = inst.get("status", "")
                                end_time = inst.get("endTimeUtc") or inst.get("endTime")
                                if status.lower() in ("completed", "succeeded") and end_time:
                                    try:
                                        ts = datetime.fromisoformat(end_time.replace("Z", "+00:00")).replace(tzinfo=None)
                                        if best_ts is None or ts > best_ts:
                                            best_ts = ts
                                            source = "notebook_job"
                                    except Exception:
                                        pass
                                    break  # Most recent completed per notebook
                    except Exception:
                        continue
            logger.debug(f"Lag: notebook scan took {time.time() - nb_scan_start:.1f}s, checked {len(ws_ids_to_check)} workspaces")
    except Exception as ex:
        logger.debug(f"Lag: notebook job query failed: {ex}")

    # 4) Artifact drift count
    p_id = _ws_id("primary")
    artifact_drift = 0
    if p_id and s_id:
        try:
            p_items = _filter_business_items(get_workspace_items(p_id))
            s_items_list = _filter_business_items(get_workspace_items(s_id))
            s_names = {(i.get("displayName"), i.get("type")) for i in s_items_list}
            for pi in p_items:
                if (pi.get("displayName"), pi.get("type")) not in s_names:
                    artifact_drift += 1
        except Exception:
            pass

    # Compute lag in minutes
    if best_ts:
        lag_minutes = round((now - best_ts).total_seconds() / 60, 1)
    else:
        lag_minutes = None  # Never synced

    result = {
        "lag_minutes": lag_minutes,
        "lag_source": source,
        "last_sync_ts": best_ts.isoformat() if best_ts else None,
        "artifact_drift": artifact_drift,
    }
    _cache[cache_key] = result
    _cache_ttl[cache_key] = time.time() + 30  # 30-second cache
    return result


def _get_workspace_region(workspace_id: str) -> Dict[str, str]:
    """Get workspace region and capacity info from Fabric API."""
    cache_key = f"region:{workspace_id}"
    now = time.time()
    if cache_key in _cache and now < _cache_ttl.get(cache_key, 0):
        return _cache[cache_key]
    info: Dict[str, str] = {}
    try:
        ws = fabric_api("GET", f"/workspaces/{workspace_id}")
        cap_id = ws.get("capacityId", "")
        info["capacity_id"] = cap_id
        if cap_id:
            # List all accessible capacities and find the matching one
            try:
                caps_data = _cached_call("_all_capacities", lambda: fabric_api("GET", "/capacities"))
                capacities = caps_data.get("value", []) if isinstance(caps_data, dict) else []
                for cap in capacities:
                    if cap.get("id") == cap_id:
                        info["region"] = cap.get("region", "")
                        info["capacity_name"] = cap.get("displayName", "")
                        info["capacity_sku"] = cap.get("sku", "")
                        info["capacity_state"] = cap.get("state", "")
                        break
            except Exception as ex:
                logger.warning(f"Capacities list failed: {ex}")
                info["region"] = ""
    except Exception as ex:
        logger.warning(f"Region lookup failed for {workspace_id}: {ex}")
    # Only cache if we got actual region data
    if info.get("region"):
        _cache[cache_key] = info
        _cache_ttl[cache_key] = now + CACHE_SECONDS
    return info


def get_regional_topology() -> Dict[str, Any]:
    """Build topology data from selected workspaces including real region info."""
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    primary_health = get_workspace_health(p_id) if p_id else {
        'name': '(not selected)', 'health': 0, 'item_count': 0,
        'last_heartbeat': datetime.now().isoformat(), 'capacity_used': 0, 'status': 'NOT_SET',
    }
    secondary_health = get_workspace_health(s_id) if s_id else {
        'name': '(not selected)', 'health': 0, 'item_count': 0,
        'last_heartbeat': datetime.now().isoformat(), 'capacity_used': 0, 'status': 'NOT_SET',
    }
    p_items = primary_health.get('item_count', 0)
    s_items = secondary_health.get('item_count', 0)
    sync_pct = (min(s_items, p_items) / max(p_items, 1)) * 100 if p_items else 0

    # Fetch workspace region & capacity info
    p_region = _get_workspace_region(p_id) if p_id else {}
    s_region = _get_workspace_region(s_id) if s_id else {}

    # Real replication lag
    lag_info = _compute_replication_lag(s_id) if s_id else {
        "lag_minutes": None, "lag_source": "never", "last_sync_ts": None, "artifact_drift": 0,
    }
    lag_min = lag_info["lag_minutes"]

    # Determine status
    if not s_id:
        status = "NO_DR"
    elif lag_min is None:
        status = "NEEDS_SYNC"
    elif lag_min <= 15 and lag_info["artifact_drift"] == 0:
        status = "HEALTHY"
    elif lag_min <= 60:
        status = "NEEDS_SYNC"
    else:
        status = "STALE"

    return {
        'primary': {
            'name': _ws_name("primary"),
            'capacity': f'{p_items} items',
            'workspace': p_id or '',
            **primary_health,
            'role': 'PRIMARY',
            'region': p_region.get('region', ''),
            'capacity_id': p_region.get('capacity_id', ''),
            'capacity_name': p_region.get('capacity_name', ''),
            'capacity_sku': p_region.get('capacity_sku', ''),
            'capacity_state': p_region.get('capacity_state', ''),
        },
        'secondary': {
            'name': _ws_name("secondary"),
            'capacity': f'{s_items} items' if s_id else 'Not configured',
            'workspace': s_id or '',
            **secondary_health,
            'role': 'STANDBY' if s_id else 'NOT SET',
            'region': s_region.get('region', ''),
            'capacity_id': s_region.get('capacity_id', ''),
            'capacity_name': s_region.get('capacity_name', ''),
            'capacity_sku': s_region.get('capacity_sku', ''),
            'capacity_state': s_region.get('capacity_state', ''),
        },
        'cross_region': p_region.get('region', '') != s_region.get('region', '') and p_region.get('region') and s_region.get('region'),
        'replication_lag': lag_min,
        'lag_source': lag_info["lag_source"],
        'last_sync_ts': lag_info["last_sync_ts"],
        'artifact_drift': lag_info["artifact_drift"],
        'status': status,
        'timestamp': datetime.now().isoformat(),
    }


def get_artifact_inventory() -> Dict[str, Any]:
    """Get artifact inventory from live workspaces."""
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    all_primary = get_workspace_items(p_id) if p_id else []
    all_secondary = get_workspace_items(s_id) if s_id else []
    primary_items = _filter_business_items(all_primary)
    secondary_items = _filter_business_items(all_secondary)
    system_items = _get_bcdr_system_items(all_secondary)

    def by_type(items):
        d = {}
        for i in items:
            t = i.get("type", "Unknown")
            d[t] = d.get(t, 0) + 1
        return d

    return {
        'primary': {
            'total': len(primary_items),
            'by_type': by_type(primary_items),
            'items': [{'id': i.get('id'), 'name': i.get('displayName'), 'type': i.get('type')} for i in primary_items],
        },
        'secondary': {
            'total': len(secondary_items),
            'by_type': by_type(secondary_items),
            'items': [{'id': i.get('id'), 'name': i.get('displayName'), 'type': i.get('type')} for i in secondary_items],
        },
        'sync_percentage': (len(secondary_items) / max(len(primary_items), 1)) * 100 if primary_items else 0,
        'system_artifacts': [{'name': i.get('displayName'), 'type': i.get('type')} for i in system_items],
    }


def get_bcdr_status() -> Dict[str, Any]:
    """Get Resiliency & Recovery status grouped by artifact type with mirroring comparison."""
    from concurrent.futures import ThreadPoolExecutor

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")

    # Fetch both workspaces in parallel (cached after first call)
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_p = pool.submit(get_workspace_items, p_id) if p_id else None
        fut_s = pool.submit(get_workspace_items, s_id) if s_id else None
    all_primary = fut_p.result() if fut_p else []
    all_secondary = fut_s.result() if fut_s else []
    primary_items = _filter_business_items(all_primary)
    secondary_items = _filter_business_items(all_secondary)
    system_items = _get_bcdr_system_items(all_secondary)

    # Group items by type
    primary_by_type: Dict[str, List[Dict]] = {}
    for item in primary_items:
        t = item.get("type", "Unknown")
        primary_by_type.setdefault(t, []).append({
            'id': item.get('id'),
            'name': item.get('displayName'),
            'type': t,
        })

    secondary_by_type: Dict[str, List[Dict]] = {}
    for item in secondary_items:
        t = item.get("type", "Unknown")
        secondary_by_type.setdefault(t, []).append({
            'id': item.get('id'),
            'name': item.get('displayName'),
            'type': t,
        })

    # Build mirroring status for each type
    # Categorize artifact types
    DATA_STORAGE = {"Lakehouse", "Warehouse", "SQLEndpoint", "KQLDatabase", "Eventhouse"}
    NOT_SUPPORTED = {"Warehouse", "SQLEndpoint"}  # Types that can't be replicated via API

    all_types = sorted(set(list(primary_by_type.keys()) + list(secondary_by_type.keys())))
    type_cards = []
    total_primary = 0
    total_mirrored = 0

    for t in all_types:
        p_items = primary_by_type.get(t, [])
        s_items = secondary_by_type.get(t, [])
        p_names = {i['name'] for i in p_items}
        s_names = {i['name'] for i in s_items}
        mirrored = len(p_names & s_names)
        total_count = len(p_items)
        total_primary += total_count
        total_mirrored += mirrored
        pct = round((mirrored / total_count) * 100) if total_count > 0 else 0

        type_cards.append({
            'type': t,
            'category': 'data_storage' if t in DATA_STORAGE else 'components',
            'primary_count': total_count,
            'secondary_count': len(s_items),
            'mirrored_count': mirrored,
            'percentage': pct,
            'supported': t not in NOT_SUPPORTED,
            'primary_items': p_items,
            'secondary_items': s_items,
        })

    overall_pct = round((total_mirrored / max(total_primary, 1)) * 100) if total_primary else 0

    # Quick capacity health check — cached and parallelized
    def _check_capacity(ws_id):
        try:
            fabric_api("GET", f"/workspaces/{ws_id}", timeout=10)
            return "ok"
        except RuntimeError as e:
            err_str = str(e)
            if "CapacityNotActive" in err_str:
                return "capacity_inactive"
            elif "401" in err_str or "403" in err_str:
                return "auth_error"
            return "ok"

    capacity_status = {"primary": "ok", "secondary": "ok"}
    cap_tasks = {}
    for label, ws_id in [("primary", p_id), ("secondary", s_id)]:
        if not ws_id:
            capacity_status[label] = "not_configured"
        else:
            cap_tasks[label] = ws_id

    if cap_tasks:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cap_futures = {
                label: pool.submit(_cached_call, f"capacity:{ws_id}", _check_capacity, ws_id)
                for label, ws_id in cap_tasks.items()
            }
            for label, fut in cap_futures.items():
                capacity_status[label] = fut.result()

    return {
        'workspace_name': _ws_name("primary"),
        'workspace_id': p_id,
        'secondary_name': _ws_name("secondary"),
        'secondary_id': s_id,
        'overall_percentage': overall_pct,
        'total_primary': total_primary,
        'total_mirrored': total_mirrored,
        'type_cards': type_cards,
        'has_secondary': s_id is not None,
        'capacity_status': capacity_status,
        'system_artifacts': [{'name': i.get('displayName'), 'type': i.get('type')} for i in system_items],
    }


# ============================================================================
# FOLDER STRUCTURE MIRRORING
# ============================================================================


def _get_workspace_folders(workspace_id: str) -> List[Dict]:
    """List all folders in a workspace via the dedicated /folders endpoint.
    Returns list of {id, displayName, workspaceId}.
    """
    try:
        resp = fabric_api("GET", f"/workspaces/{workspace_id}/folders")
        return resp.get("value", [])
    except Exception as e:
        logger.warning(f"Could not list folders for workspace {workspace_id}: {e}")
        return []


def _ensure_folder_structure(p_id: str, s_id: str, needed_folder_ids: Optional[set] = None) -> Dict[str, str]:
    """Ensure secondary workspace has the same folder structure as primary.

    Returns a mapping: {primary_folder_id: secondary_folder_id}.
    Creates missing folders in secondary using the /folders API.

    If needed_folder_ids is provided, only those folder IDs are created/mapped.
    Otherwise ALL primary folders are mirrored.
    """
    # Get folders from both workspaces via the dedicated endpoint
    p_folders = _get_workspace_folders(p_id)
    s_folders = _get_workspace_folders(s_id)

    if not p_folders:
        return {}  # No folders in primary — nothing to do

    # If caller specified which folder IDs are needed, filter to just those
    if needed_folder_ids:
        p_folders = [f for f in p_folders if f.get("id", "") in needed_folder_ids]

    if not p_folders:
        return {}

    # Build secondary lookup by name
    s_folders_by_name: Dict[str, str] = {}
    for f in s_folders:
        s_folders_by_name[f.get("displayName", "")] = f.get("id", "")

    folder_map: Dict[str, str] = {}

    for pf in p_folders:
        p_fid = pf.get("id", "")
        fname = pf.get("displayName", "")
        if not p_fid or not fname:
            continue

        if fname in s_folders_by_name:
            folder_map[p_fid] = s_folders_by_name[fname]
            logger.info(f"Folder '{fname}' already exists in secondary")
        else:
            # Create folder in secondary via /folders endpoint
            try:
                resp = fabric_api(
                    "POST",
                    f"/workspaces/{s_id}/folders",
                    payload={"displayName": fname},
                    timeout=60,
                )
                new_id = resp.get("id", "")
                if new_id:
                    folder_map[p_fid] = new_id
                    s_folders_by_name[fname] = new_id
                    logger.info(f"Created folder '{fname}' in secondary ({new_id})")
                else:
                    logger.warning(f"Created folder '{fname}' but no ID returned")
            except Exception as e:
                logger.warning(f"Could not create folder '{fname}' in secondary: {e}")

    # Clear secondary item cache since we may have created folders
    if folder_map:
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)

    return folder_map


def _get_folder_id_for_item(item: Dict, folder_map: Dict[str, str]) -> Optional[str]:
    """Given a primary item and a folder map, return the secondary folder ID."""
    p_folder_id = item.get("folderId")
    if p_folder_id and p_folder_id in folder_map:
        return folder_map[p_folder_id]
    return None


# ============================================================================
# SYNC PROGRESS TRACKING — background replication with live progress
# ============================================================================

_sync_progress: Dict[str, Any] = {}
_sync_lock = threading.Lock()


def _update_sync_progress(artifact_type: str, **kwargs):
    with _sync_lock:
        if artifact_type not in _sync_progress:
            _sync_progress[artifact_type] = {}
        _sync_progress[artifact_type].update(kwargs)


def _run_replicate_background(artifact_type: str):
    """Run replicate_items_by_type in background, updating _sync_progress."""
    try:
        _update_sync_progress(artifact_type, status="running", current=0, total=0,
                              current_item="Initializing...", results=[], error=None)
        result = replicate_items_by_type(artifact_type)
        if "error" in result:
            _update_sync_progress(artifact_type, status="failed", error=result["error"])
        else:
            _update_sync_progress(artifact_type, status="completed",
                                  current=result.get("replicated", 0),
                                  total=result.get("total", 0),
                                  results=result.get("details", []),
                                  message=result.get("message", ""))
    except Exception as e:
        logger.exception(f"Background replication error for {artifact_type}")
        _update_sync_progress(artifact_type, status="failed", error=str(e))


def _poll_environment_publish(ws_id: str, env_id: str, env_name: str,
                              logger, timeout: int = 300):
    """Poll until an environment publish completes or times out."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = fabric_api("GET", f"/workspaces/{ws_id}/environments/{env_id}")
            pub = resp.get("properties", {}).get("publishDetails", {})
            state = pub.get("state", "")
            if state == "Success":
                logger.info(f"✓ Environment published: {env_name}")
                return True
            elif state in ("Failed", "Cancelled"):
                logger.warning(f"Environment publish {state}: {env_name}")
                return False
            elif not state:
                # No pending publish state — likely completed instantly
                logger.info(f"✓ Environment publish done: {env_name}")
                return True
        except Exception:
            pass
        time.sleep(15)
    logger.warning(f"Environment publish timeout ({timeout}s): {env_name}")
    return False


def _rebind_report_to_secondary(p_id: str, s_id: str, report_name: str):
    """After replicating a Report, rebind it to the secondary SemanticModel.

    Reports reference a SemanticModel (dataset) by ID. After replication the
    Report in secondary still points to the primary SM. This uses the Power BI
    Rebind API to point it to the matching SM in the secondary workspace.
    """
    try:
        s_items = get_workspace_items(s_id)

        # Find the report in secondary
        s_report = next(
            (i for i in s_items if i.get("type") == "Report" and i.get("displayName") == report_name),
            None,
        )
        if not s_report:
            logger.warning(f"Rebind: Report '{report_name}' not found in secondary")
            return

        s_rpt_id = s_report["id"]

        # Find the SemanticModel in secondary that this report should bind to.
        # Match by: find the primary report's dataset, then find it by name in secondary.
        p_items = get_workspace_items(p_id)

        # Get the primary report's bound dataset via Power BI API
        token = _ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        pbi_url = f"https://api.powerbi.com/v1.0/myorg/groups/{p_id}/reports/{s_rpt_id}"
        # Actually we need the primary report ID to find its dataset name
        p_report = next(
            (i for i in p_items if i.get("type") == "Report" and i.get("displayName") == report_name),
            None,
        )
        if not p_report:
            logger.warning(f"Rebind: Primary Report '{report_name}' not found")
            return

        # Get the primary report's dataset binding
        resp = requests.get(
            f"https://api.powerbi.com/v1.0/myorg/groups/{p_id}/reports/{p_report['id']}",
            headers=headers, timeout=30,
        )
        if resp.status_code != 200:
            logger.warning(f"Rebind: Could not get primary report info: {resp.status_code}")
            return

        p_dataset_id = resp.json().get("datasetId", "")
        if not p_dataset_id:
            logger.warning(f"Rebind: Primary report has no datasetId")
            return

        # Find the matching SM in primary to get its name
        p_sm = next(
            (i for i in p_items if i.get("type") == "SemanticModel" and i.get("id") == p_dataset_id),
            None,
        )
        if not p_sm:
            logger.info(f"Rebind: Primary SM {p_dataset_id} not found in items, trying name match")
            # Fallback: assume SM and Report have the same name
            sm_name = report_name
        else:
            sm_name = p_sm.get("displayName", report_name)

        # Find the secondary SM by name
        s_sm = next(
            (i for i in s_items if i.get("type") == "SemanticModel" and i.get("displayName") == sm_name),
            None,
        )
        if not s_sm:
            logger.warning(f"Rebind: No SemanticModel '{sm_name}' in secondary — cannot rebind")
            return

        s_sm_id = s_sm["id"]

        # Call Power BI Rebind API
        rebind_url = f"https://api.powerbi.com/v1.0/myorg/groups/{s_id}/reports/{s_rpt_id}/Rebind"
        rebind_resp = requests.post(
            rebind_url,
            headers={**headers, "Content-Type": "application/json"},
            json={"datasetId": s_sm_id},
            timeout=30,
        )
        if rebind_resp.status_code == 200:
            logger.info(f"Rebound Report '{report_name}' to secondary SM '{sm_name}' ({s_sm_id})")
        else:
            logger.warning(f"Rebind failed {rebind_resp.status_code}: {rebind_resp.text[:300]}")
    except Exception as e:
        logger.warning(f"Rebind Report '{report_name}' failed: {e}")


def replicate_items_by_type(artifact_type: str) -> Dict[str, Any]:
    """Replicate all items of a given type from primary to secondary workspace."""
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return {"error": "Both primary and secondary workspaces must be configured"}

    primary_items = get_workspace_items(p_id)
    secondary_items = get_workspace_items(s_id)

    # Filter to the requested type
    p_typed = [i for i in primary_items if i.get("type") == artifact_type]
    s_names = {i.get("displayName") for i in secondary_items if i.get("type") == artifact_type}

    # Items missing in secondary
    to_replicate = [i for i in p_typed if i.get("displayName") not in s_names]

    if not to_replicate:
        return {"status": "ok", "message": f"All {artifact_type} items already mirrored", "replicated": 0}

    # Ensure folder structure is mirrored (only for folders used by items being replicated)
    folder_map = {}
    try:
        needed_fids = {i.get("folderId") for i in to_replicate if i.get("folderId")}
        if needed_fids:
            folder_map = _ensure_folder_structure(p_id, s_id, needed_folder_ids=needed_fids)
    except Exception as e:
        logger.warning(f"Could not mirror folder structure: {e}")

    # Build connection string replacement map for all artifact types
    conn_replacements = {}
    try:
        conn_replacements = _build_connection_map(p_id, s_id)
        logger.info(f"Connection map has {len(conn_replacements)} replacements for {artifact_type}")
    except Exception as e:
        logger.warning(f"Could not build connection map: {e}")

    results = []
    _update_sync_progress(artifact_type, total=len(to_replicate), current=0,
                          current_item="Starting...", status="running")
    for idx, item in enumerate(to_replicate):
        item_id = item.get("id")
        item_name = item.get("displayName")
        _update_sync_progress(artifact_type, current=idx, current_item=f"Exporting {item_name}...")
        try:
            # Step 1: Export item definition from primary
            export_resp = None
            definition = {}
            parts = []

            # MLModel: create as empty item (no definition).
            # Fabric's MLflow service validates MLModel definitions against the
            # referenced MLExperiment's internal state and deletes the item
            # within ~60s if they don't match. Since we can't replicate the
            # internal MLflow registry state, we create an empty placeholder.
            if artifact_type == "MLModel":
                export_error = "Skipped (MLModel — empty placeholder)"
                logger.info(f"MLModel {item_name}: creating empty placeholder (definition skipped)")
            else:
                # Some types don't support getDefinition — try and fall back gracefully
                export_error = None
                try:
                    export_resp = fabric_api(
                        "POST",
                        f"/workspaces/{p_id}/items/{item_id}/getDefinition",
                        timeout=120,
                    )
                    if export_resp and isinstance(export_resp, dict):
                        definition = export_resp.get("definition", {})
                        if definition:
                            parts = definition.get("parts", [])
                            logger.info(f"Got {len(parts)} definition parts for {item_name}")
                except Exception as export_err:
                    export_error = str(export_err)
                    logger.warning(f"Could not export definition for {item_name} ({artifact_type}): {export_err}")

            # Rewrite connection strings for all artifact types
            if parts and conn_replacements:
                logger.info(f"Rewriting connection references in {item_name} definition")
                parts = _rewrite_definition_parts(parts, conn_replacements)
                definition = dict(definition)
                definition["parts"] = parts
            elif parts:
                # Even without replacements, strip .platform parts
                parts = [p for p in parts if p.get("path") != ".platform" and not p.get("path", "").endswith("/.platform")]
                definition = dict(definition)
                definition["parts"] = parts

            if not parts:
                if artifact_type in ("SemanticModel", "Report"):
                    reason = export_error or "no definition returned"
                    raise RuntimeError(f"Failed to export {item_name}: {reason}")
                logger.info(f"No definition parts for {item_name} ({artifact_type}), creating without definition")
                create_payload = {
                    "displayName": item_name,
                    "type": artifact_type,
                }
                # For Lakehouses, detect if primary is schema-enabled and replicate that
                if artifact_type == "Lakehouse":
                    try:
                        lh_props = fabric_api("GET", f"/workspaces/{p_id}/lakehouses/{item_id}")
                        if "defaultSchema" in lh_props.get("properties", {}):
                            create_payload["creationPayload"] = {"enableSchemas": True}
                            logger.info(f"Lakehouse {item_name} is schema-enabled, creating with enableSchemas=true")
                    except Exception as e:
                        logger.warning(f"Could not check lakehouse schema status for {item_name}: {e}")
                s_folder = _get_folder_id_for_item(item, folder_map)
                if s_folder:
                    create_payload["folderId"] = s_folder
                fabric_api("POST", f"/workspaces/{s_id}/items", payload=create_payload, timeout=60)
                results.append({"name": item_name, "status": "created_empty"})
            else:
                # Create item in secondary with definition
                create_payload = {
                    "displayName": item_name,
                    "type": artifact_type,
                    "definition": definition,
                }
                s_folder = _get_folder_id_for_item(item, folder_map)
                if s_folder:
                    create_payload["folderId"] = s_folder
                fabric_api("POST", f"/workspaces/{s_id}/items", payload=create_payload, timeout=120)
                results.append({"name": item_name, "status": "replicated"})

            logger.info(f"Replicated {artifact_type}: {item_name}")
            _update_sync_progress(artifact_type, current=idx + 1,
                                  current_item=f"Done: {item_name}", results=list(results))

            # For Lakehouse, replicate table schemas after creating the item
            if artifact_type == "Lakehouse":
                # Find the newly created lakehouse in secondary
                _cache.pop(f"items:{s_id}", None)
                _cache_ttl.pop(f"items:{s_id}", None)
                new_s_items = get_workspace_items(s_id)
                new_lh = next(
                    (i for i in new_s_items
                     if i.get("type") == "Lakehouse" and i.get("displayName") == item_name),
                    None,
                )
                if new_lh:
                    s_lh_id = new_lh.get("id")
                    table_result = _replicate_lakehouse_tables(p_id, s_id, item_id, s_lh_id, item_name)
                    logger.info(f"Lakehouse {item_name} table replication: {table_result}")

            # For Report, rebind to the secondary SemanticModel
            if artifact_type == "Report":
                _rebind_report_to_secondary(p_id, s_id, item_name)

            # For MLExperiment, copy OneLake data via azcopy
            # NOTE: Skip azcopy for MLModel — copying data with primary model-version UUIDs
            # causes Fabric's MLflow service to detect inconsistency and delete the item.
            # MLModel is replicated by definition only; actual artifacts live in MLExperiment.
            if artifact_type == "MLExperiment" and _check_azcopy_available():
                _cache.pop(f"items:{s_id}", None)
                _cache_ttl.pop(f"items:{s_id}", None)
                new_s_items = get_workspace_items(s_id)
                new_ml = next(
                    (i for i in new_s_items
                     if i.get("type") == artifact_type and i.get("displayName") == item_name),
                    None,
                )
                if new_ml:
                    s_ml_id = new_ml["id"]
                    _update_artifact_csv(item_name, artifact_type, item_id, s_ml_id)
                    _update_sync_progress(artifact_type, current_item=f"Copying data for {item_name}...")
                    try:
                        src = f"https://onelake.dfs.fabric.microsoft.com/{p_id}/{item_id}/*"
                        dst = f"https://onelake.dfs.fabric.microsoft.com/{s_id}/{s_ml_id}"
                        azcopy_bin = _get_azcopy_cmd()
                        env = os.environ.copy()
                        env["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
                        proc = subprocess.run(
                            [azcopy_bin, "copy", src, dst, "--recursive",
                             "--overwrite=ifSourceNewer",
                             "--exclude-pattern=.platform",
                             "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com"],
                            capture_output=True, text=True, timeout=600, env=env,
                        )
                        if proc.returncode == 0:
                            logger.info(f"azcopy ML data for {item_name}: success")
                        else:
                            logger.warning(f"azcopy ML data for {item_name}: rc={proc.returncode} {proc.stderr[:200]}")
                    except Exception as az_err:
                        logger.warning(f"azcopy ML data for {item_name} failed: {az_err}")

            # For Environment, publish after create/update to activate Spark settings & libraries
            if artifact_type == "Environment":
                _cache.pop(f"items:{s_id}", None)
                _cache_ttl.pop(f"items:{s_id}", None)
                new_s_items = get_workspace_items(s_id)
                new_env = next(
                    (i for i in new_s_items
                     if i.get("type") == "Environment" and i.get("displayName") == item_name),
                    None,
                )
                if new_env:
                    s_env_id = new_env["id"]
                    _update_sync_progress(artifact_type, current_item=f"Publishing {item_name}...")
                    try:
                        fabric_api(
                            "POST",
                            f"/workspaces/{s_id}/environments/{s_env_id}/staging/publish?beta=false",
                            timeout=30,
                        )
                        logger.info(f"Publish triggered for Environment: {item_name}")
                        # Poll for publish completion (up to 5 min)
                        _poll_environment_publish(s_id, s_env_id, item_name, logger, timeout=300)
                    except Exception as pub_err:
                        # 202 Accepted is expected for LRO
                        if "202" in str(pub_err):
                            logger.info(f"Publish accepted (LRO) for {item_name}")
                            _poll_environment_publish(s_id, s_env_id, item_name, logger, timeout=300)
                        else:
                            logger.warning(f"Publish failed for {item_name}: {pub_err}")

        except Exception as e:
            logger.error(f"Failed to replicate {item_name}: {e}")
            results.append({"name": item_name, "status": "failed", "error": str(e)})
            _update_sync_progress(artifact_type, current=idx + 1,
                                  current_item=f"Failed: {item_name}", results=list(results))

    # Clear cache so next refresh shows updated data
    _cache.pop(f"items:{s_id}", None)
    _cache_ttl.pop(f"items:{s_id}", None)

    succeeded = sum(1 for r in results if r["status"] != "failed")
    return {
        "status": "ok",
        "message": f"Replicated {succeeded}/{len(to_replicate)} {artifact_type} items",
        "replicated": succeeded,
        "total": len(to_replicate),
        "details": results,
    }


def load_sync_logs(limit: int = 50) -> List[Dict[str, Any]]:
    """Load recent sync event logs from log files + live events."""
    events = []
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    if os.path.exists(log_dir):
        for log_file in sorted(os.listdir(log_dir), reverse=True)[:3]:
            if log_file.endswith('.log'):
                try:
                    with open(os.path.join(log_dir, log_file), 'r') as f:
                        for line in f.readlines()[-limit:]:
                            if line.strip():
                                events.append({
                                    'timestamp': datetime.now().isoformat(),
                                    'message': line.strip(),
                                    'source': 'Sync Log',
                                    'level': 'INFO',
                                })
                except Exception:
                    pass

    # Add live status events
    events.append({
        'timestamp': datetime.now().isoformat(),
        'message': f'Dashboard connected — Primary: {_ws_name("primary")}, Secondary: {_ws_name("secondary")}',
        'source': 'Dashboard',
        'level': 'SUCCESS',
    })
    return sorted(events, key=lambda x: x['timestamp'], reverse=True)[:limit]


def load_sync_plan() -> Dict[str, Any]:
    """Build a live drift analysis comparing primary/secondary workspace items,
    including workspace permissions and security settings."""
    from concurrent.futures import ThreadPoolExecutor
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")

    # Parallel fetch of workspace items
    if p_id and s_id:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_p = pool.submit(get_workspace_items, p_id)
            f_s = pool.submit(get_workspace_items, s_id)
        all_p_items = f_p.result()
        all_s_items = f_s.result()
    else:
        all_p_items = get_workspace_items(p_id) if p_id else []
        all_s_items = get_workspace_items(s_id) if s_id else []
    p_items = _filter_business_items(all_p_items)
    s_items = _filter_business_items(all_s_items)
    system_items = _get_bcdr_system_items(all_s_items)

    # Build lookup maps by displayName
    p_map = {}
    for i in p_items:
        name = i.get("displayName", "")
        p_map[name] = {"id": i.get("id"), "type": i.get("type"), "displayName": name}
    s_map = {}
    for i in s_items:
        name = i.get("displayName", "")
        s_map[name] = {"id": i.get("id"), "type": i.get("type"), "displayName": name}

    in_sync = []
    missing_in_secondary = []
    extra_in_secondary = []
    type_mismatch = []

    for name, p_info in p_map.items():
        if name in s_map:
            s_info = s_map[name]
            if p_info["type"] == s_info["type"]:
                in_sync.append({
                    "displayName": name, "type": p_info["type"],
                    "primary_id": p_info["id"], "secondary_id": s_info["id"],
                })
            else:
                type_mismatch.append({
                    "displayName": name,
                    "primary_type": p_info["type"], "secondary_type": s_info["type"],
                    "primary_id": p_info["id"], "secondary_id": s_info["id"],
                })
        else:
            missing_in_secondary.append({
                "displayName": name, "type": p_info["type"], "id": p_info["id"],
            })

    for name, s_info in s_map.items():
        if name not in p_map:
            extra_in_secondary.append({
                "displayName": name, "type": s_info["type"], "id": s_info["id"],
            })

    # --- Workspace Permissions drift ---
    perm_in_sync = []
    perm_missing = []
    perm_extra = []
    perm_mismatch = []

    if p_id and s_id:
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_pr = pool.submit(fabric_api, "GET", f"/workspaces/{p_id}/roleAssignments")
                f_sr = pool.submit(fabric_api, "GET", f"/workspaces/{s_id}/roleAssignments")
            p_roles_raw = f_pr.result()
            s_roles_raw = f_sr.result()
            p_roles = p_roles_raw.get("value", []) if isinstance(p_roles_raw, dict) else []
            s_roles = s_roles_raw.get("value", []) if isinstance(s_roles_raw, dict) else []

            def _perm_key(r):
                principal = r.get("principal", {})
                return principal.get("id", "") or principal.get("displayName", "")

            def _perm_display(r):
                principal = r.get("principal", {})
                return principal.get("displayName") or principal.get("id", "Unknown")

            p_perm_map = {}
            for r in p_roles:
                key = _perm_key(r)
                if key:
                    p_perm_map[key] = {"role": r.get("role", ""), "display": _perm_display(r), "type": r.get("principal", {}).get("type", "")}
            s_perm_map = {}
            for r in s_roles:
                key = _perm_key(r)
                if key:
                    s_perm_map[key] = {"role": r.get("role", ""), "display": _perm_display(r), "type": r.get("principal", {}).get("type", "")}

            for key, p_info in p_perm_map.items():
                if key in s_perm_map:
                    s_info = s_perm_map[key]
                    if p_info["role"] == s_info["role"]:
                        perm_in_sync.append({
                            "displayName": p_info["display"], "type": "WorkspacePermission",
                            "primary_id": p_info["role"], "secondary_id": s_info["role"],
                        })
                    else:
                        perm_mismatch.append({
                            "displayName": p_info["display"],
                            "primary_type": p_info["role"], "secondary_type": s_info["role"],
                            "primary_id": p_info["role"], "secondary_id": s_info["role"],
                            "principal_id": key, "principal_type": p_info["type"],
                        })
                else:
                    perm_missing.append({
                        "displayName": p_info["display"], "type": "WorkspacePermission",
                        "id": p_info["role"],
                        "principal_id": key, "principal_type": p_info["type"],
                    })

            for key, s_info in s_perm_map.items():
                if key not in p_perm_map:
                    perm_extra.append({
                        "displayName": s_info["display"], "type": "WorkspacePermission",
                        "id": s_info["role"],
                    })
        except Exception as e:
            logger.warning(f"Permission drift check failed: {e}")

    # Add permission drift items into the main arrays
    in_sync.extend(perm_in_sync)
    missing_in_secondary.extend(perm_missing)
    extra_in_secondary.extend(perm_extra)
    type_mismatch.extend(perm_mismatch)

    # --- Sensitivity Label drift (Microsoft Information Protection) ---
    # Use sensitivity label data already present in the list API response
    # (no per-item GET calls needed — avoids rate limiting)
    sec_in_sync = []
    sec_missing = []

    if p_id and s_id:
        try:
            # Build label lookup from already-fetched list data
            p_label_map = {}
            for i in p_items:
                name = i.get("displayName", "")
                sl = i.get("sensitivityLabel") or {}
                label_id = sl.get("labelId") or sl.get("sensitivityLabelId") or ""
                if label_id:
                    p_label_map[name] = label_id

            s_label_map = {}
            for i in s_items:
                name = i.get("displayName", "")
                sl = i.get("sensitivityLabel") or {}
                label_id = sl.get("labelId") or sl.get("sensitivityLabelId") or ""
                if label_id:
                    s_label_map[name] = label_id

            for item in in_sync:
                if item.get("type") == "WorkspacePermission":
                    continue
                name = item.get("displayName", "")
                p_label = p_label_map.get(name, "")
                s_label = s_label_map.get(name, "")
                if p_label or s_label:
                    if p_label == s_label:
                        sec_in_sync.append({
                            "displayName": name + " (Sensitivity Label)",
                            "type": "SensitivityLabel",
                            "primary_id": p_label or "none",
                            "secondary_id": s_label or "none",
                        })
                    else:
                        sec_missing.append({
                            "displayName": name + " (Label Mismatch)",
                            "type": "SensitivityLabel",
                            "id": f"primary={p_label or 'none'} secondary={s_label or 'none'}",
                        })
        except Exception as e:
            logger.warning(f"Sensitivity label drift check failed: {e}")

    in_sync.extend(sec_in_sync)
    missing_in_secondary.extend(sec_missing)

    # --- Annotate in-sync items with last-known definition hash status ---
    definition_changed = []
    pair_key = f"{p_id}:{s_id}" if p_id and s_id else ""
    saved_hashes = _artifact_hashes.get(pair_key, {})
    for item in in_sync:
        name = item.get("displayName", "")
        item_type = item.get("type", "")
        if name in saved_hashes:
            h = saved_hashes[name]
            item["definition_changed"] = h.get("changed", False)
            item["last_hash_check"] = h.get("checked_at", "")
            if h.get("changed"):
                definition_changed.append(item)
        elif item_type in _HASHABLE_TYPES:
            item["definition_changed"] = None  # Not yet checked
            item["last_hash_check"] = None
        # Non-hashable types: leave fields absent

    return {
        "summary": {
            "in_sync_count": len(in_sync),
            "missing_in_secondary_count": len(missing_in_secondary),
            "type_mismatch_count": len(type_mismatch),
            "extra_in_secondary_count": len(extra_in_secondary),
            "definition_changed_count": len(definition_changed),
        },
        "sync_plan": {
            "IN_SYNC": in_sync,
            "MISSING_IN_SECONDARY": missing_in_secondary,
            "EXTRA_IN_SECONDARY": extra_in_secondary,
            "TYPE_MISMATCH": type_mismatch,
            "DEFINITION_CHANGED": definition_changed,
        },
        "primary_total": len(p_items),
        "secondary_total": len(s_items),
        "system_artifacts": [{"name": i.get("displayName"), "type": i.get("type")} for i in system_items],
    }


# ============================================================================
# AUTH-GUARD DECORATOR
# ============================================================================

def _require_setup():
    """Redirect to login if not authenticated, or to workspace setup if no pairs configured."""
    if not is_authenticated():
        return redirect(url_for('login_page'))
    if not _workspace_state.get("pairs"):
        return redirect(url_for('workspace_setup'))
    return None


# ============================================================================
# AUTH ROUTES
# ============================================================================

@app.route('/login')
def login_page():
    """Show login page with sign-in button."""
    if is_authenticated():
        return redirect(url_for('workspace_setup'))
    return render_template('login.html')


@app.route('/api/auth/start', methods=['POST'])
def api_auth_start():
    """Start interactive browser login or Service Principal login."""
    if _auth_state.get("login_in_progress"):
        return jsonify({"status": "already_in_progress"})

    data = request.get_json() or {}
    mode = data.get("mode", "interactive")

    if mode == "service_principal":
        tenant_id = (data.get("tenant_id") or "").strip()
        client_id = (data.get("client_id") or "").strip()
        client_secret = (data.get("client_secret") or "").strip()
        if not all([tenant_id, client_id, client_secret]):
            return jsonify({"error": "Tenant ID, Client ID, and Client Secret are required"}), 400
        try:
            t = threading.Thread(target=_do_sp_login, args=(tenant_id, client_id, client_secret), daemon=True)
            t.start()
            return jsonify({"status": "started", "mode": "service_principal"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        try:
            t = threading.Thread(target=_do_interactive_login, daemon=True)
            t.start()
            return jsonify({"status": "started", "mode": "interactive"})
        except Exception as e:
            logger.exception("Failed to start interactive login")
            return jsonify({"error": str(e)}), 500


@app.route('/api/auth/status', methods=['GET'])
def api_auth_status():
    """Check current auth status — frontend polls this."""
    return jsonify({
        "authenticated": is_authenticated(),
        "in_progress": _auth_state.get("login_in_progress", False),
        "error": _auth_state.get("login_error"),
        "user_name": _auth_state.get("user_name"),
        "user_email": _auth_state.get("user_email"),
        "auth_mode": _auth_state.get("auth_mode"),
        "sp_configured": bool(_sp_config.get("client_id")),
    })


@app.route('/logout')
def logout():
    """Clear auth state and remove cached tokens."""
    _auth_state.update({
        "access_token": None, "token_expiry": 0,
        "user_name": None, "user_email": None,
        "login_in_progress": False, "login_error": None,
        "msal_app": None, "accounts": None,
        "auth_mode": None,
    })
    _workspace_state.update({
        "primary_id": None, "primary_name": None,
        "secondary_id": None, "secondary_name": None,
        "all_workspaces": [],
    })
    # Remove the MSAL token cache file so silent re-auth doesn't happen
    try:
        if os.path.exists(_TOKEN_CACHE_FILE):
            os.remove(_TOKEN_CACHE_FILE)
    except Exception:
        pass
    return redirect(url_for('login_page'))


# ============================================================================
# WORKSPACE SETUP ROUTES
# ============================================================================

@app.route('/setup')
def workspace_setup():
    """Show workspace picker page."""
    if not is_authenticated():
        return redirect(url_for('login_page'))
    return render_template('setup.html',
                           user_name=_auth_state.get("user_name", "User"),
                           user_email=_auth_state.get("user_email", ""))


@app.route('/api/workspaces', methods=['GET'])
def api_workspaces():
    """List all workspaces from Fabric API."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        workspaces = list_all_workspaces()
        return jsonify({"workspaces": [
            {"id": w.get("id"), "name": w.get("displayName", "Untitled"),
             "description": w.get("description", "")}
            for w in workspaces
        ]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/workspaces/select', methods=['POST'])
def api_workspaces_select():
    """Add a new workspace pair (or update active pair if pair_id is supplied)."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json()
    pair_id = data.get("pair_id")  # If provided, update existing pair
    primary_id = data.get("primary_id")
    primary_name = data.get("primary_name")
    secondary_id = data.get("secondary_id") or None
    secondary_name = data.get("secondary_name") or None
    label = data.get("label") or primary_name or "Workspace Pair"

    if pair_id:
        # Update existing pair
        pair = _get_pair(pair_id)
        if pair:
            pair["primary_id"] = primary_id
            pair["primary_name"] = primary_name
            pair["secondary_id"] = secondary_id
            pair["secondary_name"] = secondary_name
            pair["label"] = label
    else:
        # Add new pair
        pair_id = str(uuid.uuid4())[:8]
        new_pair = {
            "id": pair_id,
            "label": label,
            "primary_id": primary_id,
            "primary_name": primary_name,
            "secondary_id": secondary_id,
            "secondary_name": secondary_name,
        }
        _workspace_state["pairs"].append(new_pair)

    _workspace_state["active_pair"] = pair_id
    _save_workspace_state()
    return jsonify({"status": "ok", "pair_id": pair_id})


@app.route('/api/workspace-pairs', methods=['GET'])
def api_workspace_pairs():
    """Return workspace pairs with optional search and pagination.
    Query params: q (search), page (1-based), page_size (default 50).
    """
    all_pairs = _workspace_state.get("pairs", [])
    q = (request.args.get("q") or "").strip().lower()
    page = request.args.get("page", 1, type=int)
    page_size = request.args.get("page_size", 50, type=int)
    page_size = min(max(page_size, 1), 500)

    # Filter by search query
    if q:
        filtered = [p for p in all_pairs
                     if q in (p.get("label", "") or "").lower()
                     or q in (p.get("primary_name", "") or "").lower()
                     or q in (p.get("secondary_name", "") or "").lower()]
    else:
        filtered = all_pairs

    total = len(filtered)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_pairs = filtered[start:start + page_size]

    pairs_out = []
    for p in page_pairs:
        pairs_out.append({
            "id": p["id"],
            "label": p.get("label", ""),
            "primary_id": p.get("primary_id"),
            "primary_name": p.get("primary_name"),
            "secondary_id": p.get("secondary_id"),
            "secondary_name": p.get("secondary_name"),
            "dr_state": p.get("dr_state", "normal"),
        })
    return jsonify({
        "pairs": pairs_out,
        "active_pair": _workspace_state.get("active_pair"),
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    })


@app.route('/api/workspace-pairs/active', methods=['POST'])
def api_workspace_pairs_set_active():
    """Switch the active workspace pair."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    pair_id = data.get("pair_id")
    if not _get_pair(pair_id):
        return jsonify({"error": "Pair not found"}), 404
    _workspace_state["active_pair"] = pair_id
    _save_workspace_state()
    # Clear cache so next request fetches fresh data for new pair
    _cache.clear()
    _cache_ttl.clear()
    return jsonify({"status": "ok", "active_pair": pair_id})


@app.route('/api/workspace-pairs/<pair_id>', methods=['DELETE'])
def api_workspace_pairs_delete(pair_id):
    """Remove a workspace pair."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    pairs = _workspace_state.get("pairs", [])
    new_pairs = [p for p in pairs if p.get("id") != pair_id]
    if len(new_pairs) == len(pairs):
        return jsonify({"error": "Pair not found"}), 404
    _workspace_state["pairs"] = new_pairs
    # If active pair was deleted, switch to first remaining
    if _workspace_state.get("active_pair") == pair_id:
        _workspace_state["active_pair"] = new_pairs[0]["id"] if new_pairs else None
    _save_workspace_state()
    _cache.clear()
    _cache_ttl.clear()
    return jsonify({"status": "ok"})


@app.route('/api/active-pair-info', methods=['GET'])
def api_active_pair_info():
    """Return enriched info about the active workspace pair for the context banner."""
    pair = _active_pair()
    if not pair:
        return jsonify({"configured": False})
    p_id = pair.get("primary_id", "")
    s_id = pair.get("secondary_id", "")
    p_region = _get_workspace_region(p_id) if p_id else {}
    s_region = _get_workspace_region(s_id) if s_id else {}
    return jsonify({
        "configured": True,
        "id": pair["id"],
        "label": pair.get("label", ""),
        "primary_name": pair.get("primary_name", ""),
        "secondary_name": pair.get("secondary_name", ""),
        "dr_state": pair.get("dr_state", "normal"),
        "primary_region": p_region.get("region", ""),
        "secondary_region": s_region.get("region", ""),
        "primary_capacity": p_region.get("capacity_name", ""),
        "secondary_capacity": s_region.get("capacity_name", ""),
    })


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.route('/api/health', methods=['GET'])
def api_health():
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        p_id = _ws_id("primary")
        s_id = _ws_id("secondary")
        primary = get_workspace_health(p_id) if p_id else {}
        secondary = get_workspace_health(s_id) if s_id else {}
        return jsonify({
            'status': 'NOMINAL' if primary.get('health', 0) > 90 else 'DEGRADED',
            'primary': primary,
            'secondary': secondary,
            'timestamp': datetime.now().isoformat(),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/topology', methods=['GET'])
def api_topology():
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        topo = get_regional_topology()

        # Paginated all-pairs summary
        all_cfg_pairs = _workspace_state.get("pairs", [])
        page = request.args.get("page", 1, type=int)
        page_size = request.args.get("page_size", 25, type=int)
        page_size = min(max(page_size, 1), 100)
        total = len(all_cfg_pairs)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        page_pairs = all_cfg_pairs[start:start + page_size]

        all_pairs_summary = []
        for pair in page_pairs:
            p_id = pair.get("primary_id")
            s_id = pair.get("secondary_id")
            p_count = len(_filter_business_items(get_workspace_items(p_id))) if p_id else 0
            s_count = len(_filter_business_items(get_workspace_items(s_id))) if s_id else 0
            sync_pct = (min(s_count, p_count) / max(p_count, 1)) * 100 if p_count else 0
            p_region = _get_workspace_region(p_id) if p_id else {}
            s_region = _get_workspace_region(s_id) if s_id else {}
            all_pairs_summary.append({
                "id": pair["id"],
                "label": pair.get("label", ""),
                "primary_name": pair.get("primary_name", ""),
                "secondary_name": pair.get("secondary_name", ""),
                "primary_region": p_region.get("region", ""),
                "secondary_region": s_region.get("region", ""),
                "primary_items": p_count,
                "secondary_items": s_count,
                "sync_pct": round(sync_pct, 1),
                "active": pair["id"] == _workspace_state.get("active_pair"),
            })
        topo["all_pairs"] = all_pairs_summary
        topo["pairs_pagination"] = {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
        return jsonify(topo)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/lineage', methods=['GET'])
def api_lineage():
    """Return lineage pairs with live health checks.

    Dynamically matches primary ↔ secondary items by name+type,
    and optionally deep-inspects definitions to detect stale primary IDs.
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        import base64 as b64

        p_id = _ws_id("primary")
        s_id = _ws_id("secondary")
        if not p_id or not s_id:
            return jsonify({"error": "No workspace pair configured"}), 400

        # Build mappings dynamically from live workspace items
        m = _build_dynamic_mappings(p_id, s_id)
        pairs = m["pairs"]
        ref_pairs = m["ref_pairs"]
        unmatched = m["unmatched"]
        s_items = m["s_items"]

        # Deep inspection: check if secondary definitions still contain primary IDs
        deep_check = request.args.get("deep", "false").lower() == "true"
        stale_refs = []
        if deep_check:
            # Build set of primary IDs that should NOT appear in secondary definitions
            primary_ref_set = set()
            for rp in ref_pairs:
                primary_ref_set.add(rp["primary_ref"])
            for ap in pairs:
                primary_ref_set.add(ap["primary_id"])
            primary_ref_set.discard("")

            # Check a sample of secondary items (limit to avoid rate limiting)
            check_types = {"SemanticModel", "Report", "Notebook", "MLModel",
                           "GraphQLApi", "DataPipeline", "SparkJobDefinition"}
            checked = 0
            max_checks = int(request.args.get("max_checks", "10"))
            for si in s_items:
                if si.get("type") not in check_types or checked >= max_checks:
                    continue
                try:
                    resp = fabric_api(
                        "POST",
                        f"/workspaces/{s_id}/items/{si['id']}/getDefinition",
                        timeout=120,
                    )
                    defn = resp.get("definition", {}) if resp else {}
                    parts = defn.get("parts", [])
                    for part in parts:
                        payload = part.get("payload", "")
                        if part.get("payloadType") == "InlineBase64" and payload:
                            try:
                                decoded = b64.b64decode(payload).decode("utf-8", errors="replace")
                            except Exception:
                                decoded = ""
                        else:
                            decoded = payload
                        for pref in primary_ref_set:
                            if pref and pref in decoded:
                                stale_refs.append({
                                    "item_name": si["displayName"],
                                    "item_type": si.get("type", "?"),
                                    "item_id": si["id"],
                                    "stale_ref": pref,
                                    "part_path": part.get("path", "?"),
                                })
                    checked += 1
                except Exception:
                    pass  # definition export may not be supported for all types

        # Summary counts
        total_pairs = len(pairs)
        healthy = sum(1 for p in pairs if p["primary_exists"] and p["secondary_exists"])
        missing_secondary = sum(1 for p in pairs if p["primary_exists"] and not p["secondary_exists"])
        missing_primary = sum(1 for p in pairs if not p["primary_exists"] and p["secondary_exists"])

        return jsonify({
            "pairs": pairs,
            "reference_mapping": ref_pairs,
            "unmatched_items": unmatched,
            "stale_refs": stale_refs,
            "deep_checked": deep_check,
            "summary": {
                "total_pairs": total_pairs,
                "healthy": healthy,
                "missing_secondary": missing_secondary,
                "missing_primary": missing_primary,
                "unmatched_count": len(unmatched),
                "stale_count": len(stale_refs),
            },
            "primary_workspace_id": p_id,
            "secondary_workspace_id": s_id,
        })
    except Exception as e:
        logger.error(f"Lineage API error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


_lineage_conn_cache: Dict[str, Any] = {}
_lineage_conn_ts: float = 0.0
_LINEAGE_CACHE_TTL = 1800  # 30 minutes


@app.route('/api/lineage/connections', methods=['GET'])
def api_lineage_connections():
    """Inspect secondary definitions to discover actual artifact-to-artifact connections.

    Returns cached results instantly if available (< 10 min old).
    Pass ?refresh=true to force a fresh scan.
    """
    global _lineage_conn_cache, _lineage_conn_ts
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        import csv, base64 as b64

        refresh = request.args.get("refresh", "false").lower() == "true"
        now = time.time()

        # Return cached result if fresh
        if not refresh and _lineage_conn_cache and (now - _lineage_conn_ts) < _LINEAGE_CACHE_TTL:
            result = dict(_lineage_conn_cache)
            result["cached"] = True
            result["scanned_at"] = datetime.fromtimestamp(_lineage_conn_ts).isoformat()
            result["cache_age_sec"] = int(now - _lineage_conn_ts)
            return jsonify(result)

        p_id = _ws_id("primary")
        s_id = _ws_id("secondary")
        if not p_id or not s_id:
            return jsonify({"error": "No workspace pair configured"}), 400

        # Load live items from both workspaces
        p_items = _filter_business_items(get_workspace_items(p_id))
        s_items = _filter_business_items(get_workspace_items(s_id))

        # Build lookup tables: id → {name, type, workspace}
        id_lookup = {}
        for i in p_items:
            id_lookup[i["id"]] = {"name": i["displayName"], "type": i.get("type", "?"), "workspace": "primary"}
        for i in s_items:
            id_lookup[i["id"]] = {"name": i["displayName"], "type": i.get("type", "?"), "workspace": "secondary"}

        # Build primary↔secondary ID mappings dynamically from live items
        m = _build_dynamic_mappings(p_id, s_id)
        primary_to_secondary = m["primary_to_secondary"]
        secondary_to_primary = m["secondary_to_primary"]

        # All known IDs (both primary and secondary) for scanning
        all_known_ids = set(id_lookup.keys())
        # Add workspace IDs
        all_known_ids.add(p_id)
        all_known_ids.add(s_id)

        # Types worth inspecting (have definition references to other artifacts)
        inspect_types = {"SemanticModel", "Report", "Notebook", "MLModel",
                         "MLExperiment", "GraphQLApi", "DataPipeline",
                         "SparkJobDefinition", "Environment", "Eventstream",
                         "KQLQueryset", "DataAgent", "Eventhouse",
                         "KQLDatabase", "GraphModel", "Ontology"}

        connections = []
        nodes = {}  # id → node info for the graph
        errors = []

        # Build node entries for all secondary items
        for si in s_items:
            nodes[si["id"]] = {
                "id": si["id"],
                "name": si["displayName"],
                "type": si.get("type", "?"),
            }

        max_inspect = int(request.args.get("max_inspect", "20"))
        inspected = 0

        # Prioritize non-Notebook types first so DataAgents, MLModels, SM, Reports
        # get inspected before the potentially large set of notebooks
        priority_types = {"GraphQLApi", "MLModel", "MLExperiment", "SemanticModel",
                          "Report", "DataPipeline", "SparkJobDefinition",
                          "Environment", "Eventstream", "KQLQueryset",
                          "DataAgent", "Eventhouse", "KQLDatabase",
                          "GraphModel", "Ontology"}
        inspectable = [si for si in s_items if si.get("type", "") in inspect_types]
        inspectable.sort(key=lambda si: (0 if si.get("type", "") in priority_types else 1, si.get("type", "")))
        inspectable = inspectable[:max_inspect]

        # ── Bulk definition export (single API call) with per-item fallback ──
        from concurrent.futures import ThreadPoolExecutor, as_completed

        fetch_results = []  # list of (item, parts_list, error_str_or_None)
        items_by_id = {si["id"]: si for si in inspectable}

        # Try bulk API first
        bulk_ok = False
        try:
            bulk_item_ids = [si["id"] for si in inspectable]
            logger.info(f"Lineage: attempting bulk export for {len(bulk_item_ids)} items")
            bulk_resp = fabric_api(
                "POST",
                f"/workspaces/{s_id}/items/bulkExportItemDefinitions",
                payload={"itemIds": bulk_item_ids},
                timeout=300,
            )
            logger.info(f"Lineage: bulk response keys={list((bulk_resp or {}).keys())}")
            if bulk_resp and "itemDefinitions" in bulk_resp:
                bulk_ok = True
                returned_ids = set()
                for item_def in bulk_resp["itemDefinitions"]:
                    iid = item_def.get("id", "")
                    returned_ids.add(iid)
                    si = items_by_id.get(iid)
                    if si:
                        parts = item_def.get("definition", {}).get("parts", [])
                        fetch_results.append((si, parts, None))
                # Items not in bulk response — no definition available
                for si in inspectable:
                    if si["id"] not in returned_ids:
                        fetch_results.append((si, None, "Not in bulk response"))
                logger.info(f"Lineage: bulk export returned {len(returned_ids)}/{len(bulk_item_ids)} definitions")
        except Exception as bulk_err:
            logger.info(f"Lineage: bulk export not available ({type(bulk_err).__name__}: {bulk_err}), using per-item")

        # Fallback: per-item getDefinition (only if bulk failed)
        if not bulk_ok:
            def _fetch_definition(item):
                try:
                    resp = fabric_api(
                        "POST",
                        f"/workspaces/{s_id}/items/{item['id']}/getDefinition",
                        timeout=30,
                    )
                    defn = resp.get("definition", {}) if resp else {}
                    return (item, defn.get("parts", []), None)
                except Exception as e:
                    return (item, None, str(e))

            with ThreadPoolExecutor(max_workers=12) as executor:
                futures = {executor.submit(_fetch_definition, si): si for si in inspectable}
                for future in as_completed(futures):
                    fetch_results.append(future.result())

        for si, parts, error in fetch_results:
            if error:
                errors.append({"item": si["displayName"], "error": error})
                continue
            if parts is None:
                continue
            item_type = si.get("type", "")
            inspected += 1
            logger.debug(f"Lineage: {si['displayName']} ({item_type}) — {len(parts)} parts")

            for part in parts:
                payload = part.get("payload", "")
                if part.get("payloadType") == "InlineBase64" and payload:
                    try:
                        decoded = b64.b64decode(payload).decode("utf-8", errors="replace")
                    except Exception:
                        decoded = ""
                else:
                    decoded = payload

                if not decoded:
                    continue

                # Scan for known artifact IDs using regex extraction (O(len(decoded)) instead of O(N*M))
                import re
                _UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
                found_ids = set(_UUID_RE.findall(decoded))
                for known_id in found_ids & all_known_ids:
                    if not known_id or known_id == si["id"]:
                        continue

                    target_info = id_lookup.get(known_id, {})
                    target_name = target_info.get("name", "?")
                    target_type = target_info.get("type", "?")
                    target_ws = target_info.get("workspace", "?")

                    # Skip workspace IDs themselves — track separately
                    if known_id == p_id:
                        # Found primary workspace ID in secondary definition = STALE
                        connections.append({
                            "source_id": si["id"],
                            "source_name": si["displayName"],
                            "source_type": item_type,
                            "target_id": known_id,
                            "target_name": "(Primary Workspace ID)",
                            "target_type": "WorkspaceId",
                            "status": "stale",
                            "detail": "Secondary definition references primary workspace ID",
                            "part_path": part.get("path", "?"),
                        })
                        continue
                    if known_id == s_id:
                        continue  # Expected — secondary workspace ID in secondary def

                    # Determine if this is the correct secondary ref or a stale primary ref
                    is_secondary_item = target_ws == "secondary"
                    is_primary_item = target_ws == "primary"
                    # Check if this primary ID should have been remapped
                    expected_secondary = primary_to_secondary.get(known_id)

                    if is_secondary_item:
                        status = "healthy"
                        detail = f"Correctly references secondary {target_type}"
                    elif is_primary_item and expected_secondary:
                        status = "stale"
                        detail = f"References PRIMARY {target_name} — should be {expected_secondary[:8]}..."
                    elif is_primary_item:
                        status = "stale"
                        detail = f"References PRIMARY {target_name} — no mapping found"
                    else:
                        status = "unknown"
                        detail = "Reference to unknown ID"

                    # Avoid duplicate edges (same source→target)
                    edge_key = f"{si['id']}→{known_id}"
                    if any(c.get("_key") == edge_key for c in connections):
                        continue

                    connections.append({
                        "_key": edge_key,
                        "source_id": si["id"],
                        "source_name": si["displayName"],
                        "source_type": item_type,
                        "target_id": known_id,
                        "target_name": target_name,
                        "target_type": target_type,
                        "status": status,
                        "detail": detail,
                        "part_path": part.get("path", "?"),
                    })

        # Clean up internal keys
        for c in connections:
            c.pop("_key", None)

        # Summary
        healthy_count = sum(1 for c in connections if c["status"] == "healthy")
        stale_count = sum(1 for c in connections if c["status"] == "stale")

        result = {
            "connections": connections,
            "nodes": nodes,
            "inspected_count": inspected,
            "summary": {
                "total_connections": len(connections),
                "healthy": healthy_count,
                "stale": stale_count,
            },
            "errors": errors,
        }

        # Cache the result
        _lineage_conn_cache = result
        _lineage_conn_ts = time.time()

        result_with_meta = dict(result)
        result_with_meta["cached"] = False
        result_with_meta["scanned_at"] = datetime.fromtimestamp(_lineage_conn_ts).isoformat()
        result_with_meta["cache_age_sec"] = 0

        return jsonify(result_with_meta)
    except Exception as e:
        logger.error(f"Lineage connections API error: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@app.route('/api/inventory', methods=['GET'])
def api_inventory():
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        inv = get_artifact_inventory()
        # Paginated all-pairs inventory
        all_cfg_pairs = _workspace_state.get("pairs", [])
        page = request.args.get("page", 1, type=int)
        page_size = request.args.get("page_size", 25, type=int)
        page_size = min(max(page_size, 1), 100)
        total = len(all_cfg_pairs)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        page_pairs = all_cfg_pairs[start:start + page_size]

        all_pairs_inv = []
        for pair in page_pairs:
            p_id = pair.get("primary_id")
            s_id = pair.get("secondary_id")
            p_items = _filter_business_items(get_workspace_items(p_id)) if p_id else []
            s_items = _filter_business_items(get_workspace_items(s_id)) if s_id else []
            def by_type(items):
                d = {}
                for i in items:
                    t = i.get("type", "Unknown")
                    d[t] = d.get(t, 0) + 1
                return d
            all_pairs_inv.append({
                "id": pair["id"],
                "label": pair.get("label", ""),
                "primary_name": pair.get("primary_name", ""),
                "secondary_name": pair.get("secondary_name", ""),
                "primary_total": len(p_items),
                "secondary_total": len(s_items),
                "primary_by_type": by_type(p_items),
                "secondary_by_type": by_type(s_items),
                "active": pair["id"] == _workspace_state.get("active_pair"),
            })
        inv["all_pairs"] = all_pairs_inv
        inv["pairs_pagination"] = {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
        return jsonify(inv)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/logs', methods=['GET'])
def api_logs():
    try:
        limit = request.args.get('limit', 50, type=int)
        return jsonify({'events': load_sync_logs(limit)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sync-plan', methods=['GET'])
def api_sync_plan():
    try:
        return jsonify(load_sync_plan())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    global _lineage_conn_cache, _lineage_conn_ts, _integrity_cache, _integrity_cache_ts
    _cache.clear()
    _cache_ttl.clear()
    _lineage_conn_cache = {}
    _lineage_conn_ts = 0.0
    _integrity_cache = {}
    _integrity_cache_ts = 0.0
    return jsonify({'status': 'ok'})


@app.route('/api/bcdr/status', methods=['GET'])
def api_bcdr_status():
    """Get Resiliency & Recovery status with artifact type cards."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        return jsonify(get_bcdr_status())
    except Exception as e:
        logger.exception("Error getting Resiliency & Recovery status")
        return jsonify({'error': str(e)}), 500


@app.route('/api/bcdr/bulk-sync', methods=['POST'])
def api_bcdr_bulk_sync():
    """Bulk-sync all workspace definitions using the new Fabric Bulk
    Export / Import Item Definition APIs (beta).  Falls back to per-item
    getDefinition + createItem/updateDefinition automatically.

    Body (optional): { "types": ["Notebook","Report"], "dryRun": false }
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both primary and secondary workspaces must be configured"}), 400

    data = request.get_json() or {}
    item_types = data.get("types")  # optional list
    dry_run = data.get("dryRun", False)

    # Run in background thread
    def _run():
        _update_sync_progress("BulkSync", total=0, current=0,
                              current_item="Starting bulk sync...", status="running")
        try:
            from scripts.bulk_sync import bulk_sync
            result = bulk_sync(p_id, s_id, logger, dry_run=dry_run, item_types=item_types)
            _update_sync_progress("BulkSync", total=result.get("exported", 0),
                                  current=result.get("imported", 0),
                                  current_item=f"Done — mode={result['mode']}",
                                  status="completed", results=[result])
        except Exception as e:
            logger.exception("Bulk sync failed")
            _update_sync_progress("BulkSync", status="failed",
                                  current_item=f"Error: {str(e)[:200]}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "Bulk definition sync started in background"})


@app.route('/api/bcdr/replicate', methods=['POST'])
def api_bcdr_replicate():
    """Replicate all items of a specific artifact type to secondary (runs in background)."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json()
    artifact_type = data.get("type")
    if not artifact_type:
        return jsonify({"error": "Missing 'type' parameter"}), 400

    # Check if a sync is already running for this type
    with _sync_lock:
        existing = _sync_progress.get(artifact_type, {})
        if existing.get("status") == "running":
            return jsonify({"status": "running", "message": f"{artifact_type} sync already in progress"}), 409

    # Start background replication
    t = threading.Thread(target=_run_replicate_background, args=(artifact_type,), daemon=True)
    t.start()
    return jsonify({"status": "started", "message": f"{artifact_type} sync started in background"})


@app.route('/api/bcdr/replicate/progress', methods=['GET'])
@app.route('/api/bcdr/replicate/progress/<artifact_type>', methods=['GET'])
def api_bcdr_replicate_progress(artifact_type=None):
    """Get sync progress for a specific type or all types."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    with _sync_lock:
        if artifact_type:
            progress = _sync_progress.get(artifact_type, {"status": "idle"})
            return jsonify(progress)
        return jsonify(_sync_progress)


@app.route('/api/bcdr/replicate-item', methods=['POST'])
def api_bcdr_replicate_item():
    """Replicate a single item by ID to secondary workspace."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json()
    item_id = data.get("item_id")
    item_name = data.get("item_name", "Unknown")
    artifact_type = data.get("type")
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400
    if not item_id:
        return jsonify({"error": "Missing 'item_id'"}), 400
    try:
        # Export definition from primary
        export_resp = None
        definition = {}
        parts = []
        try:
            export_resp = fabric_api(
                "POST", f"/workspaces/{p_id}/items/{item_id}/getDefinition",
                timeout=120,
            )
            if export_resp and isinstance(export_resp, dict):
                definition = export_resp.get("definition", {})
                if definition:
                    parts = definition.get("parts", [])
                    logger.info(f"Got {len(parts)} definition parts for {item_name}")
        except Exception as export_err:
            logger.warning(f"Could not export definition for {item_name}: {export_err}")

        # Rewrite connection strings for all artifact types
        if parts:
            try:
                conn_replacements = _build_connection_map(p_id, s_id)
                if conn_replacements:
                    logger.info(f"Rewriting connection references in {item_name}")
                    parts = _rewrite_definition_parts(parts, conn_replacements)
                    definition = dict(definition)
                    definition["parts"] = parts
            except Exception as rw_err:
                logger.warning(f"Connection rewrite failed for {item_name}: {rw_err}")

        # Find the source item to get its folderId
        primary_items = get_workspace_items(p_id)
        source_item = next((i for i in primary_items if i.get("id") == item_id), {})

        # Determine target folder in secondary (only create the folder this item needs)
        folder_map = {}
        try:
            source_fid = source_item.get("folderId")
            if source_fid:
                folder_map = _ensure_folder_structure(p_id, s_id, needed_folder_ids={source_fid})
        except Exception:
            pass

        create_payload = {"displayName": item_name, "type": artifact_type}
        s_folder = _get_folder_id_for_item(source_item, folder_map)
        if s_folder:
            create_payload["folderId"] = s_folder
        if parts:
            create_payload["definition"] = definition
        elif artifact_type in ("SemanticModel", "Report"):
            return jsonify({"error": f"Could not get definition for {item_name}. "
                            f"{artifact_type} requires a definition to replicate."}), 400

        fabric_api("POST", f"/workspaces/{s_id}/items", payload=create_payload, timeout=120)

        # Clear cache
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)

        # For Report, rebind to the secondary SemanticModel
        if artifact_type == "Report":
            _rebind_report_to_secondary(p_id, s_id, item_name)

        return jsonify({"status": "ok", "message": f"Replicated {item_name}"})
    except Exception as e:
        logger.exception(f"Error replicating item {item_name}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# MANAGED FAILOVER & FAILBACK
# ============================================================================
# DR state machine: normal → failing_over → failed_over → failing_back → normal
# Persisted as "dr_state" on each workspace pair.

_DR_EVENT_LOG_FILE = os.path.join(os.path.dirname(__file__), ".dr_events.json")


def _load_dr_events() -> List[Dict]:
    try:
        if os.path.exists(_DR_EVENT_LOG_FILE):
            with open(_DR_EVENT_LOG_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_dr_event(event: Dict):
    events = _load_dr_events()
    events.append(event)
    # Keep last 200 events
    events = events[-200:]
    try:
        with open(_DR_EVENT_LOG_FILE, "w") as f:
            json.dump(events, f, indent=2)
    except Exception:
        pass


def _get_dr_state() -> str:
    """Get DR state for the active pair."""
    pair = _active_pair()
    return pair.get("dr_state", "normal") if pair else "normal"


def _set_dr_state(state: str):
    """Set DR state for the active pair and save."""
    pair = _active_pair()
    if pair:
        pair["dr_state"] = state
        _save_workspace_state()


def _swap_pair_roles():
    """Swap primary ↔ secondary for the active pair and save."""
    pair = _active_pair()
    if not pair:
        return
    pair["primary_id"], pair["secondary_id"] = pair["secondary_id"], pair["primary_id"]
    pair["primary_name"], pair["secondary_name"] = pair["secondary_name"], pair["primary_name"]
    _save_workspace_state()
    # Clear cached items
    _cache.clear()
    _cache_ttl.clear()


def _validate_workspace_readiness(workspace_id: str) -> Dict[str, Any]:
    """Check a workspace is accessible and count its items."""
    try:
        items = _filter_business_items(get_workspace_items(workspace_id))
        by_type: Dict[str, int] = {}
        for i in items:
            t = i.get("type", "Unknown")
            by_type[t] = by_type.get(t, 0) + 1
        return {"ok": True, "item_count": len(items), "by_type": by_type}
    except Exception as e:
        return {"ok": False, "error": str(e), "item_count": 0, "by_type": {}}


def _run_final_sync() -> Dict[str, Any]:
    """Run a final forward sync (primary → secondary) — only for types with missing items."""
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return {"error": "Workspaces not configured"}

    # Clear cache to get fresh item lists
    _cache.pop(f"items:{p_id}", None)
    _cache_ttl.pop(f"items:{p_id}", None)
    _cache.pop(f"items:{s_id}", None)
    _cache_ttl.pop(f"items:{s_id}", None)

    primary_items = get_workspace_items(p_id)
    secondary_items = get_workspace_items(s_id)

    # Build set of (displayName, type) that exist in secondary
    s_names_by_type: Dict[str, set] = {}
    for i in secondary_items:
        t = i.get("type", "")
        s_names_by_type.setdefault(t, set()).add(i.get("displayName"))

    # Only sync types that have items in primary missing from secondary
    SKIP_TYPES = {"Warehouse", "SQLEndpoint"}
    ML_REGISTER_TYPES = {"MLModel"}  # Use definition-based register instead of replicate
    results = {}
    for i in primary_items:
        t = i.get("type", "")
        if t in SKIP_TYPES or t in results or t in ML_REGISTER_TYPES:
            continue
        p_name = i.get("displayName", "")
        if p_name not in s_names_by_type.get(t, set()):
            # This type has at least one missing item — replicate it
            try:
                r = replicate_items_by_type(t)
                results[t] = r
            except Exception as e:
                results[t] = {"status": "error", "message": str(e)}

    # ML Models: use definition-based register (Fabric drops empty placeholders)
    p_ml_models = [i for i in primary_items if i.get("type") == "MLModel"]
    if p_ml_models:
        missing = [m for m in p_ml_models if m["displayName"] not in s_names_by_type.get("MLModel", set())]
        if missing:
            try:
                ml_result = _register_ml_models(target="ALL")
                results["MLModel"] = {
                    "status": "ok" if ml_result.get("summary", {}).get("success", 0) > 0 else "warning",
                    "message": f"Registered {ml_result.get('summary', {}).get('success', 0)} ML Models via definition API",
                    "replicated": ml_result.get("summary", {}).get("success", 0),
                    "detail": ml_result,
                }
            except Exception as e:
                results["MLModel"] = {"status": "error", "message": f"ML Model register failed: {e}"}
        else:
            results["MLModel"] = {"status": "ok", "message": "All MLModel items already mirrored", "replicated": 0}

    # Mark types that were already fully synced
    all_p_types = {i.get("type") for i in primary_items} - SKIP_TYPES
    for t in all_p_types:
        if t not in results:
            results[t] = {"status": "ok", "message": f"All {t} items already mirrored", "replicated": 0}

    return results


def _run_reverse_sync() -> Dict[str, Any]:
    """Reverse sync: current secondary (original primary) gets items from current primary (original secondary).

    After failover, primary_id points to original-secondary (now active),
    and secondary_id points to original-primary (recovering).
    We need to replicate from primary → secondary (i.e., original-secondary → original-primary).
    This is exactly what replicate_items_by_type already does.
    """
    return _run_final_sync()


@app.route('/api/failover/status', methods=['GET'])
def api_failover_status():
    """Get current DR state and workspace info."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    pair = _active_pair()
    if not pair:
        return jsonify({"error": "No workspace pair configured"}), 400

    p_id = pair.get("primary_id")
    s_id = pair.get("secondary_id")
    dr_state = pair.get("dr_state", "normal")

    p_check = _validate_workspace_readiness(p_id) if p_id else {"ok": False, "item_count": 0}
    s_check = _validate_workspace_readiness(s_id) if s_id else {"ok": False, "item_count": 0}

    sync_pct = 0
    if p_check["item_count"] > 0:
        sync_pct = round(min(s_check["item_count"], p_check["item_count"]) / p_check["item_count"] * 100)

    return jsonify({
        "dr_state": dr_state,
        "primary_name": pair.get("primary_name", ""),
        "secondary_name": pair.get("secondary_name", ""),
        "primary_ok": p_check["ok"],
        "secondary_ok": s_check["ok"],
        "primary_items": p_check["item_count"],
        "secondary_items": s_check["item_count"],
        "sync_pct": sync_pct,
        "events": _load_dr_events()[-10:],
    })


@app.route('/api/failover/simulate', methods=['POST'])
def api_failover_simulate():
    """Dry-run: validate secondary readiness without executing failover."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    steps = []
    # 1. Check primary accessible
    p_check = _validate_workspace_readiness(p_id)
    steps.append({"step": "Check primary accessible", "passed": p_check["ok"],
                  "detail": f"{p_check['item_count']} items" if p_check["ok"] else p_check.get("error", "")})

    # 2. Check secondary accessible
    s_check = _validate_workspace_readiness(s_id)
    steps.append({"step": "Check secondary accessible", "passed": s_check["ok"],
                  "detail": f"{s_check['item_count']} items" if s_check["ok"] else s_check.get("error", "")})

    # 3. Sync coverage
    sync_pct = round(min(s_check["item_count"], p_check["item_count"]) / max(p_check["item_count"], 1) * 100)
    steps.append({"step": "Artifact sync coverage", "passed": sync_pct >= 80,
                  "detail": f"{sync_pct}% mirrored"})

    all_passed = all(s["passed"] for s in steps)
    return jsonify({
        "status": "DRY_RUN_PASS" if all_passed else "DRY_RUN_FAIL",
        "validation_passed": all_passed,
        "steps": steps,
        "sync_pct": sync_pct,
    })


@app.route('/api/failover/execute', methods=['POST'])
def api_failover_execute():
    """Execute a managed failover: final sync, validate secondary, swap roles."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    dr_state = _get_dr_state()
    if dr_state != "normal":
        return jsonify({"error": f"Cannot failover — current state is '{dr_state}'. "
                        f"Must be 'normal' to initiate failover."}), 400

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    started = datetime.utcnow().isoformat()
    _set_dr_state("failing_over")
    log_steps = []

    try:
        # Step 1: Final sync primary → secondary
        log_steps.append({"step": "Final sync (primary → secondary)", "status": "running"})
        sync_result = _run_final_sync()
        log_steps[-1]["status"] = "done"
        log_steps[-1]["detail"] = {k: v.get("message", str(v)) for k, v in sync_result.items() if isinstance(v, dict)}

        # Step 2: Re-register ML Models (Fabric auto-drops these; recreate via definition API)
        log_steps.append({"step": "Re-register ML Models (definition-based)", "status": "running"})
        try:
            ml_result = _register_ml_models(target="ALL")
            ml_success = ml_result.get("summary", {}).get("success", 0)
            ml_total = ml_result.get("summary", {}).get("total", 0)
            if ml_total == 0:
                log_steps[-1]["status"] = "skipped"
                log_steps[-1]["detail"] = "No ML Models in primary"
            else:
                log_steps[-1]["status"] = "done"
                log_steps[-1]["detail"] = f"Registered {ml_success}/{ml_total} ML Models"
        except Exception as ml_err:
            log_steps[-1]["status"] = "warning"
            log_steps[-1]["detail"] = f"ML Model register failed (non-critical): {ml_err}"

        # Step 3: Validate secondary
        log_steps.append({"step": "Validate secondary readiness", "status": "running"})
        s_check = _validate_workspace_readiness(s_id)
        if not s_check["ok"]:
            raise RuntimeError(f"Secondary validation failed: {s_check.get('error')}")
        log_steps[-1]["status"] = "done"
        log_steps[-1]["detail"] = f"{s_check['item_count']} items ready"

        # Step 4: Swap roles
        log_steps.append({"step": "Swap roles (secondary → primary)", "status": "running"})
        _swap_pair_roles()
        log_steps[-1]["status"] = "done"

        # Step 5: Redeploy sync artifacts with new direction
        log_steps.append({"step": "Redeploy sync notebook (new direction)", "status": "running"})
        try:
            deploy_result = deploy_sync_artifacts()
            if "error" in deploy_result:
                log_steps[-1]["status"] = "warning"
                log_steps[-1]["detail"] = deploy_result["error"]
            else:
                log_steps[-1]["status"] = "done"
                log_steps[-1]["detail"] = "Sync artifacts redeployed for new direction"
        except Exception as de:
            log_steps[-1]["status"] = "warning"
            log_steps[-1]["detail"] = f"Redeploy failed (non-critical): {de}"

        # Step 6: Mark failed_over
        _set_dr_state("failed_over")
        finished = datetime.utcnow().isoformat()
        rto_seconds = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()

        event = {
            "type": "failover",
            "started": started,
            "finished": finished,
            "rto_seconds": round(rto_seconds, 1),
            "steps": log_steps,
            "success": True,
        }
        _save_dr_event(event)

        return jsonify({
            "status": "FAILOVER_COMPLETE",
            "message": f"Failover completed in {round(rto_seconds)}s. "
                       f"'{_ws_name('primary')}' is now the active primary.",
            "rto_seconds": round(rto_seconds, 1),
            "new_primary": _ws_name("primary"),
            "new_secondary": _ws_name("secondary"),
            "steps": log_steps,
        })

    except Exception as e:
        _set_dr_state("normal")  # Roll back state on failure
        logger.exception("Failover execution failed")
        event = {"type": "failover", "started": started, "finished": datetime.utcnow().isoformat(),
                 "steps": log_steps, "success": False, "error": str(e)}
        _save_dr_event(event)
        return jsonify({"error": f"Failover failed: {str(e)}", "steps": log_steps}), 500


@app.route('/api/failback/execute', methods=['POST'])
def api_failback_execute():
    """Execute failback: reverse sync, validate original primary, swap roles back."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    dr_state = _get_dr_state()
    if dr_state != "failed_over":
        return jsonify({"error": f"Cannot failback — current state is '{dr_state}'. "
                        f"Must be 'failed_over' to initiate failback."}), 400

    started = datetime.utcnow().isoformat()
    _set_dr_state("failing_back")
    log_steps = []

    try:
        # Step 1: Reverse sync — current primary (original secondary) → current secondary (original primary)
        log_steps.append({"step": "Reverse sync (active → original primary)", "status": "running"})
        sync_result = _run_reverse_sync()
        log_steps[-1]["status"] = "done"
        log_steps[-1]["detail"] = {k: v.get("message", str(v)) for k, v in sync_result.items() if isinstance(v, dict)}

        # Step 2: Validate original primary (currently stored as secondary)
        log_steps.append({"step": "Validate original primary readiness", "status": "running"})
        s_id = _ws_id("secondary")  # This is the original primary
        s_check = _validate_workspace_readiness(s_id)
        if not s_check["ok"]:
            raise RuntimeError(f"Original primary validation failed: {s_check.get('error')}")
        log_steps[-1]["status"] = "done"
        log_steps[-1]["detail"] = f"{s_check['item_count']} items ready"

        # Step 3: Swap roles back
        log_steps.append({"step": "Swap roles back (restore original primary)", "status": "running"})
        _swap_pair_roles()
        log_steps[-1]["status"] = "done"

        # Step 4: Redeploy sync artifacts with restored direction
        log_steps.append({"step": "Redeploy sync notebook (restored direction)", "status": "running"})
        try:
            deploy_result = deploy_sync_artifacts()
            if "error" in deploy_result:
                log_steps[-1]["status"] = "warning"
                log_steps[-1]["detail"] = deploy_result["error"]
            else:
                log_steps[-1]["status"] = "done"
                log_steps[-1]["detail"] = "Sync artifacts redeployed for restored direction"
        except Exception as de:
            log_steps[-1]["status"] = "warning"
            log_steps[-1]["detail"] = f"Redeploy failed (non-critical): {de}"

        # Step 5: Mark normal
        _set_dr_state("normal")
        finished = datetime.utcnow().isoformat()
        rto_seconds = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()

        event = {
            "type": "failback",
            "started": started,
            "finished": finished,
            "rto_seconds": round(rto_seconds, 1),
            "steps": log_steps,
            "success": True,
        }
        _save_dr_event(event)

        return jsonify({
            "status": "FAILBACK_COMPLETE",
            "message": f"Failback completed in {round(rto_seconds)}s. "
                       f"'{_ws_name('primary')}' is restored as the active primary.",
            "rto_seconds": round(rto_seconds, 1),
            "new_primary": _ws_name("primary"),
            "new_secondary": _ws_name("secondary"),
            "steps": log_steps,
        })

    except Exception as e:
        _set_dr_state("failed_over")  # Roll back to previous state
        logger.exception("Failback execution failed")
        event = {"type": "failback", "started": started, "finished": datetime.utcnow().isoformat(),
                 "steps": log_steps, "success": False, "error": str(e)}
        _save_dr_event(event)
        return jsonify({"error": f"Failback failed: {str(e)}", "steps": log_steps}), 500


@app.route('/api/failover/events', methods=['GET'])
def api_failover_events():
    """Get DR event history."""
    return jsonify({"events": _load_dr_events()})


@app.route('/api/bcdr/onelake-list', methods=['GET'])
def api_onelake_list():
    """Raw OneLake DFS recursive listing for a lakehouse subpath."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    ws_id = request.args.get("workspace_id")
    lh_id = request.args.get("lakehouse_id")
    subpath = request.args.get("subpath", "Tables")
    if not ws_id or not lh_id:
        return jsonify({"error": "workspace_id and lakehouse_id required"}), 400

    onelake_token = _get_onelake_token()
    if not onelake_token:
        return jsonify({"error": "Could not acquire OneLake token"}), 500

    dfs_url = f"https://onelake.dfs.fabric.microsoft.com/{ws_id}/{lh_id}/{subpath}"
    resp = requests.get(
        dfs_url, headers={"Authorization": f"Bearer {onelake_token}"}, timeout=60,
        params={"resource": "filesystem", "recursive": "true"},
    )
    if resp.status_code != 200:
        return jsonify({"error": f"DFS {resp.status_code}", "detail": resp.text[:500]}), resp.status_code

    return jsonify(resp.json())


@app.route('/api/bcdr/lakehouse-tables', methods=['GET'])
def api_lakehouse_tables():
    """Compare lakehouse table schemas and files between primary and secondary.
    Uses the Fabric Tables API and also shows the Files section."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id:
        return jsonify({"error": "Primary workspace not set"}), 400

    token = _ensure_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Get a separate token for OneLake DFS (storage scope)
    onelake_token = _get_onelake_token()
    onelake_headers = {"Authorization": f"Bearer {onelake_token}"} if onelake_token else headers

    def list_onelake_dirs(ws_id, lh_id, subpath="Tables"):
        """List directories under a lakehouse path via OneLake DFS API."""
        dfs_url = f"https://onelake.dfs.fabric.microsoft.com/{ws_id}/{lh_id}/{subpath}"
        try:
            resp = requests.get(
                dfs_url, headers=onelake_headers, timeout=30,
                params={"resource": "filesystem", "recursive": "false"},
            )
            if resp.status_code == 200:
                paths = resp.json().get("paths", [])
                return [p for p in paths if p.get("isDirectory") == "true"]
            else:
                logger.warning(f"OneLake DFS {resp.status_code} for {subpath}: {resp.text[:200]}")
        except Exception as ex:
            logger.debug(f"OneLake list {ws_id}/{lh_id}/{subpath}: {ex}")
        return []

    def discover_tables_via_onelake(ws_id, lh_id):
        """Discover Delta tables by listing Tables/ recursively via OneLake DFS."""
        tables = []
        dfs_url = f"https://onelake.dfs.fabric.microsoft.com/{ws_id}/{lh_id}/Tables"
        try:
            resp = requests.get(
                dfs_url, headers=onelake_headers, timeout=60,
                params={"resource": "filesystem", "recursive": "true"},
            )
            if resp.status_code == 404:
                # Check for capacity issues
                try:
                    err_body = resp.json()
                    err_code = err_body.get("error", {}).get("code", "")
                    if "Capacity" in err_code:
                        raise RuntimeError(f"Capacity error: {err_body.get('error', {}).get('message', err_code)}")
                except (ValueError, AttributeError):
                    pass
                return tables
            if resp.status_code != 200:
                logger.warning(f"OneLake recursive list {resp.status_code}: {resp.text[:200]}")
                # Check for capacity errors in non-200 responses
                try:
                    err_body = resp.json()
                    err_code = err_body.get("error", {}).get("code", "")
                    if "Capacity" in err_code:
                        raise RuntimeError(f"Capacity error: {err_body.get('error', {}).get('message', err_code)}")
                except (ValueError, AttributeError):
                    pass
                return tables
            all_paths = resp.json().get("paths", [])
            # Find all _delta_log directories — their parent is a table
            delta_logs = [p.get("name", "") for p in all_paths
                          if p.get("isDirectory") == "true" and p.get("name", "").endswith("/_delta_log")]
            for dl in delta_logs:
                # Remove /Tables prefix and /_delta_log suffix
                table_path = dl.rsplit("/_delta_log", 1)[0]
                # Remove the lakehouse Tables prefix (Tables/...)
                parts = table_path.split("/")
                # Path looks like: Tables/schema/table or Tables/table
                if len(parts) >= 3:
                    # Tables/schema/table/_delta_log
                    schema = parts[-2] if parts[-2] != "Tables" else ""
                    tname = parts[-1]
                elif len(parts) == 2:
                    schema = ""
                    tname = parts[-1]
                else:
                    continue
                display = f"{schema}.{tname}" if schema else tname
                tables.append(display)
        except Exception as ex:
            logger.debug(f"OneLake recursive discovery for {ws_id}/{lh_id}: {ex}")
        return tables

    def get_lh_details(ws_id):
        items = get_workspace_items(ws_id)
        lakehouses = [i for i in items if i.get("type") == "Lakehouse"]
        result = {}
        for lh in lakehouses:
            lh_id = lh["id"]
            lh_name = lh["displayName"]
            lh_data = {"id": lh_id, "tables": [], "files": []}
            try:
                # Use OneLake DFS to discover tables (works for both flat and schema-enabled)
                table_names = discover_tables_via_onelake(ws_id, lh_id)
                for tname in sorted(table_names):
                    if not tname.startswith("_bcdr_"):
                        lh_data["tables"].append({"name": tname, "type": "Table", "format": "delta"})
                if not lh_data["tables"]:
                    lh_data["schema_enabled"] = True
            except RuntimeError as cap_err:
                if "Capacity" in str(cap_err):
                    lh_data["capacity_error"] = str(cap_err)
                else:
                    lh_data["tables_error"] = str(cap_err)
            except Exception as e:
                lh_data["tables_error"] = str(e)
            result[lh_name] = lh_data
        return result

    primary_data = get_lh_details(p_id)
    secondary_data = get_lh_details(s_id) if s_id else {}

    return jsonify({
        "primary": primary_data,
        "secondary": secondary_data,
    })


# ============================================================================
# LAKEHOUSE AZCOPY REPLICATION
# ============================================================================

_azcopy_state: Dict[str, Any] = {
    "last_run": None,
    "last_status": None,
    "last_mode": None,
    "run_count": 0,
}
_AZCOPY_STATE_FILE = os.path.join(os.path.dirname(__file__), ".azcopy_state.json")


def _load_azcopy_state():
    try:
        if os.path.exists(_AZCOPY_STATE_FILE):
            with open(_AZCOPY_STATE_FILE, "r") as f:
                saved = json.load(f)
            _azcopy_state["last_run"] = saved.get("last_run")
            _azcopy_state["last_status"] = saved.get("last_status")
            _azcopy_state["last_mode"] = saved.get("last_mode")
            _azcopy_state["run_count"] = saved.get("run_count", 0)
    except Exception:
        pass


def _save_azcopy_state():
    try:
        with open(_AZCOPY_STATE_FILE, "w") as f:
            json.dump({
                "last_run": _azcopy_state["last_run"],
                "last_status": _azcopy_state["last_status"],
                "last_mode": _azcopy_state["last_mode"],
                "run_count": _azcopy_state["run_count"],
            }, f)
    except Exception:
        pass

# ── Azcopy Scheduled Incremental Sync ──
_azcopy_schedule_state: Dict[str, Any] = {
    "enabled": False,
    "interval_minutes": 15,
    "timer": None,
    "last_run": None,
    "last_status": None,
    "next_run": None,
    "run_count": 0,
    "include_ml": True,
}
_AZCOPY_SCHEDULE_FILE = os.path.join(os.path.dirname(__file__), ".azcopy_schedule.json")


def _load_azcopy_schedule():
    try:
        if os.path.exists(_AZCOPY_SCHEDULE_FILE):
            with open(_AZCOPY_SCHEDULE_FILE, "r") as f:
                saved = json.load(f)
            _azcopy_schedule_state["interval_minutes"] = saved.get("interval_minutes", 15)
            _azcopy_schedule_state["include_ml"] = saved.get("include_ml", True)
            if saved.get("enabled"):
                _start_azcopy_schedule(saved["interval_minutes"])
    except Exception:
        pass


def _save_azcopy_schedule():
    try:
        with open(_AZCOPY_SCHEDULE_FILE, "w") as f:
            json.dump({
                "enabled": _azcopy_schedule_state["enabled"],
                "interval_minutes": _azcopy_schedule_state["interval_minutes"],
                "include_ml": _azcopy_schedule_state["include_ml"],
            }, f)
    except Exception:
        pass


def _azcopy_schedule_tick():
    """Background timer — runs azcopy incremental sync for all lakehouses (and optionally ML items)."""
    if not _azcopy_schedule_state["enabled"]:
        return
    now = datetime.now()
    _azcopy_schedule_state["last_run"] = now.isoformat()
    _azcopy_schedule_state["run_count"] += 1
    run_num = _azcopy_schedule_state["run_count"]
    logger.info(f"Azcopy scheduled sync #{run_num} starting...")

    errors = []
    try:
        p_id = _ws_id("primary")
        s_id = _ws_id("secondary")
        if not p_id or not s_id:
            _azcopy_schedule_state["last_status"] = "Error: workspaces not configured"
            return

        if not _check_azcopy_available():
            _azcopy_schedule_state["last_status"] = "Error: azcopy not available"
            return

        # Lakehouse sync
        lh_mappings = _get_lakehouse_mappings()
        lh_count = 0
        for lh in (lh_mappings or []):
            try:
                result = _run_azcopy_for_lakehouse(
                    p_id, s_id,
                    lh["name"], lh["name"],
                    lh["primary_id"], lh["secondary_id"],
                    mode="sync", subpath="Tables,Files", dry_run=False,
                )
                lh_count += 1
                errors.extend(result.get("errors", []))
            except Exception as e:
                errors.append(f"Lakehouse {lh['name']}: {e}")

        # ML items sync (MLExperiment only — MLModel is definition-only)
        ml_count = 0
        if _azcopy_schedule_state.get("include_ml"):
            ml_mappings = _get_ml_mappings(include_missing=False)
            ml_mappings = [m for m in (ml_mappings or []) if m["type"] == "MLExperiment"]
            azcopy_bin = _get_azcopy_cmd()
            env = os.environ.copy()
            env["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
            for m in (ml_mappings or []):
                try:
                    src = f"https://onelake.dfs.fabric.microsoft.com/{p_id}/{m['primary_id']}/*"
                    dst = f"https://onelake.dfs.fabric.microsoft.com/{s_id}/{m['secondary_id']}"
                    proc = subprocess.run(
                        [azcopy_bin, "sync", src, dst, "--recursive",
                         "--delete-destination=false",
                         "--exclude-pattern=.platform",
                         "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com"],
                        capture_output=True, text=True, timeout=600, env=env,
                    )
                    ml_count += 1
                    if proc.returncode != 0:
                        errors.append(f"ML {m['name']}: {proc.stderr[:200]}")
                except Exception as e:
                    errors.append(f"ML {m['name']}: {e}")

        # Update azcopy_state too so lag calculation picks it up
        _azcopy_state["last_run"] = now.isoformat()
        _azcopy_state["last_mode"] = "sync"
        _azcopy_state["run_count"] += 1
        _save_azcopy_state()

        status_msg = f"Run #{run_num}: {lh_count} lakehouses"
        if _azcopy_schedule_state.get("include_ml"):
            status_msg += f", {ml_count} ML items"
        if errors:
            status_msg += f" ({len(errors)} errors)"
        _azcopy_schedule_state["last_status"] = status_msg
        logger.info(f"Azcopy scheduled sync #{run_num} done: {status_msg}")

    except Exception as e:
        _azcopy_schedule_state["last_status"] = f"Error: {e}"
        logger.exception("Azcopy scheduled sync error")

    # Schedule next run
    if _azcopy_schedule_state["enabled"]:
        interval = _azcopy_schedule_state["interval_minutes"] * 60
        _azcopy_schedule_state["next_run"] = (datetime.now() + timedelta(seconds=interval)).isoformat()
        t = threading.Timer(interval, _azcopy_schedule_tick)
        t.daemon = True
        t.start()
        _azcopy_schedule_state["timer"] = t


def _start_azcopy_schedule(interval_minutes: int):
    _stop_azcopy_schedule()
    _azcopy_schedule_state["enabled"] = True
    _azcopy_schedule_state["interval_minutes"] = interval_minutes
    interval = interval_minutes * 60
    _azcopy_schedule_state["next_run"] = (datetime.now() + timedelta(seconds=interval)).isoformat()
    t = threading.Timer(interval, _azcopy_schedule_tick)
    t.daemon = True
    t.start()
    _azcopy_schedule_state["timer"] = t
    _save_azcopy_schedule()
    logger.info(f"Azcopy schedule started: every {interval_minutes} minutes")


def _stop_azcopy_schedule():
    _azcopy_schedule_state["enabled"] = False
    _azcopy_schedule_state["next_run"] = None
    if _azcopy_schedule_state.get("timer"):
        _azcopy_schedule_state["timer"].cancel()
        _azcopy_schedule_state["timer"] = None
    _save_azcopy_schedule()


@app.route('/api/bcdr/azcopy-schedule', methods=['GET'])
def api_azcopy_schedule_get():
    """Get azcopy schedule state."""
    return jsonify({
        "enabled": _azcopy_schedule_state["enabled"],
        "interval_minutes": _azcopy_schedule_state["interval_minutes"],
        "include_ml": _azcopy_schedule_state["include_ml"],
        "last_run": _azcopy_schedule_state["last_run"],
        "last_status": _azcopy_schedule_state["last_status"],
        "next_run": _azcopy_schedule_state["next_run"],
        "run_count": _azcopy_schedule_state["run_count"],
    })


@app.route('/api/bcdr/azcopy-schedule', methods=['POST'])
def api_azcopy_schedule_set():
    """Start or stop the azcopy incremental sync schedule."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    enabled = data.get("enabled", False)
    interval = max(5, min(1440, int(data.get("interval_minutes", 15))))
    include_ml = data.get("include_ml", True)
    _azcopy_schedule_state["include_ml"] = include_ml
    if enabled:
        _start_azcopy_schedule(interval)
        return jsonify({"status": "ok", "message": f"Azcopy schedule started: every {interval} min"})
    else:
        _stop_azcopy_schedule()
        return jsonify({"status": "ok", "message": "Azcopy schedule stopped"})


def _get_azcopy_cmd() -> str:
    """Find the azcopy executable — check local project directory first, then PATH."""
    local_azcopy = os.path.join(os.path.dirname(__file__), "azcopy.exe")
    if os.path.exists(local_azcopy):
        return local_azcopy
    local_azcopy_nix = os.path.join(os.path.dirname(__file__), "azcopy")
    if os.path.exists(local_azcopy_nix):
        return local_azcopy_nix
    return "azcopy"


def _check_azcopy_available() -> bool:
    """Check if azcopy is available (local or PATH)."""
    try:
        cmd = _get_azcopy_cmd()
        result = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return False


def _run_azcopy_for_lakehouse(primary_ws_id: str, secondary_ws_id: str,
                               primary_lh_name: str, secondary_lh_name: str,
                               primary_lh_id: str, secondary_lh_id: str,
                               mode: str = "sync",
                               subpath: str = "Tables",
                               dry_run: bool = False) -> Dict[str, Any]:
    """Run azcopy copy (full) or sync (incremental) for a lakehouse.

    Args:
        mode: "copy" for full initial copy, "sync" for incremental
        subpath: "Tables", "Files", or "Tables,Files" for both
    """
    result = {"lakehouse": primary_lh_name, "mode": mode, "subpaths": [], "errors": []}

    subpaths = [s.strip() for s in subpath.split(",")]

    for sp in subpaths:
        # Use /* on source to copy CONTENTS of the folder, not the folder itself.
        # Without /*, azcopy nests the folder: dest/Tables/Tables/...
        source = (
            f"https://onelake.dfs.fabric.microsoft.com/{primary_ws_id}/{primary_lh_id}/{sp}/*"
        )
        dest = (
            f"https://onelake.dfs.fabric.microsoft.com/{secondary_ws_id}/{secondary_lh_id}/{sp}"
        )

        azcopy_bin = _get_azcopy_cmd()
        if mode == "copy":
            cmd = [
                azcopy_bin, "copy",
                source, dest,
                "--recursive",
                "--overwrite=ifSourceNewer",
                "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com",
            ]
        else:
            cmd = [
                azcopy_bin, "sync",
                source, dest,
                "--recursive",
                "--delete-destination=false",
                "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com",
            ]

        sp_result = {"subpath": sp, "source": source, "dest": dest, "status": "pending"}

        if dry_run:
            sp_result["status"] = "dry_run"
            sp_result["command"] = " ".join(cmd)
            result["subpaths"].append(sp_result)
            continue

        try:
            env = os.environ.copy()
            env["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, env=env)
            if proc.returncode == 0:
                sp_result["status"] = "success"
                # Parse summary from azcopy output
                stdout = proc.stdout or ""
                for line in stdout.split("\n"):
                    line = line.strip()
                    if any(kw in line.lower() for kw in ["total", "transfer", "elapsed", "final job"]):
                        sp_result.setdefault("summary_lines", []).append(line)
            else:
                sp_result["status"] = "failed"
                sp_result["stderr"] = (proc.stderr or "")[:500]
                sp_result["stdout"] = (proc.stdout or "")[:500]
                error_detail = (proc.stderr or proc.stdout or "unknown error")[:200]
                result["errors"].append(f"{sp}: {error_detail}")
        except subprocess.TimeoutExpired:
            sp_result["status"] = "timeout"
            result["errors"].append(f"{sp}: timed out after 1 hour")
        except FileNotFoundError:
            sp_result["status"] = "azcopy_not_found"
            result["errors"].append("azcopy not found — install from https://aka.ms/downloadazcopy")
        except Exception as e:
            sp_result["status"] = "error"
            sp_result["error"] = str(e)
            result["errors"].append(f"{sp}: {e}")

        result["subpaths"].append(sp_result)

    return result


@app.route('/api/bcdr/azcopy-status', methods=['GET'])
def api_azcopy_status():
    """Check if azcopy is available and return lakehouse pairs."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    available = _check_azcopy_available()
    lh_mappings = _get_lakehouse_mappings()

    return jsonify({
        "azcopy_available": available,
        "lakehouse_pairs": lh_mappings,
        "last_run": _azcopy_state["last_run"],
        "last_status": _azcopy_state["last_status"],
        "last_mode": _azcopy_state["last_mode"],
        "run_count": _azcopy_state["run_count"],
    })


@app.route('/api/bcdr/delete-secondary-tables', methods=['POST'])
def api_delete_secondary_tables():
    """Delete ALL table folders from secondary lakehouse(s) via OneLake DFS API.

    Body: { "lakehouse": "ALL" or "bronze_lakehouse", "dry_run": false }
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    target_lh = data.get("lakehouse", "ALL")
    dry_run = data.get("dry_run", False)

    s_id = _ws_id("secondary")
    if not s_id:
        return jsonify({"error": "Secondary workspace not configured"}), 400

    lh_mappings = _get_lakehouse_mappings()
    if not lh_mappings:
        return jsonify({"error": "No lakehouse mappings found"}), 404

    if target_lh != "ALL":
        lh_mappings = [m for m in lh_mappings if m["name"] == target_lh]
        if not lh_mappings:
            return jsonify({"error": f"Lakehouse '{target_lh}' not found"}), 404

    onelake_token = _get_onelake_token()
    if not onelake_token:
        return jsonify({"error": "Could not acquire OneLake token"}), 500
    onelake_headers = {"Authorization": f"Bearer {onelake_token}"}

    results = []
    for lh in lh_mappings:
        sec_id = lh["secondary_id"]
        lh_name = lh["name"]
        entry = {"lakehouse": lh_name, "tables_found": [], "deleted": [], "errors": []}

        # List top-level directories under Tables/
        dfs_url = f"https://onelake.dfs.fabric.microsoft.com/{s_id}/{sec_id}/Tables"
        try:
            resp = requests.get(
                dfs_url, headers=onelake_headers, timeout=60,
                params={"resource": "filesystem", "recursive": "false"},
            )
            if resp.status_code != 200:
                entry["errors"].append(f"List failed: {resp.status_code} - {resp.text[:200]}")
                results.append(entry)
                continue

            paths = resp.json().get("paths", [])
            dirs = [p.get("name", "") for p in paths if p.get("isDirectory") == "true"]
            entry["tables_found"] = dirs

            if dry_run:
                entry["status"] = "dry_run"
                results.append(entry)
                continue

            # Delete each top-level directory recursively
            for dir_path in dirs:
                del_url = f"https://onelake.dfs.fabric.microsoft.com/{s_id}/{dir_path}"
                try:
                    del_resp = requests.delete(
                        del_url, headers=onelake_headers, timeout=60,
                        params={"recursive": "true"},
                    )
                    if del_resp.status_code in (200, 202, 204):
                        entry["deleted"].append(dir_path)
                        logger.info(f"Deleted {dir_path} from {lh_name} secondary")
                    else:
                        entry["errors"].append(f"Delete {dir_path}: {del_resp.status_code} - {del_resp.text[:200]}")
                except Exception as ex:
                    entry["errors"].append(f"Delete {dir_path}: {ex}")

            entry["status"] = "success" if not entry["errors"] else "partial"

        except Exception as e:
            entry["errors"].append(str(e))
            entry["status"] = "error"

        results.append(entry)

    return jsonify({"dry_run": dry_run, "results": results})


@app.route('/api/bcdr/ml-status', methods=['GET'])
def api_ml_status():
    """Get ML Model and Experiment pairs with OneLake file counts."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    ml_mappings = _get_ml_mappings(include_missing=True)
    azcopy_ok = _check_azcopy_available()

    # Get OneLake file counts for each item
    onelake_token = _get_onelake_token()
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")

    enriched = []
    for m in ml_mappings:
        entry = dict(m)
        # MLModel is definition-only — no data to compare, skip file counting
        if m["type"] == "MLModel":
            entry["primary_files"] = 0
            entry["secondary_files"] = 0
            entry["definition_only"] = True
            enriched.append(entry)
            continue
        for label, ws, item_id in [("primary", p_id, m["primary_id"]), ("secondary", s_id, m["secondary_id"])]:
            count = 0
            if onelake_token and item_id:
                try:
                    dfs_url = f"https://onelake.dfs.fabric.microsoft.com/{ws}/{item_id}"
                    resp = requests.get(dfs_url,
                        headers={"Authorization": f"Bearer {onelake_token}"},
                        params={"resource": "filesystem", "recursive": "true"}, timeout=30)
                    if resp.status_code == 200:
                        paths = resp.json().get("paths", [])
                        count = len([p for p in paths if p.get("isDirectory") != "true"])
                except Exception:
                    pass
            entry[label + "_files"] = count
        enriched.append(entry)

    return jsonify({
        "azcopy_available": azcopy_ok,
        "ml_pairs": enriched,
    })


@app.route('/api/debug/definition/<workspace>/<item_id>', methods=['GET'])
def api_debug_definition(workspace, item_id):
    """Debug: export and decode an item's definition parts."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    ws_id = _ws_id(workspace) if workspace in ("primary", "secondary") else workspace
    try:
        resp = fabric_api("POST", f"/workspaces/{ws_id}/items/{item_id}/getDefinition", timeout=120)
        defn = resp.get("definition", resp) if resp else {}
        parts = defn.get("parts", [])
        decoded = []
        for p in parts:
            payload = p.get("payload", "")
            try:
                text = base64.b64decode(payload).decode("utf-8", errors="replace") if payload else ""
            except Exception:
                text = "(decode error)"
            decoded.append({"path": p.get("path"), "content": text})
        return jsonify({"parts_count": len(parts), "parts": decoded})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/bcdr/rename-item', methods=['POST'])
def api_rename_item():
    """Rename or delete an item in the secondary workspace.
    Body: { "item_id": "...", "new_name": "..." }
    Or:   { "item_id": "...", "action": "delete" }
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")
    action = data.get("action")
    new_name = data.get("new_name")
    if not item_id:
        return jsonify({"error": "item_id is required"}), 400
    s_id = _ws_id("secondary")
    if not s_id:
        return jsonify({"error": "Secondary workspace not configured"}), 400
    try:
        if action == "delete":
            fabric_api("DELETE", f"/workspaces/{s_id}/items/{item_id}", timeout=30)
            _cache.pop(f"items:{s_id}", None)
            _cache_ttl.pop(f"items:{s_id}", None)
            return jsonify({"status": "deleted", "item_id": item_id})
        elif new_name:
            fabric_api("PATCH", f"/workspaces/{s_id}/items/{item_id}",
                        payload={"displayName": new_name}, timeout=30)
            _cache.pop(f"items:{s_id}", None)
            _cache_ttl.pop(f"items:{s_id}", None)
            return jsonify({"status": "ok", "item_id": item_id, "new_name": new_name})
        else:
            return jsonify({"error": "new_name or action='delete' is required"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/bcdr/ml-replicate', methods=['POST'])
def api_ml_replicate():
    """Copy ML Model/Experiment artifacts from primary to secondary via azcopy.

    If an ML item exists in primary but not secondary, it is auto-created
    (definition export + import) before copying OneLake data.

    Body: { "mode": "copy", "item": "ALL" or "model_name", "dry_run": false }
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "copy")
    target = data.get("item", "ALL")
    dry_run = data.get("dry_run", False)

    if mode not in ("copy", "sync"):
        return jsonify({"error": "mode must be 'copy' or 'sync'"}), 400

    if not _check_azcopy_available() and not dry_run:
        return jsonify({"error": "azcopy is not available"}), 400

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    ml_mappings = _get_ml_mappings(include_missing=True)
    if not ml_mappings:
        return jsonify({"error": "No ML Model/Experiment items found in primary workspace"}), 404

    if target != "ALL":
        ml_mappings = [m for m in ml_mappings if m["name"] == target]
        if not ml_mappings:
            return jsonify({"error": f"ML item '{target}' not found in primary workspace"}), 404

    results = []
    for m in ml_mappings:
        entry = {"name": m["name"], "type": m["type"], "mode": mode, "status": "pending",
                 "created": False}

        # ── Auto-create if missing in secondary ──
        if m.get("missing") or not m.get("secondary_id"):
            if dry_run:
                entry["status"] = "dry_run"
                entry["note"] = "Would create in secondary then copy data"
                results.append(entry)
                continue

            logger.info(f"ML replicate: {m['name']} ({m['type']}) missing in secondary — creating")
            try:
                create_body = {"displayName": m["name"], "type": m["type"]}

                # MLModel: create empty (no definition).
                # Fabric validates MLModel definitions against the MLExperiment's
                # internal MLflow state and deletes mismatches within ~60s.
                if m["type"] != "MLModel":
                    defn_resp = fabric_api(
                        "POST",
                        f"/workspaces/{p_id}/items/{m['primary_id']}/getDefinition",
                        timeout=120,
                    )
                    defn = defn_resp.get("definition", {}) if defn_resp else {}

                    if defn.get("parts"):
                        try:
                            conn_map = _build_connection_map(p_id, s_id)
                            defn = dict(defn)
                            defn["parts"] = _rewrite_definition_parts(defn["parts"], conn_map)
                            logger.info(f"  Remapped {len(conn_map)} references in {m['name']} definition")
                        except Exception as remap_err:
                            logger.warning(f"  Could not remap definition for {m['name']}: {remap_err}")
                        create_body["definition"] = defn
                else:
                    logger.info(f"  MLModel: creating empty placeholder (definition skipped)")

                create_resp = None
                used_temp_name = False
                create_retries = 3 if m["type"] == "MLModel" else 1
                for attempt in range(create_retries):
                    try:
                        create_resp = fabric_api(
                            "POST", f"/workspaces/{s_id}/items",
                            payload=create_body, timeout=120,
                        )
                        break
                    except Exception as create_err:
                        err_msg = str(create_err)
                        # MLModel can't be renamed, so temp names won't work — just retry with delay
                        if ("getting deleted" in err_msg.lower() or "alreadyinuse" in err_msg.lower()) and attempt < create_retries - 1:
                            wait = 60 * (attempt + 1)
                            logger.info(f"  Name conflict (Fabric still purging), retrying in {wait}s (attempt {attempt+1}/{create_retries})")
                            import time; time.sleep(wait)
                        else:
                            raise

                if create_resp and create_resp.get("id"):
                    m["secondary_id"] = create_resp["id"]
                    m["missing"] = False
                    entry["created"] = True
                    logger.info(f"  Created {m['name']} in secondary: {m['secondary_id']}")
                else:
                    # Try to find it (may have been created via LRO)
                    s_items = fabric_api("GET", f"/workspaces/{s_id}/items?type={m['type']}")
                    for si in (s_items or {}).get("value", []):
                        if si["displayName"] == m["name"]:
                            m["secondary_id"] = si["id"]
                            m["missing"] = False
                            entry["created"] = True
                            break

                if not m.get("secondary_id"):
                    entry["status"] = "failed"
                    entry["error"] = "Could not create item in secondary"
                    results.append(entry)
                    continue

                # Update artifact_mapping.csv with the new secondary ID
                _update_artifact_csv(m["name"], m["type"], m["primary_id"], m["secondary_id"])

            except Exception as e:
                entry["status"] = "failed"
                entry["error"] = f"Create failed: {str(e)}"
                results.append(entry)
                continue

        # ── azcopy data ──
        # Skip azcopy for MLModel — copying data with primary model-version UUIDs
        # causes Fabric's MLflow service to detect inconsistency and delete the item.
        # MLModel is replicated by definition only; actual artifacts live in MLExperiment.
        if m["type"] == "MLModel":
            entry["status"] = "definition_only"
            entry["note"] = "MLModel replicated by definition only (data lives in MLExperiment)"
            results.append(entry)
            continue

        source = f"https://onelake.dfs.fabric.microsoft.com/{p_id}/{m['primary_id']}/*"
        dest = f"https://onelake.dfs.fabric.microsoft.com/{s_id}/{m['secondary_id']}"
        entry["source"] = source
        entry["dest"] = dest

        azcopy_bin = _get_azcopy_cmd()
        if mode == "copy":
            cmd = [azcopy_bin, "copy", source, dest, "--recursive",
                   "--overwrite=ifSourceNewer",
                   "--exclude-pattern=.platform",
                   "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com"]
        else:
            cmd = [azcopy_bin, "sync", source, dest, "--recursive",
                   "--delete-destination=false",
                   "--exclude-pattern=.platform",
                   "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com"]

        if dry_run:
            entry["status"] = "dry_run"
            entry["command"] = " ".join(cmd)
            results.append(entry)
            continue

        try:
            env = os.environ.copy()
            env["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=env)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            if proc.returncode == 0:
                entry["status"] = "success"
                summary_lines = []
                for line in stdout.split("\n"):
                    line = line.strip()
                    if any(kw in line.lower() for kw in ["total", "transfer", "elapsed", "final job"]):
                        summary_lines.append(line)
                entry["summary"] = "; ".join(summary_lines[-3:]) if summary_lines else "Completed"
            else:
                entry["status"] = "failed"
                entry["stderr"] = stderr[:500]
                entry["stdout"] = stdout[:500]
        except subprocess.TimeoutExpired:
            entry["status"] = "timeout"
        except FileNotFoundError:
            entry["status"] = "azcopy_not_found"
        except Exception as e:
            entry["status"] = "error"
            entry["error"] = str(e)

        results.append(entry)

    # Update azcopy state so topology lag picks it up
    if not dry_run and any(r.get("status") in ("success", "definition_only") for r in results):
        _azcopy_state["last_run"] = datetime.now().isoformat()
        _azcopy_state["last_mode"] = mode
        _azcopy_state["run_count"] += 1
        _azcopy_state["last_status"] = f"ML replicate: {len(results)} items"
        _save_azcopy_state()

    # Clear workspace items cache so next status check sees new items
    if not dry_run and any(r.get("created") for r in results):
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)

    return jsonify({"mode": mode, "dry_run": dry_run, "results": results})


def _fabric_mlflow(workspace_id: str, path: str, method: str = "GET",
                   payload: Dict = None, params: Dict = None) -> Dict:
    """Call Fabric MLflow REST API.

    Endpoint: https://api.fabric.microsoft.com/v1/workspaces/{ws}/mlflow/api/2.0/mlflow/{path}
    """
    token = _ensure_token()
    if not token:
        raise RuntimeError("Not authenticated")
    base = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/mlflow/api/2.0/mlflow"
    url = f"{base}/{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, params=params or {}, timeout=60)
    else:
        resp = requests.request(method.upper(), url, headers=headers, json=payload or {}, timeout=60)

    if resp.status_code >= 400:
        logger.warning(f"MLflow API {method} {path}: {resp.status_code} - {resp.text[:300]}")
        resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


def _sync_ml_model_versions(model_name: str, p_id: str, s_id: str, dry_run: bool = False) -> Dict[str, Any]:
    """Sync ML Model versions from primary to secondary via MLflow REST API.

    This is the critical step that makes MLModel items persist in Fabric.
    Without registered model versions backed by real experiment runs,
    Fabric's MLflow validation deletes the item after ~60s.

    Steps:
    1. Get registered model + versions from primary MLflow
    2. For each version, get the source run details
    3. Create a matching run in the secondary experiment
    4. Copy run artifacts via azcopy (experiment data already copied)
    5. Register model version in secondary pointing to new run
    """
    result = {"model": model_name, "versions_synced": 0, "errors": [], "details": []}

    # Build experiment mapping
    p_items = get_workspace_items(p_id)
    s_items = get_workspace_items(s_id)
    p_exp_map = {i["id"]: i["displayName"] for i in p_items if i.get("type") == "MLExperiment"}
    s_exp_by_name = {i["displayName"]: i["id"] for i in s_items if i.get("type") == "MLExperiment"}

    # Step 1: Get model versions from primary
    try:
        versions_resp = _fabric_mlflow(
            p_id, "model-versions/search",
            method="GET",
            params={"filter": f"name='{model_name}'"}
        )
        versions = versions_resp.get("model_versions", [])
    except Exception as e:
        result["errors"].append(f"Cannot get model versions: {e}")
        # Try alternate endpoint
        try:
            model_resp = _fabric_mlflow(p_id, "registered-models/get", params={"name": model_name})
            versions = model_resp.get("registered_model", {}).get("latest_versions", [])
        except Exception as e2:
            result["errors"].append(f"Alternate endpoint also failed: {e2}")
            return result

    if not versions:
        result["note"] = "No model versions found in primary — model may have no registered versions"
        return result

    logger.info(f"MLModel {model_name}: found {len(versions)} versions in primary")

    if dry_run:
        result["note"] = f"Would sync {len(versions)} model versions"
        for v in versions:
            result["details"].append({
                "version": v.get("version"),
                "run_id": v.get("run_id"),
                "status": v.get("status"),
                "source": v.get("source", "")[:100],
            })
        return result

    # Ensure registered model exists in secondary
    try:
        _fabric_mlflow(s_id, "registered-models/get", params={"name": model_name})
        logger.info(f"MLModel {model_name}: registered model already exists in secondary")
    except Exception:
        try:
            _fabric_mlflow(s_id, "registered-models/create", method="POST",
                           payload={"name": model_name})
            logger.info(f"MLModel {model_name}: created registered model in secondary")
        except Exception as e:
            result["errors"].append(f"Cannot create registered model in secondary: {e}")
            # Don't return — the Items API might have already created it

    # Step 2: Sync each version
    for version in versions:
        v_num = version.get("version", "?")
        run_id = version.get("run_id", "")
        source = version.get("source", "")
        v_detail = {"version": v_num, "status": "pending"}

        if not run_id:
            v_detail["status"] = "skipped"
            v_detail["note"] = "No run_id"
            result["details"].append(v_detail)
            continue

        # Get source run details from primary
        try:
            run_resp = _fabric_mlflow(p_id, "runs/get", params={"run_id": run_id})
            run = run_resp.get("run", {})
            run_info = run.get("info", {})
            run_data = run.get("data", {})
        except Exception as e:
            v_detail["status"] = "error"
            v_detail["note"] = f"Cannot get run {run_id}: {e}"
            result["details"].append(v_detail)
            continue

        # Map experiment to secondary
        p_exp_id = run_info.get("experiment_id", "")
        p_exp_name = p_exp_map.get(p_exp_id, "")
        s_exp_id = s_exp_by_name.get(p_exp_name, "")

        if not s_exp_id:
            v_detail["status"] = "error"
            v_detail["note"] = f"Cannot map experiment '{p_exp_name}' to secondary"
            result["details"].append(v_detail)
            continue

        # Create run in secondary experiment
        try:
            # Filter user tags, skip internal mlflow.* tags
            user_tags = [{"key": t["key"], "value": t["value"]}
                         for t in run_data.get("tags", [])
                         if not t["key"].startswith("mlflow.")]

            new_run_resp = _fabric_mlflow(s_id, "runs/create", method="POST", payload={
                "experiment_id": s_exp_id,
                "start_time": run_info.get("start_time"),
                "tags": user_tags,
            })
            new_run = new_run_resp.get("run", {})
            new_run_id = new_run.get("info", {}).get("run_id", "")
            if not new_run_id:
                raise RuntimeError("No run_id returned from runs/create")
            logger.info(f"MLModel {model_name} v{v_num}: created run {new_run_id[:8]}… in secondary experiment {p_exp_name}")
        except Exception as e:
            v_detail["status"] = "error"
            v_detail["note"] = f"Cannot create run in secondary: {e}"
            result["details"].append(v_detail)
            continue

        # Log params and metrics from original run
        try:
            params_list = run_data.get("params", [])
            metrics_list = run_data.get("metrics", [])
            batch = {}
            if params_list:
                batch["params"] = params_list
            if metrics_list:
                batch["metrics"] = [{"key": m["key"], "value": float(m["value"]),
                                     "timestamp": m.get("timestamp", 0), "step": m.get("step", 0)}
                                    for m in metrics_list]
            if batch:
                batch["run_id"] = new_run_id
                _fabric_mlflow(s_id, "runs/log-batch", method="POST", payload=batch)
        except Exception as e:
            logger.warning(f"MLModel {model_name} v{v_num}: log-batch failed (non-critical): {e}")

        # Copy run artifacts from primary experiment → secondary experiment via azcopy
        if _check_azcopy_available():
            try:
                src = f"https://onelake.dfs.fabric.microsoft.com/{p_id}/{p_exp_id}/{run_id}/*"
                dst = f"https://onelake.dfs.fabric.microsoft.com/{s_id}/{s_exp_id}/{new_run_id}"
                azcopy_bin = _get_azcopy_cmd()
                env = os.environ.copy()
                env["AZCOPY_AUTO_LOGIN_TYPE"] = "AZCLI"
                proc = subprocess.run(
                    [azcopy_bin, "copy", src, dst, "--recursive",
                     "--overwrite=ifSourceNewer",
                     "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com"],
                    capture_output=True, text=True, timeout=300, env=env,
                )
                if proc.returncode == 0:
                    logger.info(f"MLModel {model_name} v{v_num}: artifacts copied via azcopy")
                    v_detail["artifacts_copied"] = True
                else:
                    logger.warning(f"MLModel {model_name} v{v_num}: azcopy rc={proc.returncode} {proc.stderr[:200]}")
                    v_detail["artifacts_copied"] = False
            except Exception as e:
                logger.warning(f"MLModel {model_name} v{v_num}: artifact copy failed: {e}")
                v_detail["artifacts_copied"] = False

        # End the run
        try:
            _fabric_mlflow(s_id, "runs/update", method="POST", payload={
                "run_id": new_run_id,
                "status": "FINISHED",
                "end_time": run_info.get("end_time", int(time.time() * 1000)),
            })
        except Exception:
            pass

        # Register model version pointing to the new run
        try:
            # Build the source artifact URI for the new run
            new_source = source
            if source and run_id in source:
                new_source = source.replace(run_id, new_run_id)
                # Also remap experiment ID if present
                if p_exp_id in new_source:
                    new_source = new_source.replace(p_exp_id, s_exp_id)
                # Remap workspace ID if present
                if p_id in new_source:
                    new_source = new_source.replace(p_id, s_id)
            elif not source:
                new_source = f"runs:/{new_run_id}/model"

            _fabric_mlflow(s_id, "model-versions/create", method="POST", payload={
                "name": model_name,
                "source": new_source,
                "run_id": new_run_id,
            })
            v_detail["status"] = "success"
            v_detail["new_run_id"] = new_run_id
            result["versions_synced"] += 1
            logger.info(f"MLModel {model_name} v{v_num}: registered version in secondary")
        except Exception as e:
            v_detail["status"] = "error"
            v_detail["note"] = f"Cannot register version: {e}"

        result["details"].append(v_detail)

    return result


def _register_ml_model_via_notebook(model_name: str, p_id: str, s_id: str,
                                     experiment_name: str = None) -> Dict[str, Any]:
    """Register an ML Model in secondary by deploying and running a Fabric notebook.

    This is the only reliable way to create persistent MLModel items.
    External API registration (Items API, MLflow REST) creates items that
    Fabric's internal validation eventually deletes. Running mlflow.register_model()
    inside a Fabric notebook triggers proper internal hooks.

    Steps:
    1. Find the latest run in the secondary experiment
    2. Deploy a small notebook that calls mlflow.register_model()
    3. Execute the notebook via Fabric Jobs API
    4. Poll for completion
    """
    result_info = {"model": model_name, "status": "pending"}

    # Find experiment in secondary
    s_items = get_workspace_items(s_id)
    if not experiment_name:
        # Guess experiment from primary model's dependency
        p_items = get_workspace_items(p_id)
        p_model = next((i for i in p_items if i.get("type") == "MLModel"
                         and i.get("displayName") == model_name), None)
        if p_model:
            try:
                versions = _fabric_mlflow(p_id, "model-versions/search",
                                          method="GET", params={"filter": f"name='{model_name}'"})
                for v in versions.get("model_versions", []):
                    run_id = v.get("run_id", "")
                    if run_id:
                        run_resp = _fabric_mlflow(p_id, "runs/get", params={"run_id": run_id})
                        exp_id = run_resp.get("run", {}).get("info", {}).get("experiment_id", "")
                        p_exp_map = {i["id"]: i["displayName"] for i in p_items if i.get("type") == "MLExperiment"}
                        experiment_name = p_exp_map.get(exp_id, "")
                        break
            except Exception:
                pass

    if not experiment_name:
        # Fall back to first experiment
        s_exps = [i for i in s_items if i.get("type") == "MLExperiment"]
        if s_exps:
            experiment_name = s_exps[0]["displayName"]

    if not experiment_name:
        result_info["status"] = "error"
        result_info["error"] = "No MLExperiment found in secondary"
        return result_info

    s_exp = next((i for i in s_items if i.get("type") == "MLExperiment"
                  and i.get("displayName") == experiment_name), None)
    if not s_exp:
        result_info["status"] = "error"
        result_info["error"] = f"Experiment '{experiment_name}' not found in secondary"
        return result_info

    s_exp_id = s_exp["id"]
    result_info["experiment"] = experiment_name

    # Generate notebook content — creates a lightweight model and registers it.
    # A real model artifact is needed for Fabric to keep the MLModel item persistent.
    nb_lines = [
        "import mlflow",
        "import mlflow.pyfunc",
        "",
        f'EXPERIMENT_NAME = "{experiment_name}"',
        f'MODEL_NAME = "{model_name}"',
        "",
        "# Set experiment context",
        "mlflow.set_experiment(EXPERIMENT_NAME)",
        "",
        "# Define a lightweight placeholder model",
        "class BCDRPlaceholderModel(mlflow.pyfunc.PythonModel):",
        '    """Placeholder model for Resiliency & Recovery failover. Replace with actual model after failover."""',
        "    def predict(self, context, model_input):",
        "        import pandas as pd",
        "        return pd.DataFrame({'prediction': [0] * len(model_input)})",
        "",
        "# Log model and register",
        "with mlflow.start_run(run_name='BCDR_failover_registration') as run:",
        '    mlflow.log_param("bcdr_source", "failover_registration")',
        '    mlflow.log_param("original_workspace", "primary")',
        "    mlflow.pyfunc.log_model(",
        '        artifact_path="model",',
        "        python_model=BCDRPlaceholderModel(),",
        '        registered_model_name=MODEL_NAME,',
        "    )",
        '    print(f"Registered {MODEL_NAME} from run {run.info.run_id}")',
        '    print("Resiliency & Recovery model registration complete")',
    ]

    nb_ipynb = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {
            "kernel_info": {"name": "synapse_pyspark"},
            "language_info": {"name": "python"},
            "trident": {"lakehouse": {}},
        },
        "cells": [
            {
                "cell_type": "code",
                "source": [line + "\n" for line in nb_lines],
                "metadata": {},
                "outputs": [],
            }
        ],
    }

    nb_name = f"BCDR_MLModel_Register_{model_name}"
    nb_b64 = base64.b64encode(json.dumps(nb_ipynb).encode("utf-8")).decode("ascii")
    nb_definition = {
        "format": "ipynb",
        "parts": [{"path": "artifact.content.ipynb", "payload": nb_b64, "payloadType": "InlineBase64"}],
    }

    # Deploy notebook
    existing_nb = next((i for i in s_items if i.get("displayName") == nb_name and i.get("type") == "Notebook"), None)
    try:
        if existing_nb:
            fabric_api("POST", f"/workspaces/{s_id}/items/{existing_nb['id']}/updateDefinition",
                       payload={"definition": nb_definition}, timeout=120)
            nb_id = existing_nb["id"]
            logger.info(f"MLModel notebook: updated {nb_name}")
        else:
            resp = fabric_api("POST", f"/workspaces/{s_id}/items",
                              payload={"displayName": nb_name, "type": "Notebook", "definition": nb_definition},
                              timeout=120)
            nb_id = resp.get("id", "")
            logger.info(f"MLModel notebook: created {nb_name} ({nb_id})")
        result_info["notebook_id"] = nb_id
    except Exception as e:
        result_info["status"] = "error"
        result_info["error"] = f"Failed to deploy notebook: {e}"
        return result_info

    # Execute notebook
    try:
        token = _ensure_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{FABRIC_API_BASE}/workspaces/{s_id}/items/{nb_id}/jobs/instances?jobType=RunNotebook"
        resp = requests.post(url, headers=headers, timeout=30)

        if resp.status_code not in (200, 201, 202):
            result_info["status"] = "error"
            result_info["error"] = f"Notebook execution failed: {resp.status_code} {resp.text[:200]}"
            return result_info

        location = resp.headers.get("Location", "")
        result_info["job_location"] = location
        logger.info(f"MLModel notebook: triggered {nb_name}, polling {location}")

        # Poll for completion (up to 5 minutes)
        for attempt in range(30):
            time.sleep(10)
            if location:
                poll_resp = requests.get(location, headers=headers, timeout=30)
                if poll_resp.status_code == 200:
                    try:
                        op = poll_resp.json()
                        status = op.get("status", "")
                        if status == "Completed":
                            result_info["status"] = "success"
                            result_info["notebook_status"] = "completed"
                            logger.info(f"MLModel notebook: {nb_name} completed successfully")
                            break
                        elif status in ("Failed", "Cancelled"):
                            result_info["status"] = "error"
                            result_info["error"] = f"Notebook {status}: {op.get('error', {}).get('message', 'Unknown')}"
                            break
                    except Exception:
                        pass
        else:
            result_info["status"] = "timeout"
            result_info["note"] = "Notebook execution timed out after 5 minutes"

    except Exception as e:
        result_info["status"] = "error"
        result_info["error"] = f"Notebook execution failed: {e}"

    # Check if MLModel item appeared
    if result_info["status"] == "success":
        time.sleep(5)
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)
        refreshed = get_workspace_items(s_id)
        sec_model = next((i for i in refreshed if i.get("type") == "MLModel"
                          and i.get("displayName") == model_name), None)
        if sec_model:
            result_info["secondary_id"] = sec_model["id"]
            result_info["created"] = True
        else:
            result_info["note"] = "Notebook completed but MLModel item not yet visible"

    return result_info


def _register_ml_models(target: str = "ALL", dry_run: bool = False, verify: bool = False) -> Dict[str, Any]:
    """Register ML Models in secondary workspace.

    Strategy (ordered by reliability):
    1. Pre-stage: Sync experiment runs + artifacts via MLflow API + azcopy
    2. Register: Deploy and run a Fabric notebook that calls mlflow.register_model()
       (This is the only way to create persistent MLModel items — external API
       registration gets deleted by Fabric's internal validation.)
    3. Fallback: If notebook execution fails, create via Items API (ephemeral)
    """
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return {"error": "Both workspaces must be configured"}

    # Get ML items from both workspaces
    p_items = get_workspace_items(p_id)
    s_items = get_workspace_items(s_id)
    p_models = [i for i in p_items if i.get("type") == "MLModel"]
    s_by_name = {i["displayName"]: i for i in s_items if i.get("type") == "MLModel"}
    s_experiments = {i["displayName"]: i for i in s_items if i.get("type") == "MLExperiment"}

    if target != "ALL":
        p_models = [m for m in p_models if m["displayName"] == target]
        if not p_models:
            return {"error": f"MLModel '{target}' not found in primary workspace"}

    results = []
    for model in p_models:
        model_name = model["displayName"]
        model_id = model["id"]
        entry = {"name": model_name, "type": "MLModel", "status": "pending",
                 "created": False, "definition_applied": False}

        if not s_experiments:
            entry["status"] = "warning"
            entry["note"] = "No MLExperiments found in secondary — sync experiments first"
            results.append(entry)
            continue

        if dry_run:
            mlflow_result = _sync_ml_model_versions(model_name, p_id, s_id, dry_run=True)
            entry["status"] = "dry_run"
            entry["mlflow_sync"] = mlflow_result
            entry["note"] = mlflow_result.get("note", "Would sync via pre-stage + create approach")
            results.append(entry)
            continue

        # ---------------------------------------------------------------
        # PHASE 1: Pre-stage MLflow runs + artifacts + model versions
        # ---------------------------------------------------------------
        try:
            logger.info(f"MLModel {model_name}: Phase 1 — pre-staging MLflow versions...")
            mlflow_result = _sync_ml_model_versions(model_name, p_id, s_id, dry_run=False)
            entry["mlflow_sync"] = mlflow_result
            versions_synced = mlflow_result.get("versions_synced", 0)

            if versions_synced > 0:
                entry["versions_synced"] = versions_synced
                logger.info(f"MLModel {model_name}: pre-staged {versions_synced} versions")
            elif mlflow_result.get("errors"):
                logger.warning(f"MLModel {model_name}: pre-stage errors: {mlflow_result['errors'][:2]}")
        except Exception as e:
            logger.warning(f"MLModel {model_name}: pre-stage failed: {e}")
            mlflow_result = {"error": str(e)}
            entry["mlflow_sync"] = mlflow_result
            versions_synced = 0

        # ---------------------------------------------------------------
        # PHASE 2: Register via Fabric notebook (reliable persistence)
        # ---------------------------------------------------------------
        logger.info(f"MLModel {model_name}: Phase 2 — registering via Fabric notebook...")
        try:
            nb_result = _register_ml_model_via_notebook(model_name, p_id, s_id)
            entry["notebook_result"] = nb_result

            if nb_result.get("status") == "success":
                entry["status"] = "success"
                entry["action"] = "notebook_registered"
                if nb_result.get("secondary_id"):
                    entry["secondary_id"] = nb_result["secondary_id"]
                    entry["created"] = True
                    _update_artifact_csv(model_name, "MLModel", model_id, nb_result["secondary_id"])
                logger.info(f"MLModel {model_name}: notebook registration succeeded")
            else:
                logger.warning(f"MLModel {model_name}: notebook registration: {nb_result.get('status')} - {nb_result.get('error', nb_result.get('note', ''))}")
                raise RuntimeError(nb_result.get("error", "Notebook registration did not succeed"))

        except Exception as nb_err:
            # ---------------------------------------------------------------
            # PHASE 3 (Fallback): Create via Items API (ephemeral, ~3 min)
            # ---------------------------------------------------------------
            logger.info(f"MLModel {model_name}: Phase 3 fallback — creating via Items API...")
            if model_name in s_by_name:
                entry["secondary_id"] = s_by_name[model_name]["id"]
                entry["status"] = "success"
                entry["action"] = "already_exists"
            else:
                try:
                    export_resp = fabric_api("POST",
                                             f"/workspaces/{p_id}/items/{model_id}/getDefinition",
                                             timeout=120)
                    defn = (export_resp or {}).get("definition", {})
                    parts = defn.get("parts", [])
                    if parts:
                        try:
                            conn_map = _build_connection_map(p_id, s_id)
                        except Exception:
                            conn_map = {p_id: s_id}
                        remapped_parts = _rewrite_definition_parts(parts, conn_map)
                        remapped_defn = dict(defn)
                        remapped_defn["parts"] = remapped_parts
                        create_resp = fabric_api("POST", f"/workspaces/{s_id}/items",
                                                  payload={"displayName": model_name, "type": "MLModel",
                                                           "definition": remapped_defn}, timeout=120)
                    else:
                        create_resp = fabric_api("POST", f"/workspaces/{s_id}/items",
                                                  payload={"displayName": model_name, "type": "MLModel"}, timeout=60)

                    if create_resp and create_resp.get("id"):
                        entry["secondary_id"] = create_resp["id"]
                        entry["created"] = True
                        entry["action"] = "items_api_fallback"
                        entry["status"] = "success"
                        entry["note"] = "Created via Items API (ephemeral ~3min). For persistent model, run mlflow.register_model() from a Fabric notebook."
                        _update_artifact_csv(model_name, "MLModel", model_id, create_resp["id"])
                    else:
                        entry["status"] = "failed"
                        entry["error"] = f"Notebook: {nb_err}; Items API: no id returned"
                except Exception as api_err:
                    entry["status"] = "failed"
                    entry["error"] = f"Notebook: {nb_err}; Items API: {api_err}"

        # Verify persistence (optional)
        if verify and entry.get("secondary_id") and entry["status"] == "success":
            entry["verify"] = "waiting"
            logger.info(f"MLModel {model_name}: waiting 90s for Fabric validation...")
            time.sleep(90)
            try:
                check_resp = fabric_api("GET", f"/workspaces/{s_id}/items/{entry['secondary_id']}")
                if check_resp and check_resp.get("id"):
                    entry["verify"] = "survived"
                    logger.info(f"MLModel {model_name}: ✓ survived Fabric validation")
                else:
                    entry["verify"] = "deleted"
                    entry["status"] = "deleted_by_fabric"
                    entry["note"] = "Item deleted by Fabric despite pre-staged versions"
            except Exception:
                entry["verify"] = "deleted"
                entry["status"] = "deleted_by_fabric"
                entry["note"] = "Item deleted by Fabric despite pre-staged versions"

        results.append(entry)

    if not dry_run and any(r.get("created") for r in results):
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)

    return {
        "dry_run": dry_run,
        "verify": verify,
        "results": results,
        "summary": {
            "total": len(results),
            "success": sum(1 for r in results if r["status"] == "success"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "partial": sum(1 for r in results if r["status"] == "partial"),
            "deleted_by_fabric": sum(1 for r in results if r["status"] == "deleted_by_fabric"),
        }
    }


@app.route('/api/bcdr/ml-model-register', methods=['POST'])
def api_ml_model_register():
    """API wrapper for ML Model register."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    target = data.get("item", "ALL")
    dry_run = data.get("dry_run", False)
    verify = data.get("verify", False)
    # Also accept model_id — look up name from primary
    model_id = data.get("model_id")
    if model_id and target == "ALL":
        p_items = get_workspace_items(_ws_id("primary"))
        match = [i for i in p_items if i["id"] == model_id and i.get("type") == "MLModel"]
        if match:
            target = match[0]["displayName"]

    result = _register_ml_models(target=target, dry_run=dry_run, verify=verify)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/bcdr/azcopy-replicate', methods=['POST'])
def api_azcopy_replicate():
    """Run azcopy copy (full) or sync (incremental) for lakehouse(s).

    Body:
      { "mode": "copy", "lakehouse": "ALL", "subpath": "Tables,Files", "dry_run": false }
      { "mode": "sync", "lakehouse": "sales_data", "subpath": "Tables" }
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "sync")  # "copy" = full, "sync" = incremental
    target_lh = data.get("lakehouse", "ALL")
    subpath = data.get("subpath", "Tables,Files")
    dry_run = data.get("dry_run", False)

    if mode not in ("copy", "sync"):
        return jsonify({"error": "mode must be 'copy' (full) or 'sync' (incremental)"}), 400

    if not _check_azcopy_available() and not dry_run:
        return jsonify({"error": "azcopy is not available on this system. Install from https://aka.ms/downloadazcopy"}), 400

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    lh_mappings = _get_lakehouse_mappings()
    if not lh_mappings:
        return jsonify({"error": "No matching lakehouse pairs found. Sync lakehouse artifacts first."}), 404

    if target_lh != "ALL":
        lh_mappings = [m for m in lh_mappings if m["name"] == target_lh]
        if not lh_mappings:
            return jsonify({"error": f"Lakehouse '{target_lh}' not found in both workspaces"}), 404

    all_results = {"mode": mode, "dry_run": dry_run, "lakehouses": [], "errors": []}

    for lh in lh_mappings:
        lh_result = _run_azcopy_for_lakehouse(
            p_id, s_id,
            lh["name"], lh["name"],
            lh["primary_id"], lh["secondary_id"],
            mode=mode, subpath=subpath, dry_run=dry_run,
        )
        all_results["lakehouses"].append(lh_result)
        all_results["errors"].extend(lh_result.get("errors", []))

    _azcopy_state["last_run"] = datetime.now().isoformat()
    _azcopy_state["last_mode"] = mode
    _azcopy_state["run_count"] += 1
    _azcopy_state["last_status"] = (
        f"{'Dry run' if dry_run else 'Completed'}: {len(lh_mappings)} lakehouse(s), mode={mode}, "
        f"errors={len(all_results['errors'])}"
    )
    _save_azcopy_state()

    return jsonify(all_results)


@app.route('/api/bcdr/deploy-sync', methods=['POST'])
def api_deploy_sync():
    """Deploy the Resiliency & Recovery replication notebook + pipeline to secondary workspace."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        result = deploy_sync_artifacts()
        if "error" in result:
            return jsonify(result), 400
        return jsonify({"status": "ok", **result})
    except Exception as e:
        logger.exception("Error deploying sync artifacts")
        return jsonify({"error": str(e)}), 500


@app.route('/api/bcdr/run-sync', methods=['POST'])
def api_run_sync():
    """Trigger the replication notebook in secondary workspace."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        result = run_sync_notebook()
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        logger.exception("Error running sync")
        return jsonify({"error": str(e)}), 500


@app.route('/api/bcdr/sync-permission', methods=['POST'])
def api_sync_permission():
    """Add or update a workspace permission on the secondary to match primary.

    Body: { "principal_id": "...", "role": "Admin|Member|Contributor|Viewer", "principal_type": "User|Group|ServicePrincipal" }
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    s_id = _ws_id("secondary")
    if not s_id:
        return jsonify({"error": "Secondary workspace not configured"}), 400

    data = request.get_json() or {}
    principal_id = data.get("principal_id")
    role = data.get("role")
    principal_type = data.get("principal_type", "User")

    if not principal_id or not role:
        return jsonify({"error": "principal_id and role are required"}), 400

    # Check if this principal already has a role assignment in secondary
    try:
        existing = fabric_api("GET", f"/workspaces/{s_id}/roleAssignments")
        existing_roles = existing.get("value", []) if isinstance(existing, dict) else []
        existing_entry = None
        for r in existing_roles:
            pid = r.get("principal", {}).get("id", "")
            if pid == principal_id:
                existing_entry = r
                break

        if existing_entry:
            # Update existing role assignment
            ra_id = existing_entry.get("id", "")
            fabric_api("PATCH", f"/workspaces/{s_id}/roleAssignments/{ra_id}",
                        payload={"role": role}, timeout=30)
            logger.info(f"Updated permission for {principal_id} to {role} in secondary")
        else:
            # Add new role assignment
            fabric_api("POST", f"/workspaces/{s_id}/roleAssignments",
                        payload={
                            "principal": {"id": principal_id, "type": principal_type},
                            "role": role,
                        }, timeout=30)
            logger.info(f"Added permission for {principal_id} ({role}) in secondary")

        return jsonify({"status": "ok", "principal_id": principal_id, "role": role})
    except Exception as e:
        logger.error(f"Failed to sync permission: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# SECURITY — RLS, CLS, Item Permissions (OneLake Data Access Roles API)
# ============================================================================

@app.route('/api/security/proxy', methods=['GET'])
def api_security_proxy():
    """Proxy a GET call to Fabric REST API — debug endpoint."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    endpoint = request.args.get("endpoint", "")
    if not endpoint:
        return jsonify({"error": "endpoint param required"}), 400
    try:
        result = fabric_api("GET", endpoint)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500


@app.route('/api/security/test-connection', methods=['GET'])
def api_security_test_connection():
    """Test SQL connection to a lakehouse — debug endpoint."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    lh_name = request.args.get("lakehouse", "bronze_lakehouse")
    ws_label = request.args.get("workspace", "primary")
    sql = request.args.get("sql", "SELECT 1 AS test")
    ws_id = _ws_id(ws_label)
    if not ws_id:
        return jsonify({"error": f"Workspace '{ws_label}' not configured"}), 400
    # Clear cache for fresh resolution
    _sql_server_cache.clear()
    result = {"lakehouse": lh_name, "workspace": ws_label, "workspace_id": ws_id}
    try:
        server = _resolve_sql_server(ws_id, lh_name)
        result["resolved_server"] = server
    except Exception as e:
        result["resolve_error"] = str(e)
        return jsonify(result)
    try:
        rows = _run_sql(ws_id, lh_name, sql)
        result["sql_test"] = "OK"
        result["rows"] = rows[:50]
        result["row_count"] = len(rows)
    except Exception as e:
        result["connect_error"] = str(e)[:500]
    return jsonify(result)


def _parse_roles(roles_list):
    """Parse raw Data Access Roles into a simplified structure."""
    parsed = []
    for role in roles_list:
        role_info = {
            "id": role.get("id", ""),
            "name": role.get("name", ""),
            "kind": role.get("kind", ""),
            "etag": role.get("etag") or role.get("eTag", ""),
            "tables": [],
            "cls_columns": [],
            "members": role.get("members", {}),
        }
        for rule in role.get("decisionRules", []):
            for perm in rule.get("permission", []):
                if perm.get("attributeName") == "Path":
                    for path in perm.get("attributeValueIncludedIn", []):
                        if path != "*":
                            role_info["tables"].append(path)
            constraints = rule.get("constraints", {})
            for col_rule in constraints.get("columns", []):
                table_path = col_rule.get("tablePath", "")
                col_names = col_rule.get("columnNames", [])
                col_effect = col_rule.get("columnEffect", "")
                col_action = col_rule.get("columnAction", [])
                if col_names:
                    role_info["cls_columns"].append({
                        "tablePath": table_path,
                        "columns": col_names,
                        "effect": col_effect,
                        "action": col_action,
                    })
        parsed.append(role_info)
    return parsed


def _get_data_access_roles(ws_id, item_id):
    """GET data access roles for any Fabric item. Returns (roles_list, error_str_or_None)."""
    try:
        resp = fabric_api("GET", f"/workspaces/{ws_id}/items/{item_id}/dataAccessRoles")
        roles = resp.get("value", []) if isinstance(resp, dict) else []
        return roles, None
    except Exception as e:
        return [], str(e)[:300]


def _enable_onelake_security(ws_id, item_id):
    """Enable OneLake security on an item by PUT-ting a DefaultReader role.

    Returns (success_bool, message).
    """
    default_role = {
        "name": "DefaultReader",
        "decisionRules": [{
            "effect": "Permit",
            "permission": [
                {"attributeName": "Path", "attributeValueIncludedIn": ["*"]},
                {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
            ],
        }],
        "members": {
            "fabricItemMembers": [{
                "itemAccess": ["ReadAll"],
                "sourcePath": f"{ws_id}/{item_id}",
            }],
        },
    }
    try:
        fabric_api("PUT", f"/workspaces/{ws_id}/items/{item_id}/dataAccessRoles",
                   payload={"value": [default_role]})
        return True, "OneLake security enabled with DefaultReader role"
    except Exception as e:
        return False, str(e)[:300]


@app.route('/api/security/rls-status', methods=['GET'])
def api_rls_status():
    """Get current OneLake Data Access Roles for all lakehouses using Fabric REST API."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Workspaces not configured"}), 400

    p_items = get_workspace_items(p_id)
    s_items = get_workspace_items(s_id)

    p_lakehouses = [i for i in p_items if i.get("type") == "Lakehouse"]
    s_lakehouses = {i["displayName"]: i for i in s_items if i.get("type") == "Lakehouse"}

    lakehouses = []
    for lh in p_lakehouses:
        name = lh["displayName"]
        entry = {
            "name": name,
            "primary_id": lh["id"],
            "secondary_id": s_lakehouses.get(name, {}).get("id"),
            "primary_roles": [],
            "secondary_roles": [],
            "primary_security_enabled": True,
            "secondary_security_enabled": True,
        }

        # Always scan primary first
        p_lh_id = entry["primary_id"]
        p_roles_raw, p_err = _get_data_access_roles(p_id, p_lh_id)
        if p_err and "UniversalSecurityFeatureDisabled" in p_err:
            entry["primary_security_enabled"] = False
            entry["primary_roles"] = [{"security_disabled": True, "error": "OneLake security not enabled"}]
        elif p_err:
            entry["primary_roles"] = [{"error": p_err}]
        else:
            entry["primary_roles"] = _parse_roles(p_roles_raw)

        # Only scan secondary if primary has actual roles
        has_primary_roles = entry["primary_security_enabled"] and len(p_roles_raw) > 0
        s_lh_id = entry.get("secondary_id")
        if has_primary_roles and s_lh_id:
            s_roles_raw, s_err = _get_data_access_roles(s_id, s_lh_id)
            if s_err and "UniversalSecurityFeatureDisabled" in s_err:
                entry["secondary_security_enabled"] = False
                entry["secondary_roles"] = [{"security_disabled": True, "error": "OneLake security not enabled"}]
            elif s_err:
                entry["secondary_roles"] = [{"error": s_err}]
            else:
                entry["secondary_roles"] = _parse_roles(s_roles_raw)
        elif not has_primary_roles:
            entry["secondary_roles"] = []  # no need to check secondary

        lakehouses.append(entry)

    return jsonify({"lakehouses": lakehouses})


@app.route('/api/security/apply-rls', methods=['POST'])
def api_apply_rls():
    """Create/update a OneLake Data Access Role on a lakehouse.

    Body: {
        "workspace": "primary" or "secondary",
        "lakehouse": "gold_lakehouse",
        "role_name": "CrestRLS",
        "tables": ["/Tables/smartclaims/gold_claims_summary"],
        "cls_columns": [
            {
                "tablePath": "/Tables/smartclaims/gold_claims_summary",
                "columns": ["col1", "col2"],
                "columnEffect": "Permit",
                "columnAction": ["Read"]
            }
        ],
        "members": {
            "microsoftEntraMembers": [
                {"objectId": "...", "tenantId": "..."}
            ],
            "fabricItemMembers": []
        }
    }
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    ws_label = data.get("workspace", "primary")
    lh_name = data.get("lakehouse")
    role_name = data.get("role_name")
    tables = data.get("tables", [])
    cls_columns = data.get("cls_columns", [])
    members = data.get("members", {"microsoftEntraMembers": [], "fabricItemMembers": []})

    if not lh_name or not role_name:
        return jsonify({"error": "lakehouse and role_name are required"}), 400

    ws_id = _ws_id(ws_label)
    if not ws_id:
        return jsonify({"error": f"Workspace '{ws_label}' not configured"}), 400

    # Find lakehouse ID
    items = get_workspace_items(ws_id)
    lh_item = next((i for i in items if i.get("displayName") == lh_name and i.get("type") == "Lakehouse"), None)
    if not lh_item:
        return jsonify({"error": f"Lakehouse '{lh_name}' not found"}), 404
    lh_id = lh_item["id"]

    try:
        # Build the decision rule
        permission_attrs = [
            {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]},
        ]
        if tables:
            permission_attrs.append(
                {"attributeName": "Path", "attributeValueIncludedIn": tables}
            )
        else:
            permission_attrs.append(
                {"attributeName": "Path", "attributeValueIncludedIn": ["*"]}
            )

        decision_rule = {
            "effect": "Permit",
            "permission": permission_attrs,
        }

        # Add CLS column constraints if provided
        if cls_columns:
            decision_rule["constraints"] = {
                "columns": [
                    {
                        "tablePath": cc.get("tablePath", ""),
                        "columnNames": cc.get("columns", []),
                        "columnEffect": cc.get("columnEffect", "Permit"),
                        "columnAction": cc.get("columnAction", ["Read"]),
                    }
                    for cc in cls_columns
                ]
            }

        role_payload = {
            "name": role_name,
            "decisionRules": [decision_rule],
            "members": members,
        }

        # Check if security is enabled; auto-enable if not
        existing_roles, get_err = _get_data_access_roles(ws_id, lh_id)
        if get_err and "UniversalSecurityFeatureDisabled" in get_err:
            ok, msg = _enable_onelake_security(ws_id, lh_id)
            if not ok:
                return jsonify({"error": f"OneLake security not enabled: {msg}"}), 400
            existing_roles, _ = _get_data_access_roles(ws_id, lh_id)

        existing_role = next((r for r in existing_roles if r.get("name") == role_name), None)

        if existing_role:
            # Replace all roles via PUT (preserving other existing roles)
            other_roles = [r for r in existing_roles if r.get("name") != role_name]
            all_roles = other_roles + [role_payload]
            # Strip server-only fields from existing roles for PUT
            put_roles = []
            for r in all_roles:
                put_roles.append({
                    "name": r.get("name", ""),
                    "decisionRules": r.get("decisionRules", []),
                    "members": r.get("members", {}),
                })
            fabric_api("PUT",
                f"/workspaces/{ws_id}/items/{lh_id}/dataAccessRoles",
                payload={"value": put_roles})
            action = "updated"
        else:
            # Add new role alongside existing ones
            put_roles = []
            for r in existing_roles:
                put_roles.append({
                    "name": r.get("name", ""),
                    "decisionRules": r.get("decisionRules", []),
                    "members": r.get("members", {}),
                })
            put_roles.append(role_payload)
            fabric_api("PUT",
                f"/workspaces/{ws_id}/items/{lh_id}/dataAccessRoles",
                payload={"value": put_roles})
            action = "created"

        return jsonify({"status": "ok", "action": action, "role_name": role_name,
                        "workspace": ws_label, "lakehouse": lh_name,
                        "results": [{"step": f"role_{action}", "status": "ok", "name": role_name}]})
    except Exception as e:
        return jsonify({"error": str(e),
                        "results": [{"step": "error", "error": str(e)}]}), 500


@app.route('/api/security/apply-cls', methods=['POST'])
def api_apply_cls():
    """Update CLS column constraints on an existing Data Access Role.

    Body: {
        "workspace": "primary",
        "lakehouse": "gold_lakehouse",
        "role_name": "CrestRLS",
        "cls_columns": [
            {
                "tablePath": "/Tables/smartclaims/gold_claims_summary",
                "columns": ["col1", "col2"],
                "columnEffect": "Permit",
                "columnAction": ["Read"]
            }
        ]
    }
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    ws_label = data.get("workspace", "primary")
    lh_name = data.get("lakehouse")
    role_name = data.get("role_name")
    cls_columns = data.get("cls_columns", [])

    if not lh_name or not role_name or not cls_columns:
        return jsonify({"error": "lakehouse, role_name, and cls_columns are required"}), 400

    ws_id = _ws_id(ws_label)
    if not ws_id:
        return jsonify({"error": f"Workspace '{ws_label}' not configured"}), 400

    # Find lakehouse
    items = get_workspace_items(ws_id)
    lh_item = next((i for i in items if i.get("displayName") == lh_name and i.get("type") == "Lakehouse"), None)
    if not lh_item:
        return jsonify({"error": f"Lakehouse '{lh_name}' not found"}), 404
    lh_id = lh_item["id"]

    try:
        # Get existing roles
        existing_roles, get_err = _get_data_access_roles(ws_id, lh_id)
        if get_err:
            return jsonify({"error": f"Cannot read roles: {get_err}"}), 400
        role = next((r for r in existing_roles if r.get("name") == role_name), None)

        if not role:
            return jsonify({"error": f"Role '{role_name}' not found on {lh_name}"}), 404

        # Update the decision rules with new CLS constraints
        decision_rules = role.get("decisionRules", [])
        if decision_rules:
            decision_rules[0]["constraints"] = {
                "columns": [
                    {
                        "tablePath": cc.get("tablePath", ""),
                        "columnNames": cc.get("columns", []),
                        "columnEffect": cc.get("columnEffect", "Permit"),
                        "columnAction": cc.get("columnAction", ["Read"]),
                    }
                    for cc in cls_columns
                ]
            }

        # PUT all roles with updated CLS
        put_roles = []
        for r in existing_roles:
            if r.get("name") == role_name:
                put_roles.append({
                    "name": role_name,
                    "decisionRules": decision_rules,
                    "members": role.get("members", {}),
                })
            else:
                put_roles.append({
                    "name": r.get("name", ""),
                    "decisionRules": r.get("decisionRules", []),
                    "members": r.get("members", {}),
                })

        fabric_api("PUT",
            f"/workspaces/{ws_id}/items/{lh_id}/dataAccessRoles",
            payload={"value": put_roles})

        return jsonify({"status": "ok", "workspace": ws_label, "lakehouse": lh_name,
                        "role_name": role_name,
                        "results": [{"step": "update_cls", "status": "ok", "name": role_name}]})
    except Exception as e:
        return jsonify({"error": str(e),
                        "results": [{"step": "error", "error": str(e)}]}), 500


@app.route('/api/security/replicate', methods=['POST'])
def api_security_replicate():
    """Replicate OneLake Data Access Roles from primary to secondary."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Workspaces not configured"}), 400

    results = {"roles": []}

    p_items = get_workspace_items(p_id)
    s_items = get_workspace_items(s_id)
    p_lakehouses = [i for i in p_items if i.get("type") == "Lakehouse"]
    s_lh_by_name = {i["displayName"]: i for i in s_items if i.get("type") == "Lakehouse"}

    for lh in p_lakehouses:
        name = lh["displayName"]
        s_lh = s_lh_by_name.get(name)
        if not s_lh:
            continue
        p_lh_id = lh["id"]
        s_lh_id = s_lh["id"]

        try:
            # Get primary roles — skip lakehouses with no roles on primary
            p_roles, p_err = _get_data_access_roles(p_id, p_lh_id)
            if p_err or not p_roles:
                if p_err and "UniversalSecurityFeatureDisabled" in p_err:
                    reason = "OneLake security not enabled on primary (no roles to replicate)"
                elif p_err:
                    reason = p_err[:200]
                else:
                    reason = "No data access roles on primary"
                results["roles"].append({
                    "lakehouse": name, "status": "skipped",
                    "reason": reason,
                })
                continue

            # Check if secondary has security enabled
            s_roles, s_err = _get_data_access_roles(s_id, s_lh_id)
            if s_err and "UniversalSecurityFeatureDisabled" in s_err:
                # Auto-enable OneLake security on secondary lakehouse
                ok, msg = _enable_onelake_security(s_id, s_lh_id)
                if ok:
                    results["roles"].append({
                        "lakehouse": name, "role": "(auto-enable)",
                        "action": "enabled", "status": "ok",
                        "message": msg,
                    })
                    # Re-read secondary roles after enabling
                    s_roles, s_err = _get_data_access_roles(s_id, s_lh_id)
                else:
                    results["roles"].append({
                        "lakehouse": name, "status": "error",
                        "error": f"OneLake security not enabled on secondary. Enable it in Fabric portal first: open lakehouse '{name}' → Manage OneLake security (preview)",
                    })
                    continue

            s_roles_by_name = {r.get("name"): r for r in s_roles}

            # Build remapped roles for PUT (full replacement)
            remapped_roles = []
            for p_role in p_roles:
                role_name = p_role.get("name", "")
                members = p_role.get("members", {})
                remapped_members = {
                    "microsoftEntraMembers": members.get("microsoftEntraMembers", []),
                    "fabricItemMembers": [],
                }
                for fim in members.get("fabricItemMembers", []):
                    source_path = fim.get("sourcePath", "")
                    if source_path.startswith(p_id):
                        remapped_path = source_path.replace(p_id, s_id, 1)
                        remapped_path = remapped_path.replace(p_lh_id, s_lh_id, 1)
                    else:
                        remapped_path = source_path
                    remapped_members["fabricItemMembers"].append({
                        "itemAccess": fim.get("itemAccess", []),
                        "sourcePath": remapped_path,
                    })

                remapped_roles.append({
                    "name": role_name,
                    "decisionRules": p_role.get("decisionRules", []),
                    "members": remapped_members,
                })

            # PUT all roles at once (atomic create/update/delete to match primary)
            fabric_api("PUT",
                f"/workspaces/{s_id}/items/{s_lh_id}/dataAccessRoles",
                payload={"value": remapped_roles})
            for rr in remapped_roles:
                s_existing = s_roles_by_name.get(rr["name"])
                results["roles"].append({
                    "lakehouse": name, "role": rr["name"],
                    "action": "updated" if s_existing else "created",
                    "status": "ok",
                })
        except Exception as e:
            results["roles"].append({
                "lakehouse": name, "status": "error", "error": str(e)[:200],
            })

    total = len(results["roles"])
    errors = sum(1 for r in results["roles"] if r.get("status") == "error")
    return jsonify({"status": "ok", "total_actions": total, "errors": errors, "results": results})


@app.route('/api/security/item-permissions', methods=['GET'])
def api_item_permissions():
    """Get item-level permissions for all artifacts in both workspaces."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Workspaces not configured"}), 400

    p_items = get_workspace_items(p_id)
    s_items = get_workspace_items(s_id)
    s_by_name_type = {(i.get("type"), i.get("displayName")): i for i in s_items}

    items_perms = []
    for p_item in p_items:
        p_type = p_item.get("type")
        p_name = p_item.get("displayName")
        if p_type in ("SQLEndpoint",):
            continue  # Skip system items
        s_item = s_by_name_type.get((p_type, p_name))
        entry = {"name": p_name, "type": p_type, "primary_permissions": [], "secondary_permissions": [],
                 "in_sync": True}
        try:
            p_perms = fabric_api("GET", f"/workspaces/{p_id}/items/{p_item['id']}/permissions")
            entry["primary_permissions"] = p_perms.get("value", []) if isinstance(p_perms, dict) else []
        except Exception:
            pass
        if s_item:
            try:
                s_perms = fabric_api("GET", f"/workspaces/{s_id}/items/{s_item['id']}/permissions")
                entry["secondary_permissions"] = s_perms.get("value", []) if isinstance(s_perms, dict) else []
            except Exception:
                pass
        # Check sync status
        p_set = {(p.get("principal", {}).get("id"), p.get("role")) for p in entry["primary_permissions"]}
        s_set = {(p.get("principal", {}).get("id"), p.get("role")) for p in entry["secondary_permissions"]}
        entry["in_sync"] = p_set == s_set
        entry["missing_in_secondary"] = len(p_set - s_set)
        entry["extra_in_secondary"] = len(s_set - p_set)

        if entry["primary_permissions"] or entry["secondary_permissions"]:
            items_perms.append(entry)

    return jsonify({"items": items_perms})


@app.route('/security', methods=['GET'])
def security_page():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('security.html')


@app.route('/api/bcdr/cleanup-tables', methods=['POST'])
def api_cleanup_tables():
    """Remove tables from a secondary lakehouse that don't belong there.

    Compares tables in each secondary lakehouse against its primary counterpart
    and deletes tables in secondary that don't exist in primary (stale duplicates).
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    dry_run = request.json.get("dry_run", True) if request.json else True

    onelake_token = _get_onelake_token()
    if not onelake_token:
        return jsonify({"error": "Could not acquire OneLake token"}), 500
    onelake_headers = {"Authorization": f"Bearer {onelake_token}"}

    lh_mappings = _get_lakehouse_mappings()
    if not lh_mappings:
        return jsonify({"error": "No lakehouse mappings found"}), 400

    # Discover tables in each lakehouse pair
    def discover_tables(ws_id, lh_id):
        """Returns dict of {relative_table_path: full_path} for Delta tables."""
        tables = {}
        dfs_url = f"https://onelake.dfs.fabric.microsoft.com/{ws_id}/{lh_id}/Tables"
        try:
            resp = requests.get(
                dfs_url, headers=onelake_headers, timeout=60,
                params={"resource": "filesystem", "recursive": "true"},
            )
            if resp.status_code != 200:
                return tables
            all_paths = resp.json().get("paths", [])
            delta_logs = [p.get("name", "") for p in all_paths
                          if p.get("isDirectory") == "true" and p.get("name", "").endswith("/_delta_log")]
            for dl in delta_logs:
                full_table_path = dl.rsplit("/_delta_log", 1)[0]
                # Extract relative path after Tables/ for comparison
                if "/Tables/" in full_table_path:
                    rel_path = full_table_path.split("/Tables/", 1)[1]
                else:
                    rel_path = full_table_path
                tables[rel_path] = full_table_path
        except Exception as ex:
            logger.warning(f"discover_tables {ws_id}/{lh_id}: {ex}")
        return tables

    results = []
    for lh in lh_mappings:
        lh_name = lh["name"]
        p_tables = discover_tables(p_id, lh["primary_id"])
        s_tables = discover_tables(s_id, lh["secondary_id"])
        # Compare by relative path (after Tables/), not full path with lakehouse ID
        extra_rel_paths = set(s_tables.keys()) - set(p_tables.keys())
        if not extra_rel_paths:
            results.append({"lakehouse": lh_name, "extra_tables": 0, "deleted": []})
            continue

        deleted = []
        for rel_path in sorted(extra_rel_paths):
            full_path = s_tables[rel_path]
            if dry_run:
                deleted.append({"path": rel_path, "status": "would_delete"})
            else:
                # Delete the table folder via OneLake DFS
                # full_path already includes {lh_id}/Tables/..., so use it directly under workspace
                del_url = f"https://onelake.dfs.fabric.microsoft.com/{s_id}/{full_path}"
                try:
                    del_resp = requests.delete(
                        del_url, headers=onelake_headers, timeout=30,
                        params={"recursive": "true"},
                    )
                    if del_resp.status_code in (200, 202, 204):
                        deleted.append({"path": rel_path, "status": "deleted"})
                        logger.info(f"Cleanup: deleted {rel_path} from {lh_name} in secondary")
                    else:
                        deleted.append({"path": rel_path, "status": f"error_{del_resp.status_code}"})
                        logger.warning(f"Cleanup: failed to delete {rel_path}: {del_resp.status_code}")
                except Exception as ex:
                    deleted.append({"path": rel_path, "status": f"error: {ex}"})
        results.append({"lakehouse": lh_name, "extra_tables": len(extra_rel_paths), "deleted": deleted})

    return jsonify({"dry_run": dry_run, "results": results})


# ============================================================================
# ARTIFACT DELTA TRACKING — fast targeted change detection & sync
# ============================================================================
# Instead of scanning ALL definitions (slow, hits rate limits), this tracks
# per-item SHA-256 hashes and supports targeted checks on specific items.
# Flow: user edits Notebook X in primary → clicks "Check & Sync" for item X
#       → export definition from primary (1 API call) → compare hash →
#       if changed, export + remap + updateDefinition on secondary (2 API calls)

_HASH_FILE = os.path.join(os.path.dirname(__file__), ".artifact_hashes.json")
_HASHABLE_TYPES = {"Notebook", "DataPipeline", "SemanticModel", "Report",
                   "SparkJobDefinition", "Dataflow", "Eventstream"}


def _load_artifact_hashes() -> Dict[str, Any]:
    try:
        if os.path.exists(_HASH_FILE):
            with open(_HASH_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_artifact_hashes(hashes: Dict[str, Any]):
    try:
        with open(_HASH_FILE, "w") as f:
            json.dump(hashes, f, indent=2)
    except Exception:
        logger.warning("Could not save artifact hashes")


_artifact_hashes: Dict[str, Any] = _load_artifact_hashes()


def _hash_definition(definition: Dict) -> str:
    """Compute a stable SHA-256 hash of an artifact definition."""
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _export_and_hash(workspace_id: str, item_id: str) -> tuple:
    """Export item definition and return (definition_dict, hash_str).
    Returns (None, None) if not exportable."""
    try:
        resp = fabric_api(
            "POST", f"/workspaces/{workspace_id}/items/{item_id}/getDefinition",
            timeout=120,
        )
        if resp and isinstance(resp, dict):
            definition = resp.get("definition", {})
            if definition and definition.get("parts"):
                return definition, _hash_definition(definition)
    except Exception as e:
        logger.warning(f"Could not export definition for {item_id}: {e}")
    return None, None


def _delta_sync_item(p_id: str, s_id: str, item_name: str,
                     item_type: str, p_item_id: str, s_item_id: str) -> Dict[str, Any]:
    """Check a single item for definition changes and sync if needed.
    Fast: only 2-3 API calls total (export primary, optionally export secondary + update).
    """
    pair_key = f"{p_id}:{s_id}"
    now = datetime.now().isoformat()
    result = {"name": item_name, "type": item_type, "action": "unchanged", "error": None}

    if item_type not in _HASHABLE_TYPES:
        result["action"] = "not_supported"
        return result

    # 1. Export primary definition + hash  (1 API call)
    p_def, p_hash = _export_and_hash(p_id, p_item_id)
    if not p_hash:
        result["action"] = "export_failed"
        result["error"] = "Could not export primary definition"
        return result

    # 2. Check against stored hash — skip secondary export if unchanged
    saved = _artifact_hashes.get(pair_key, {}).get(item_name, {})
    if saved.get("primary_hash") == p_hash and not saved.get("changed"):
        result["action"] = "unchanged"
        result["primary_hash"] = p_hash[:12]
        return result

    # 3. Primary hash changed or first check — export secondary (1 API call)
    s_def, s_hash = _export_and_hash(s_id, s_item_id)

    if p_hash == s_hash:
        # Definitions match — update stored hashes
        if pair_key not in _artifact_hashes:
            _artifact_hashes[pair_key] = {}
        _artifact_hashes[pair_key][item_name] = {
            "primary_hash": p_hash, "secondary_hash": s_hash,
            "changed": False, "checked_at": now, "type": item_type,
        }
        _save_artifact_hashes(_artifact_hashes)
        result["action"] = "unchanged"
        result["primary_hash"] = p_hash[:12]
        return result

    # 4. Definitions differ — sync primary → secondary  (1 API call)
    try:
        definition = p_def
        parts = definition.get("parts", [])

        # Remap connections
        try:
            conn_map = _build_connection_map(p_id, s_id)
            if conn_map and parts:
                parts = _rewrite_definition_parts(parts, conn_map)
                definition = dict(definition)
                definition["parts"] = parts
        except Exception:
            pass

        try:
            fabric_api(
                "POST",
                f"/workspaces/{s_id}/items/{s_item_id}/updateDefinition",
                payload={"definition": definition},
                timeout=120,
            )
        except Exception as update_err:
            # updateDefinition can fail when the secondary item has stale bindings
            # (e.g. SemanticModel referencing a deleted lakehouse).
            # Fallback: delete + recreate the item with the correct definition.
            if item_type in ("SemanticModel", "Report"):
                logger.warning(f"updateDefinition failed for '{item_name}' ({item_type}), "
                               f"trying delete+recreate: {update_err}")
                fabric_api("DELETE", f"/workspaces/{s_id}/items/{s_item_id}", timeout=60)
                resp = fabric_api(
                    "POST", f"/workspaces/{s_id}/items",
                    payload={
                        "displayName": item_name,
                        "type": item_type,
                        "definition": definition,
                    },
                    timeout=120,
                )
                new_id = resp.get("id", s_item_id)
                logger.info(f"Recreated '{item_name}' ({item_type}) as {new_id}")
            else:
                raise

        if item_type == "Report":
            _rebind_report_to_secondary(p_id, s_id, item_name)

        # Update stored hash (now they match since we just synced)
        new_hash = _hash_definition(definition)
        if pair_key not in _artifact_hashes:
            _artifact_hashes[pair_key] = {}
        _artifact_hashes[pair_key][item_name] = {
            "primary_hash": p_hash, "secondary_hash": new_hash,
            "changed": False, "checked_at": now, "type": item_type,
        }
        _save_artifact_hashes(_artifact_hashes)

        result["action"] = "synced"
        result["primary_hash"] = p_hash[:12]
        result["previous_hash"] = (s_hash or "none")[:12]
        logger.info(f"Delta sync: updated '{item_name}' ({item_type})")

    except Exception as e:
        # Record as changed but failed to sync
        if pair_key not in _artifact_hashes:
            _artifact_hashes[pair_key] = {}
        _artifact_hashes[pair_key][item_name] = {
            "primary_hash": p_hash, "secondary_hash": s_hash,
            "changed": True, "checked_at": now, "type": item_type,
        }
        _save_artifact_hashes(_artifact_hashes)
        result["action"] = "sync_failed"
        result["error"] = str(e)
        logger.warning(f"Delta sync failed for '{item_name}': {e}")

    return result


@app.route('/api/bcdr/delta-sync', methods=['POST'])
def api_delta_sync():
    """Check and sync specific artifacts by name, or all hashable matched items.
    Fast: targets only requested items (2-3 API calls per item).

    Body: { "items": ["Notebook_A", "Pipeline_B"] }  — check+sync specific items
           or { }  — check+sync all hashable matched items
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    data = request.get_json() or {}
    requested_names = data.get("items")  # Optional list of item names

    # Build matched item lookup (uses cached item lists — fast)
    _cache.pop(f"items:{p_id}", None)
    _cache_ttl.pop(f"items:{p_id}", None)
    _cache.pop(f"items:{s_id}", None)
    _cache_ttl.pop(f"items:{s_id}", None)
    all_p = _filter_business_items(get_workspace_items(p_id))
    all_s = _filter_business_items(get_workspace_items(s_id))
    p_map = {i["displayName"]: i for i in all_p}
    s_map = {i["displayName"]: i for i in all_s}

    # Build targets
    targets = []
    for name, pi in p_map.items():
        si = s_map.get(name)
        if not si or pi.get("type") != si.get("type"):
            continue
        if pi.get("type") not in _HASHABLE_TYPES:
            continue
        if requested_names and name not in requested_names:
            continue
        targets.append({
            "name": name, "type": pi["type"],
            "primary_id": pi["id"], "secondary_id": si["id"],
        })

    if not targets:
        return jsonify({"status": "ok", "message": "No matching artifacts to check",
                        "results": [], "synced": 0, "unchanged": 0, "failed": 0})

    results = []
    synced = 0
    unchanged = 0
    failed = 0
    for t in targets:
        r = _delta_sync_item(p_id, s_id, t["name"], t["type"], t["primary_id"], t["secondary_id"])
        results.append(r)
        if r["action"] == "synced":
            synced += 1
        elif r["action"] in ("unchanged",):
            unchanged += 1
        elif r["action"] in ("sync_failed", "export_failed"):
            failed += 1

    # Clear secondary cache after sync
    if synced > 0:
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)

    return jsonify({
        "status": "ok",
        "total_checked": len(targets),
        "synced": synced,
        "unchanged": unchanged,
        "failed": failed,
        "results": results,
    })


@app.route('/api/bcdr/delta-check-item', methods=['POST'])
def api_delta_check_item():
    """Check a single artifact for definition changes — ultra-fast (2 API calls).
    Body: { "name": "My_Notebook" }
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    data = request.get_json() or {}
    item_name = data.get("name", "").strip()
    if not item_name:
        return jsonify({"error": "Item name is required"}), 400

    all_p = _filter_business_items(get_workspace_items(p_id))
    all_s = _filter_business_items(get_workspace_items(s_id))
    p_item = next((i for i in all_p if i["displayName"] == item_name), None)
    s_item = next((i for i in all_s if i["displayName"] == item_name), None)

    if not p_item:
        return jsonify({"error": f"Item '{item_name}' not found in primary"}), 404
    if not s_item:
        return jsonify({"error": f"Item '{item_name}' not found in secondary — needs full sync first"}), 404

    result = _delta_sync_item(
        p_id, s_id, item_name, p_item["type"], p_item["id"], s_item["id"]
    )
    return jsonify({"status": "ok", **result})


# ============================================================================
# AUTO DEFINITION CHECK — periodic polling for definition changes
# ============================================================================

_defcheck_state: Dict[str, Any] = {
    "enabled": False,
    "interval_minutes": 5,
    "timer": None,
    "last_check": None,
    "last_status": None,
    "synced_count": 0,
    "check_count": 0,
}
_DEFCHECK_FILE = os.path.join(os.path.dirname(__file__), ".defcheck_state.json")


def _load_defcheck():
    try:
        if os.path.exists(_DEFCHECK_FILE):
            with open(_DEFCHECK_FILE, "r") as f:
                saved = json.load(f)
            _defcheck_state["interval_minutes"] = saved.get("interval_minutes", 5)
            if saved.get("enabled"):
                _start_defcheck(_defcheck_state["interval_minutes"])
    except Exception:
        pass


def _save_defcheck():
    try:
        with open(_DEFCHECK_FILE, "w") as f:
            json.dump({
                "enabled": _defcheck_state["enabled"],
                "interval_minutes": _defcheck_state["interval_minutes"],
            }, f)
    except Exception:
        pass


def _defcheck_tick():
    """Background timer — check all hashable items for definition drift and auto-sync."""
    if not _defcheck_state["enabled"]:
        return
    _defcheck_state["check_count"] += 1
    _defcheck_state["last_check"] = datetime.now().isoformat()

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id or not is_authenticated():
        _defcheck_state["last_status"] = "Skipped — not ready"
        _schedule_next_defcheck()
        return

    try:
        _cache.pop(f"items:{p_id}", None)
        _cache_ttl.pop(f"items:{p_id}", None)
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)

        all_p = _filter_business_items(get_workspace_items(p_id))
        all_s = _filter_business_items(get_workspace_items(s_id))
        p_map = {i["displayName"]: i for i in all_p}
        s_map = {i["displayName"]: i for i in all_s}

        synced = 0
        failed = 0
        checked = 0
        for name, pi in p_map.items():
            si = s_map.get(name)
            if not si or pi.get("type") != si.get("type"):
                continue
            if pi.get("type") not in _HASHABLE_TYPES:
                continue
            checked += 1
            r = _delta_sync_item(p_id, s_id, name, pi["type"], pi["id"], si["id"])
            if r["action"] == "synced":
                synced += 1
            elif r["action"] in ("sync_failed", "export_failed"):
                failed += 1

        _defcheck_state["synced_count"] += synced
        if synced > 0 or failed > 0:
            _defcheck_state["last_status"] = (
                f"Check #{_defcheck_state['check_count']}: "
                f"{checked} checked, {synced} synced, {failed} failed"
            )
        else:
            _defcheck_state["last_status"] = (
                f"Check #{_defcheck_state['check_count']}: "
                f"{checked} checked, all definitions match"
            )
        logger.info(f"Def-check: {checked} checked, {synced} synced, {failed} failed")

    except Exception as e:
        _defcheck_state["last_status"] = f"Error: {e}"
        logger.exception("Def-check error")

    _schedule_next_defcheck()


def _schedule_next_defcheck():
    if _defcheck_state["enabled"]:
        interval = _defcheck_state["interval_minutes"] * 60
        t = threading.Timer(interval, _defcheck_tick)
        t.daemon = True
        t.start()
        _defcheck_state["timer"] = t


def _start_defcheck(interval_minutes: int = 5):
    _stop_defcheck()
    _defcheck_state["enabled"] = True
    _defcheck_state["interval_minutes"] = interval_minutes
    _save_defcheck()
    t = threading.Timer(interval_minutes * 60, _defcheck_tick)
    t.daemon = True
    t.start()
    _defcheck_state["timer"] = t
    logger.info(f"Auto definition check started: every {interval_minutes} min")


def _stop_defcheck():
    _defcheck_state["enabled"] = False
    if _defcheck_state["timer"]:
        _defcheck_state["timer"].cancel()
        _defcheck_state["timer"] = None
    _save_defcheck()


@app.route('/api/bcdr/defcheck', methods=['GET'])
def api_defcheck_get():
    return jsonify({
        "enabled": _defcheck_state["enabled"],
        "interval_minutes": _defcheck_state["interval_minutes"],
        "last_check": _defcheck_state["last_check"],
        "last_status": _defcheck_state["last_status"],
        "synced_count": _defcheck_state["synced_count"],
        "check_count": _defcheck_state["check_count"],
    })


@app.route('/api/bcdr/defcheck', methods=['POST'])
def api_defcheck_set():
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    enabled = data.get("enabled", False)
    interval = max(5, min(1440, int(data.get("interval_minutes", 5))))
    if enabled:
        _start_defcheck(interval)
        return jsonify({"status": "ok", "message": f"Auto definition check: every {interval} min"})
    else:
        _stop_defcheck()
        return jsonify({"status": "ok", "message": "Auto definition check disabled"})


# ============================================================================
# AUTO-SYNC WATCHER — automatically replicate new artifacts to secondary
# ============================================================================

_autosync_state: Dict[str, Any] = {
    "enabled": False,
    "interval_seconds": 60,
    "timer": None,
    "last_check": None,
    "last_status": None,
    "replicated_count": 0,
    "check_count": 0,
}
_AUTOSYNC_FILE = os.path.join(os.path.dirname(__file__), ".autosync_state.json")
_AUTOSYNC_NOT_SUPPORTED = {"Warehouse", "SQLEndpoint"}


def _load_autosync():
    try:
        if os.path.exists(_AUTOSYNC_FILE):
            with open(_AUTOSYNC_FILE, "r") as f:
                saved = json.load(f)
            _autosync_state["interval_seconds"] = saved.get("interval_seconds", 60)
            _autosync_state["last_check"] = saved.get("last_check")
            _autosync_state["last_status"] = saved.get("last_status")
            if saved.get("enabled"):
                _start_autosync(_autosync_state["interval_seconds"])
    except Exception:
        pass


def _save_autosync():
    try:
        with open(_AUTOSYNC_FILE, "w") as f:
            json.dump({
                "enabled": _autosync_state["enabled"],
                "interval_seconds": _autosync_state["interval_seconds"],
                "last_check": _autosync_state.get("last_check"),
                "last_status": _autosync_state.get("last_status"),
            }, f)
    except Exception:
        pass


def _autosync_tick():
    """Background timer — check for new primary items missing in secondary and replicate."""
    if not _autosync_state["enabled"]:
        return
    _autosync_state["check_count"] += 1
    _autosync_state["last_check"] = datetime.now().isoformat()

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id or not is_authenticated():
        _autosync_state["last_status"] = "Skipped — workspaces not ready"
        _schedule_next_autosync()
        return

    try:
        # Force fresh data
        _cache.pop(f"items:{p_id}", None)
        _cache_ttl.pop(f"items:{p_id}", None)
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)

        p_items = get_workspace_items(p_id)
        s_items = get_workspace_items(s_id)
        s_names_by_type = {}
        for si in s_items:
            key = (si.get("displayName"), si.get("type"))
            s_names_by_type[key] = True

        new_items = []
        for pi in p_items:
            ptype = pi.get("type", "")
            pname = pi.get("displayName", "")
            if ptype in _AUTOSYNC_NOT_SUPPORTED:
                continue
            if (pname, ptype) not in s_names_by_type:
                new_items.append(pi)

        if not new_items:
            # No new items — check a batch of existing items for definition changes
            try:
                p_filtered = _filter_business_items(p_items)
                s_filtered = _filter_business_items(s_items)
                p_map_a = {i["displayName"]: i for i in p_filtered}
                s_map_a = {i["displayName"]: i for i in s_filtered}
                delta_synced = 0
                delta_failed = 0
                for name, pi in p_map_a.items():
                    si = s_map_a.get(name)
                    if not si or pi.get("type") != si.get("type"):
                        continue
                    if pi.get("type") not in _HASHABLE_TYPES:
                        continue
                    r = _delta_sync_item(p_id, s_id, name, pi["type"], pi["id"], si["id"])
                    if r["action"] == "synced":
                        delta_synced += 1
                    elif r["action"] in ("sync_failed", "export_failed"):
                        delta_failed += 1
                if delta_synced > 0 or delta_failed > 0:
                    _autosync_state["replicated_count"] += delta_synced
                    _autosync_state["last_status"] = (
                        f"Check #{_autosync_state['check_count']}: "
                        f"{delta_synced} definitions updated, {delta_failed} failed"
                    )
                    logger.info(f"Auto-sync delta: {delta_synced} updated, {delta_failed} failed")
                    _schedule_next_autosync()
                    return
            except Exception as de:
                logger.warning(f"Auto-sync delta check failed: {de}")

            _autosync_state["last_status"] = f"Check #{_autosync_state['check_count']}: all synced"
            logger.debug("Auto-sync: no new items to replicate")
            _schedule_next_autosync()
            return

        # Ensure folder structure is mirrored only for folders referenced by new items
        auto_folder_map = {}
        try:
            needed_fids = {i.get("folderId") for i in new_items if i.get("folderId")}
            if needed_fids:
                auto_folder_map = _ensure_folder_structure(p_id, s_id, needed_folder_ids=needed_fids)
        except Exception:
            pass

        replicated = 0
        errors = 0
        for item in new_items:
            item_id = item.get("id")
            item_name = item.get("displayName", "")
            item_type = item.get("type", "")
            try:
                # Export definition
                definition = {}
                parts = []
                try:
                    export_resp = fabric_api(
                        "POST", f"/workspaces/{p_id}/items/{item_id}/getDefinition",
                        timeout=120,
                    )
                    if export_resp and isinstance(export_resp, dict):
                        definition = export_resp.get("definition", {})
                        parts = definition.get("parts", []) if definition else []
                except Exception:
                    pass

                # Rewrite connections for all artifact types
                if parts:
                    try:
                        conn_map = _build_connection_map(p_id, s_id)
                        if conn_map:
                            parts = _rewrite_definition_parts(parts, conn_map)
                            definition = dict(definition)
                            definition["parts"] = parts
                    except Exception:
                        pass

                create_payload = {"displayName": item_name, "type": item_type}
                s_folder = _get_folder_id_for_item(item, auto_folder_map)
                if s_folder:
                    create_payload["folderId"] = s_folder
                if parts:
                    create_payload["definition"] = definition
                elif item_type in ("SemanticModel", "Report"):
                    logger.warning(f"Auto-sync: skipping {item_name} — no definition available")
                    continue

                fabric_api("POST", f"/workspaces/{s_id}/items", payload=create_payload, timeout=120)
                replicated += 1
                logger.info(f"Auto-sync: replicated '{item_name}' ({item_type})")

                # For Report, rebind to the secondary SemanticModel
                if item_type == "Report":
                    _cache.pop(f"items:{s_id}", None)
                    _cache_ttl.pop(f"items:{s_id}", None)
                    _rebind_report_to_secondary(p_id, s_id, item_name)
            except Exception as e:
                errors += 1
                logger.warning(f"Auto-sync: failed to replicate '{item_name}': {e}")

        _autosync_state["replicated_count"] += replicated
        _autosync_state["last_status"] = (
            f"Check #{_autosync_state['check_count']}: "
            f"{replicated} replicated, {errors} errors, {len(new_items)} detected"
        )
        logger.info(f"Auto-sync: {replicated}/{len(new_items)} replicated")

        # Trigger DevOps pipeline if new items were detected
        if len(new_items) > 0:
            trigger_devops_on_new_artifacts(len(new_items))

        # Clear secondary cache after replication
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)

    except Exception as e:
        _autosync_state["last_status"] = f"Error: {e}"
        logger.exception("Auto-sync error")

    _save_autosync()
    _schedule_next_autosync()


def _schedule_next_autosync():
    if _autosync_state["enabled"]:
        interval = _autosync_state["interval_seconds"]
        t = threading.Timer(interval, _autosync_tick)
        t.daemon = True
        t.start()
        _autosync_state["timer"] = t


def _start_autosync(interval_seconds: int = 60):
    _stop_autosync()
    _autosync_state["enabled"] = True
    _autosync_state["interval_seconds"] = interval_seconds
    _save_autosync()
    t = threading.Timer(interval_seconds, _autosync_tick)
    t.daemon = True
    t.start()
    _autosync_state["timer"] = t
    logger.info(f"Auto-sync watcher started: every {interval_seconds}s")


def _stop_autosync():
    _autosync_state["enabled"] = False
    if _autosync_state["timer"]:
        _autosync_state["timer"].cancel()
        _autosync_state["timer"] = None
    _save_autosync()
    logger.info("Auto-sync watcher stopped")


@app.route('/api/bcdr/autosync', methods=['GET'])
def api_autosync_get():
    return jsonify({
        "enabled": _autosync_state["enabled"],
        "interval_seconds": _autosync_state["interval_seconds"],
        "last_check": _autosync_state["last_check"],
        "last_status": _autosync_state["last_status"],
        "replicated_count": _autosync_state["replicated_count"],
        "check_count": _autosync_state["check_count"],
    })


@app.route('/api/bcdr/autosync', methods=['POST'])
def api_autosync_set():
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    enabled = data.get("enabled", False)
    interval = max(30, min(600, int(data.get("interval_seconds", 60))))
    if enabled:
        _start_autosync(interval)
        return jsonify({"status": "ok", "message": f"Auto-sync enabled: every {interval}s"})
    else:
        _stop_autosync()
        return jsonify({"status": "ok", "message": "Auto-sync disabled"})


# ============================================================================
# SCHEDULED SYNC
# ============================================================================

_schedule_state: Dict[str, Any] = {
    "enabled": False,
    "interval_minutes": 15,
    "timer": None,
    "last_run": None,
    "last_status": None,
    "next_run": None,
    "run_count": 0,
}
_SCHEDULE_FILE = os.path.join(os.path.dirname(__file__), ".sync_schedule.json")


def _load_schedule():
    try:
        if os.path.exists(_SCHEDULE_FILE):
            with open(_SCHEDULE_FILE, "r") as f:
                saved = json.load(f)
            _schedule_state["interval_minutes"] = saved.get("interval_minutes", 15)
            _schedule_state["last_run"] = saved.get("last_run")
            _schedule_state["last_status"] = saved.get("last_status")
            if saved.get("enabled"):
                _start_schedule(saved["interval_minutes"])
    except Exception:
        pass

def _save_schedule():
    try:
        with open(_SCHEDULE_FILE, "w") as f:
            json.dump({
                "enabled": _schedule_state["enabled"],
                "interval_minutes": _schedule_state["interval_minutes"],
                "last_run": _schedule_state.get("last_run"),
                "last_status": _schedule_state.get("last_status"),
            }, f)
    except Exception:
        pass


def _scheduled_sync_tick():
    """Background timer callback — triggers the sync notebook."""
    if not _schedule_state["enabled"]:
        return
    now = datetime.now()
    _schedule_state["last_run"] = now.isoformat()
    _schedule_state["run_count"] += 1
    logger.info(f"Scheduled sync #{_schedule_state['run_count']} starting...")
    try:
        result = run_sync_notebook()
        if "error" in result:
            _schedule_state["last_status"] = f"Error: {result['error']}"
            logger.warning(f"Scheduled sync failed: {result['error']}")
        else:
            _schedule_state["last_status"] = "Triggered OK"
            logger.info(f"Scheduled sync triggered successfully")
    except Exception as e:
        _schedule_state["last_status"] = f"Exception: {e}"
        logger.exception("Scheduled sync exception")
    _save_schedule()
    # Schedule next run
    if _schedule_state["enabled"]:
        interval = _schedule_state["interval_minutes"] * 60
        _schedule_state["next_run"] = (datetime.now() + timedelta(seconds=interval)).isoformat()
        t = threading.Timer(interval, _scheduled_sync_tick)
        t.daemon = True
        t.start()
        _schedule_state["timer"] = t


def _start_schedule(interval_minutes: int):
    """Start the scheduled sync with the given interval."""
    _stop_schedule()
    _schedule_state["enabled"] = True
    _schedule_state["interval_minutes"] = interval_minutes
    interval = interval_minutes * 60
    _schedule_state["next_run"] = (datetime.now() + timedelta(seconds=interval)).isoformat()
    t = threading.Timer(interval, _scheduled_sync_tick)
    t.daemon = True
    t.start()
    _schedule_state["timer"] = t
    _save_schedule()
    logger.info(f"Sync schedule started: every {interval_minutes} minutes")


def _stop_schedule():
    """Stop the scheduled sync."""
    _schedule_state["enabled"] = False
    _schedule_state["next_run"] = None
    if _schedule_state["timer"]:
        _schedule_state["timer"].cancel()
        _schedule_state["timer"] = None
    _save_schedule()
    logger.info("Sync schedule stopped")


@app.route('/api/bcdr/schedule', methods=['GET'])
def api_schedule_get():
    """Get the current sync schedule state."""
    return jsonify({
        "enabled": _schedule_state["enabled"],
        "interval_minutes": _schedule_state["interval_minutes"],
        "last_run": _schedule_state["last_run"],
        "last_status": _schedule_state["last_status"],
        "next_run": _schedule_state["next_run"],
        "run_count": _schedule_state["run_count"],
    })


@app.route('/api/bcdr/schedule', methods=['POST'])
def api_schedule_set():
    """Start or stop the sync schedule."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    enabled = data.get("enabled", False)
    interval = data.get("interval_minutes", 15)
    # Clamp interval to [5, 1440]
    interval = max(5, min(1440, int(interval)))
    if enabled:
        _start_schedule(interval)
        return jsonify({"status": "ok", "message": f"Schedule started: every {interval} minutes"})
    else:
        _stop_schedule()
        return jsonify({"status": "ok", "message": "Schedule stopped"})


# ============================================================================
# WEB ROUTES (all require auth + workspace selection)
# ============================================================================

@app.route('/', methods=['GET'])
def dashboard():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('dashboard.html', user_name=_auth_state.get("user_name", ""))


@app.route('/drift', methods=['GET'])
def drift_detection():
    guard = _require_setup()
    if guard:
        return guard
    # Render page immediately; JS will fetch drift data via /api/sync-plan
    empty = json.dumps({"summary": {}, "sync_plan": {}, "system_artifacts": []})
    resp = make_response(render_template('drift.html', sync_plan=empty))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp


@app.route('/integrity', methods=['GET'])
def data_integrity():
    guard = _require_setup()
    if guard:
        return guard
    # Render page immediately; JS will fetch checks via /api/bcdr/integrity-checks
    assurance_data = json.dumps({"checks": [], "system_artifacts": []})
    return render_template('integrity.html', assurance_data=assurance_data)


_integrity_cache: Dict[str, Any] = {}
_integrity_cache_ts: float = 0.0
_INTEGRITY_CACHE_TTL = 300  # 5 minutes


@app.route('/api/bcdr/integrity-checks', methods=['GET'])
def api_integrity_checks():
    """JSON-only endpoint for Data Assurance checks (used by AJAX).

    Query params:
      phase=fast    — return only artifact-count checks (instant)
      phase=slow    — return only DFS + KQL checks (takes ~10-15s)
      phase=all     — return everything (default, uses cache)
      refresh=true  — bypass cache
    """
    guard = _require_setup()
    if guard:
        return jsonify({"error": "Setup required"}), 403

    global _integrity_cache, _integrity_cache_ts
    phase = request.args.get("phase", "all").lower()
    refresh = request.args.get("refresh", "false").lower() == "true"
    now = time.time()

    # Return cached full result if fresh (phase=all only)
    if phase == "all" and not refresh and _integrity_cache and (now - _integrity_cache_ts) < _INTEGRITY_CACHE_TTL:
        result = dict(_integrity_cache)
        result["cached"] = True
        result["cache_age_sec"] = int(now - _integrity_cache_ts)
        return jsonify(result)

    checks = []
    system_items = []
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")

    if p_id and s_id:
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            # --- Phase 0: Parallel fetch of workspace items + OneLake token ---
            with ThreadPoolExecutor(max_workers=3) as init_pool:
                f_p_items = init_pool.submit(get_workspace_items, p_id)
                f_s_items = init_pool.submit(get_workspace_items, s_id)
                f_ol_token = init_pool.submit(_get_onelake_token)

            raw_p = f_p_items.result()
            raw_s = f_s_items.result()
            p_items = _filter_business_items(raw_p)
            s_items = _filter_business_items(raw_s)
            system_items = _get_bcdr_system_items(raw_s)

            # --- Fast checks: artifact type counts (instant) ---
            fast_checks = []
            type_order = ["SemanticModel", "Report", "Notebook", "DataPipeline",
                          "Eventhouse", "KQLDatabase", "KQLQueryset", "Eventstream",
                          "MLModel", "MLExperiment", "DataAgent", "Ontology", "GraphModel"]
            p_by_type, s_by_type = {}, {}
            for i in p_items:
                p_by_type.setdefault(i.get("type"), []).append(i)
            for i in s_items:
                s_by_type.setdefault(i.get("type"), []).append(i)
            for atype in type_order:
                p_list, s_list = p_by_type.get(atype, []), s_by_type.get(atype, [])
                if not p_list and not s_list:
                    continue
                variance = abs(len(p_list) - len(s_list))
                fast_checks.append({"artifact": atype, "check": "Item Count",
                                    "primary": str(len(p_list)), "secondary": str(len(s_list)),
                                    "variance": str(variance),
                                    "status": "pass" if variance == 0 else "warning"})

            if phase == "fast":
                return jsonify({
                    "checks": fast_checks,
                    "system_artifacts": [{"name": i.get("displayName"), "type": i.get("type")} for i in system_items],
                    "phase": "fast",
                })

            # --- Slow checks: DFS + KQL (parallel) ---
            onelake_token = f_ol_token.result()
            onelake_headers = {"Authorization": f"Bearer {onelake_token}"} if onelake_token else None

            def _count_tables(ws_id, lh_id):
                if not onelake_headers:
                    return None
                dfs_url = f"https://onelake.dfs.fabric.microsoft.com/{ws_id}/{lh_id}/Tables"
                try:
                    resp = requests.get(dfs_url, headers=onelake_headers, timeout=15,
                                        params={"resource": "filesystem", "recursive": "true"})
                    if resp.status_code == 200:
                        paths = resp.json().get("paths", [])
                        return sum(1 for p in paths
                                   if p.get("isDirectory") == "true" and p.get("name", "").endswith("/_delta_log"))
                except Exception:
                    pass
                return None

            p_lakehouses = {i["displayName"]: i["id"] for i in p_items if i.get("type") == "Lakehouse"}
            s_lakehouses = {i["displayName"]: i["id"] for i in s_items if i.get("type") == "Lakehouse"}

            def _all_lakehouse_checks():
                lh_checks = []
                lh_futures = {}
                with ThreadPoolExecutor(max_workers=8) as pool:
                    for lh_name, p_lh_id in p_lakehouses.items():
                        s_lh_id = s_lakehouses.get(lh_name)
                        fp = pool.submit(_count_tables, p_id, p_lh_id)
                        fs = pool.submit(_count_tables, s_id, s_lh_id) if s_lh_id else None
                        lh_futures[lh_name] = (fp, fs, s_lh_id)
                for lh_name, (fp, fs, s_lh_id) in lh_futures.items():
                    p_count = fp.result()
                    s_count = fs.result() if fs else None
                    if p_count is not None and s_count is not None:
                        variance = abs(p_count - s_count)
                        lh_checks.append({"artifact": f"Lakehouse: {lh_name}", "check": "Table Count",
                                          "primary": str(p_count), "secondary": str(s_count),
                                          "variance": f"{variance} tables",
                                          "status": "pass" if variance == 0 else "warning"})
                    elif s_lh_id is None:
                        lh_checks.append({"artifact": f"Lakehouse: {lh_name}", "check": "Existence",
                                          "primary": "Present", "secondary": "Missing",
                                          "variance": "N/A", "status": "fail"})
                return lh_checks

            def _kql_db_assurance_api(p_db, s_db):
                db_checks = []
                db_name = p_db.get("displayName", "")
                if not s_db:
                    return [{"artifact": f"KQL DB: {db_name}", "check": "Existence",
                             "primary": "Present", "secondary": "Missing",
                             "variance": "N/A", "status": "fail"}]
                p_uri = p_db.get("properties", {}).get("queryServiceUri")
                s_uri = s_db.get("properties", {}).get("queryServiceUri")
                p_dbname = p_db.get("properties", {}).get("databaseName", db_name)
                s_dbname = s_db.get("properties", {}).get("databaseName", db_name)
                if not p_uri or not s_uri:
                    return db_checks
                try:
                    with ThreadPoolExecutor(max_workers=2) as tp:
                        fp_show = tp.submit(_run_kql_command, p_uri, p_dbname, ".show tables")
                        fs_show = tp.submit(_run_kql_command, s_uri, s_dbname, ".show tables")
                    def _ext(show_resp):
                        return [row[0] if isinstance(row, list) else row.get("TableName", "")
                                for row in show_resp.get("Tables", [{}])[0].get("Rows", [])
                                if (row[0] if isinstance(row, list) else row.get("TableName", ""))]
                    p_tables, s_tables = _ext(fp_show.result()), _ext(fs_show.result())
                    p_tc, s_tc = len(p_tables), len(s_tables)
                    db_checks.append({"artifact": f"KQL DB: {db_name}", "check": "Table Count",
                                      "primary": str(p_tc), "secondary": str(s_tc),
                                      "variance": f"{abs(p_tc - s_tc)} tables",
                                      "status": "pass" if p_tc == s_tc else "warning"})
                    s_set = set(s_tables)
                    def _rc(tname):
                        try:
                            pr = _run_kql_query(p_uri, p_dbname, f"{tname} | count")
                            p_rc = pr.get("Tables", [{}])[0].get("Rows", [[0]])[0]
                            p_rc = p_rc[0] if isinstance(p_rc, list) else p_rc.get("Count", 0)
                            s_rc = 0
                            if tname in s_set:
                                sr = _run_kql_query(s_uri, s_dbname, f"{tname} | count")
                                s_r = sr.get("Tables", [{}])[0].get("Rows", [[0]])[0]
                                s_rc = s_r[0] if isinstance(s_r, list) else s_r.get("Count", 0)
                            v = abs(int(p_rc) - int(s_rc))
                            return {"artifact": f"KQL DB: {db_name} → {tname}", "check": "Row Count",
                                    "primary": str(p_rc), "secondary": str(s_rc),
                                    "variance": f"{v} rows", "status": "pass" if v == 0 else "warning"}
                        except Exception:
                            return None
                    with ThreadPoolExecutor(max_workers=8) as rp:
                        db_checks.extend(r for r in rp.map(_rc, p_tables) if r)
                except Exception as e:
                    logger.warning(f"KQL assurance for {db_name}: {e}")
                return db_checks

            def _all_kql_checks():
                kql_checks = []
                try:
                    p_kql_dbs = fabric_api("GET", f"/workspaces/{p_id}/kqlDatabases").get("value", [])
                    s_kql_dbs = fabric_api("GET", f"/workspaces/{s_id}/kqlDatabases").get("value", [])
                    s_kql_by_name = {db.get("displayName"): db for db in s_kql_dbs}
                    with ThreadPoolExecutor(max_workers=4) as kql_pool:
                        futs = {kql_pool.submit(_kql_db_assurance_api, p_db,
                                s_kql_by_name.get(p_db.get("displayName"))): p_db for p_db in p_kql_dbs}
                        for fut in as_completed(futs):
                            kql_checks.extend(fut.result())
                except Exception as e:
                    logger.warning(f"KQL DB assurance error: {e}")
                return kql_checks

            slow_checks = []
            with ThreadPoolExecutor(max_workers=2) as phase_pool:
                f_lh = phase_pool.submit(_all_lakehouse_checks)
                f_kql = phase_pool.submit(_all_kql_checks)
            slow_checks.extend(f_lh.result())
            slow_checks.extend(f_kql.result())

            if phase == "slow":
                return jsonify({"checks": slow_checks, "phase": "slow"})

            # phase=all — combine and cache
            checks = slow_checks + fast_checks

        except Exception as e:
            logger.error(f"Assurance data error: {e}")
            checks.append({"artifact": "Error", "check": str(e),
                           "primary": "-", "secondary": "-", "variance": "-", "status": "fail"})

    result = {
        "checks": checks,
        "system_artifacts": [{"name": i.get("displayName"), "type": i.get("type")} for i in system_items],
    }
    _integrity_cache = result
    _integrity_cache_ts = time.time()
    result_out = dict(result)
    result_out["cached"] = False
    return jsonify(result_out)


@app.route('/architecture', methods=['GET'])
def architecture():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('architecture.html')


@app.route('/topology', methods=['GET'])
def topology():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('topology.html')


@app.route('/inventory', methods=['GET'])
def inventory():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('inventory.html')


@app.route('/failover', methods=['GET'])
def failover():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('failover.html')


@app.route('/gateways', methods=['GET'])
def gateways():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('gateways.html')


@app.route('/lakehouse', methods=['GET'])
def lakehouse_page():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('lakehouse.html')


@app.route('/rti', methods=['GET'])
def rti_page():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('rti.html')


@app.route('/devops', methods=['GET'])
def devops_page():
    guard = _require_setup()
    if guard:
        return guard
    return render_template('devops.html')


# ============================================================================
# ON-PREMISES DATA GATEWAY DISCOVERY
# ============================================================================


def get_gateway_info() -> Dict[str, Any]:
    """Discover all on-premises data gateways and their data sources via Fabric API."""
    gateways_resp = fabric_api("GET", "/gateways")
    raw_gateways = gateways_resp.get("value", [])

    gateways = []
    for gw in raw_gateways:
        gw_id = gw.get("id", "")
        gw_type = gw.get("type", "")

        # Parse annotation for machine/cluster info
        annotation = {}
        annotation_str = gw.get("gatewayAnnotation", "")
        if annotation_str:
            try:
                annotation = json.loads(annotation_str)
            except Exception:
                pass

        # Fetch members/data sources for this gateway
        datasources = []
        try:
            members_resp = fabric_api("GET", f"/gateways/{gw_id}/members")
            for m in members_resp.get("value", []):
                datasources.append({
                    "id": m.get("id", ""),
                    "datasourceType": m.get("type", ""),
                    "connectionDetails": m.get("connectionDetails", {}),
                    "credentialType": m.get("credentialType", ""),
                    "datasourceName": m.get("displayName", m.get("name", "")),
                })
        except Exception:
            pass

        # Also try listing connections on the gateway
        if not datasources:
            try:
                conn_resp = fabric_api("GET", f"/gateways/{gw_id}/datasources")
                for ds in conn_resp.get("value", []):
                    conn_details = ds.get("connectionDetails", "")
                    if isinstance(conn_details, str) and conn_details:
                        try:
                            conn_details = json.loads(conn_details)
                        except Exception:
                            pass
                    datasources.append({
                        "id": ds.get("id", ""),
                        "datasourceType": ds.get("datasourceType", ds.get("type", "")),
                        "connectionDetails": conn_details,
                        "credentialType": ds.get("credentialType", ""),
                        "datasourceName": ds.get("datasourceName", ds.get("displayName", "")),
                    })
            except Exception as e:
                logger.warning(f"Could not fetch datasources for gateway {gw_id}: {e}")

        gateways.append({
            "id": gw_id,
            "name": gw.get("name", "Unknown"),
            "type": gw_type,
            "publicKey": gw.get("publicKey", {}),
            "version": annotation.get("gatewayWireDefaultVersion", ""),
            "machineName": annotation.get("gatewayMachine", ""),
            "clusterStatus": annotation.get("gatewayClusterStatus", ""),
            "contactInfo": annotation.get("gatewayContactInformation", ""),
            "datasources": datasources,
            "datasourceCount": len(datasources),
        })

    return {
        "gateways": gateways,
        "total": len(gateways),
    }


@app.route('/api/gateways', methods=['GET'])
def api_gateways():
    """Get all on-premises data gateways and their data sources."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    try:
        return jsonify(get_gateway_info())
    except Exception as e:
        logger.exception("Error fetching gateway info")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# AZURE DEVOPS INTEGRATION
# ============================================================================

_DEVOPS_CONFIG_FILE = os.path.join(os.path.dirname(__file__), ".devops_config.json")

_devops_config: Dict[str, Any] = {
    "enabled": False,
    "org": "",           # e.g., "FabricGuard"
    "project": "",       # e.g., "FabricBCDR"
    "repo": "",          # e.g., "FabricBCDR"
    "pat": "",           # Personal Access Token
    "pipeline_id": None, # ADO pipeline ID (int)
    "primary_branch": "FabricBCDRArtifacts",
    "secondary_branch": "FabricBCDRSecondary",
    "auto_trigger": True,  # Trigger pipeline when auto-sync detects new items
}


def _load_devops_config():
    global _devops_config
    try:
        if os.path.exists(_DEVOPS_CONFIG_FILE):
            with open(_DEVOPS_CONFIG_FILE, "r") as f:
                saved = json.load(f)
                _devops_config.update(saved)
    except Exception:
        pass


def _save_devops_config():
    try:
        with open(_DEVOPS_CONFIG_FILE, "w") as f:
            json.dump(_devops_config, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save devops config: {e}")


def _ado_api(method: str, path: str, payload: Any = None) -> Dict:
    """Call Azure DevOps REST API using the configured PAT."""
    pat = _devops_config.get("pat", "")
    org = _devops_config.get("org", "")
    if not pat or not org:
        raise RuntimeError("Azure DevOps PAT and org must be configured")

    import base64 as b64
    auth_b64 = b64.b64encode(f":{pat}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_b64}",
        "Content-Type": "application/json",
    }
    url = f"https://dev.azure.com/{org}/{path}"
    resp = requests.request(method, url, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"ADO API {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.text else {}


def _get_git_status(workspace_id: str) -> Dict[str, Any]:
    """Get Fabric Git integration status for a workspace."""
    try:
        # Connection details (org, project, repo, branch)
        conn = fabric_api("GET", f"/workspaces/{workspace_id}/git/connection")
        provider = conn.get("gitProviderDetails", {})
        branch = provider.get("branch", "") or provider.get("branchName", "")
        repo = provider.get("repositoryName", "")
        org = provider.get("organizationName", "")
        project = provider.get("projectName", "")

        # Sync status (commit hashes, changes)
        resp = fabric_api("GET", f"/workspaces/{workspace_id}/git/status")
        return {
            "connected": True,
            "synced": resp.get("workspaceHead", "") == resp.get("remoteCommitHash", ""),
            "head_commit": resp.get("workspaceHead", ""),
            "remote_commit": resp.get("remoteCommitHash", ""),
            "branch": branch,
            "repo": repo,
            "org": org,
            "project": project,
            "sync_state": resp.get("syncState", ""),
            "changes": resp.get("changes", []),
            "raw": resp,
        }
    except Exception as e:
        err_str = str(e)
        if "404" in err_str or "not connected" in err_str.lower() or "ItemNotFound" in err_str:
            return {"connected": False, "error": "No Git integration configured"}
        return {"connected": False, "error": err_str}


def _trigger_ado_pipeline(reason: str = "Resiliency & Recovery auto-trigger") -> Dict[str, Any]:
    """Trigger the configured Azure DevOps pipeline."""
    project = _devops_config.get("project", "")
    pipeline_id = _devops_config.get("pipeline_id")
    primary_branch = _devops_config.get("primary_branch", "main")

    if not pipeline_id:
        return {"error": "No pipeline ID configured"}

    try:
        result = _ado_api("POST", f"{project}/_apis/pipelines/{pipeline_id}/runs?api-version=7.1", payload={
            "resources": {
                "repositories": {
                    "self": {
                        "refName": f"refs/heads/{primary_branch}"
                    }
                }
            },
            "templateParameters": {
                "trigger_reason": reason,
            }
        })
        run_id = result.get("id")
        state = result.get("state", "unknown")
        url = result.get("_links", {}).get("web", {}).get("href", "")
        logger.info(f"Triggered ADO pipeline {pipeline_id}, run #{run_id}: {state}")
        return {"ok": True, "run_id": run_id, "state": state, "url": url}
    except Exception as e:
        logger.warning(f"Failed to trigger ADO pipeline: {e}")
        return {"error": str(e)}


def _get_ado_pipeline_runs(top: int = 10) -> List[Dict]:
    """Get recent pipeline runs from Azure DevOps."""
    project = _devops_config.get("project", "")
    pipeline_id = _devops_config.get("pipeline_id")
    if not pipeline_id:
        return []
    try:
        result = _ado_api("GET", f"{project}/_apis/pipelines/{pipeline_id}/runs?api-version=7.1&$top={top}")
        runs = []
        for r in result.get("value", []):
            runs.append({
                "id": r.get("id"),
                "state": r.get("state", ""),
                "result": r.get("result", ""),
                "created": r.get("createdDate", ""),
                "finished": r.get("finishedDate", ""),
                "url": r.get("_links", {}).get("web", {}).get("href", ""),
                "name": r.get("name", ""),
                "template_params": r.get("templateParameters", {}),
            })
        return runs
    except Exception as e:
        logger.warning(f"Failed to fetch ADO pipeline runs: {e}")
        return []


def trigger_devops_on_new_artifacts(new_item_count: int):
    """Called by auto-sync watcher when new items are detected — triggers ADO pipeline if configured."""
    if not _devops_config.get("enabled") or not _devops_config.get("auto_trigger"):
        return
    if not _devops_config.get("pat") or not _devops_config.get("pipeline_id"):
        return
    if new_item_count <= 0:
        return
    reason = f"Auto-sync detected {new_item_count} new artifact(s)"
    result = _trigger_ado_pipeline(reason)
    if result.get("ok"):
        logger.info(f"DevOps: triggered pipeline — {reason}")
    else:
        logger.warning(f"DevOps: pipeline trigger failed — {result.get('error')}")


@app.route('/api/devops/status', methods=['GET'])
def api_devops_status():
    """Get DevOps integration status: config, Git status for both workspaces, recent runs."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")

    p_git = _get_git_status(p_id) if p_id else {"connected": False}
    s_git = _get_git_status(s_id) if s_id else {"connected": False}

    # Auto-fill config from detected Git connection if not yet configured
    if not _devops_config.get("org") and p_git.get("org"):
        _devops_config["org"] = p_git["org"]
    if not _devops_config.get("project") and p_git.get("project"):
        _devops_config["project"] = p_git["project"]
    if not _devops_config.get("repo") and p_git.get("repo"):
        _devops_config["repo"] = p_git["repo"]
    if not _devops_config.get("primary_branch") and p_git.get("branch"):
        _devops_config["primary_branch"] = p_git["branch"]
    if not _devops_config.get("secondary_branch") and s_git.get("branch"):
        _devops_config["secondary_branch"] = s_git["branch"]

    # Get pipeline runs if configured
    runs = []
    pipeline_info = None
    if _devops_config.get("enabled") and _devops_config.get("pat") and _devops_config.get("pipeline_id"):
        runs = _get_ado_pipeline_runs(10)
        try:
            project = _devops_config.get("project", "")
            pid = _devops_config.get("pipeline_id")
            pipeline_info = _ado_api("GET", f"{project}/_apis/pipelines/{pid}?api-version=7.1")
        except Exception:
            pass

    return jsonify({
        "config": {
            "enabled": _devops_config.get("enabled", False),
            "org": _devops_config.get("org", ""),
            "project": _devops_config.get("project", ""),
            "repo": _devops_config.get("repo", ""),
            "pipeline_id": _devops_config.get("pipeline_id"),
            "primary_branch": _devops_config.get("primary_branch", ""),
            "secondary_branch": _devops_config.get("secondary_branch", ""),
            "auto_trigger": _devops_config.get("auto_trigger", False),
            "has_pat": bool(_devops_config.get("pat")),
        },
        "primary_git": p_git,
        "secondary_git": s_git,
        "pipeline": pipeline_info,
        "runs": runs,
    })


@app.route('/api/devops/configure', methods=['POST'])
def api_devops_configure():
    """Save Azure DevOps integration configuration."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}

    _devops_config["enabled"] = data.get("enabled", False)
    _devops_config["org"] = data.get("org", "").strip()
    _devops_config["project"] = data.get("project", "").strip()
    _devops_config["repo"] = data.get("repo", "").strip()
    _devops_config["primary_branch"] = data.get("primary_branch", "FabricBCDRArtifacts").strip()
    _devops_config["secondary_branch"] = data.get("secondary_branch", "FabricBCDRSecondary").strip()
    _devops_config["auto_trigger"] = data.get("auto_trigger", True)
    if data.get("pat"):
        _devops_config["pat"] = data["pat"].strip()
    if data.get("pipeline_id") is not None:
        _devops_config["pipeline_id"] = int(data["pipeline_id"]) if data["pipeline_id"] else None

    _save_devops_config()
    return jsonify({"status": "ok", "message": "DevOps configuration saved"})


@app.route('/api/devops/trigger', methods=['POST'])
def api_devops_trigger():
    """Manually trigger the Azure DevOps pipeline."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    if not _devops_config.get("enabled"):
        return jsonify({"error": "DevOps integration is not enabled"}), 400

    data = request.get_json() or {}
    reason = data.get("reason", "Manual trigger from Resiliency & Recovery Dashboard")
    result = _trigger_ado_pipeline(reason)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/devops/runs', methods=['GET'])
def api_devops_runs():
    """Get recent Azure DevOps pipeline runs."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    top = request.args.get("top", 10, type=int)
    return jsonify({"runs": _get_ado_pipeline_runs(top)})


# ============================================================================
# REAL-TIME INTELLIGENCE (RTI) API
# ============================================================================

@app.route('/api/rti/status', methods=['GET'])
def api_rti_status():
    """Get RTI artifact status across primary and secondary workspaces."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    RTI_ARTIFACT_TYPES = ["Eventhouse", "KQLDatabase", "KQLQueryset", "Eventstream"]
    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    try:
        all_primary = get_workspace_items(p_id)
        all_secondary = get_workspace_items(s_id)
        primary_items = _filter_business_items(all_primary)
        secondary_items = _filter_business_items(all_secondary)

        types_data = {}
        for art_type in RTI_ARTIFACT_TYPES:
            p_list = [i for i in primary_items if i.get("type") == art_type]
            s_list = [i for i in secondary_items if i.get("type") == art_type]
            p_names = {i.get("displayName") for i in p_list}
            s_names = {i.get("displayName") for i in s_list}
            mirrored = len(p_names & s_names)
            types_data[art_type] = {
                "primary": [{"id": i.get("id"), "name": i.get("displayName")} for i in p_list],
                "secondary": [{"id": i.get("id"), "name": i.get("displayName")} for i in s_list],
                "mirrored": mirrored,
            }

        return jsonify({"types": types_data})
    except Exception as e:
        logger.exception("Error getting RTI status")
        return jsonify({"error": str(e)}), 500


@app.route('/api/rti/sync', methods=['POST'])
def api_rti_sync():
    """Sync RTI artifacts from primary to secondary (all types or a specific type).

    Request body (JSON):
      { "type": "Eventhouse" }       — sync one type
      { }                            — sync all RTI types
      { "dry_run": true }            — preview only
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    artifact_type = data.get("type")
    dry_run = data.get("dry_run", False)

    RTI_TYPES = ["Eventhouse", "KQLDatabase", "KQLQueryset", "Eventstream"]
    if artifact_type and artifact_type not in RTI_TYPES:
        return jsonify({"error": f"Invalid RTI type: {artifact_type}. Must be one of {RTI_TYPES}"}), 400

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    target_types = [artifact_type] if artifact_type else RTI_TYPES

    try:
        # Build connection string replacement map
        all_primary = get_workspace_items(p_id)
        all_secondary = get_workspace_items(s_id)
        conn_map = {}
        conn_map[p_id] = s_id
        s_by_name = {i.get("displayName", ""): i for i in all_secondary}
        for p_item in all_primary:
            p_name = p_item.get("displayName", "")
            pid = p_item.get("id", "")
            s_item = s_by_name.get(p_name)
            if s_item and pid:
                sid = s_item.get("id", "")
                if sid and pid != sid:
                    conn_map[pid] = sid

        # Build Eventhouse name→ID maps for KQL Database parent remapping
        p_eventhouses = {i.get("displayName"): i.get("id") for i in all_primary if i.get("type") == "Eventhouse"}
        s_eventhouses = {i.get("displayName"): i.get("id") for i in all_secondary if i.get("type") == "Eventhouse"}
        # Map primary Eventhouse IDs → secondary Eventhouse IDs (by name)
        eventhouse_id_map = {}
        for eh_name, p_eh_id in p_eventhouses.items():
            s_eh_id = s_eventhouses.get(eh_name)
            if s_eh_id and p_eh_id:
                eventhouse_id_map[p_eh_id] = s_eh_id

        results = {}
        for t in target_types:
            p_items = [i for i in all_primary if i.get("type") == t]
            s_items = [i for i in all_secondary if i.get("type") == t]
            s_names = {i.get("displayName") for i in s_items}

            type_result = {"synced": [], "failed": [], "already_mirrored": [], "skipped": []}

            for p_item in p_items:
                name = p_item.get("displayName", "")
                pid = p_item.get("id", "")

                if name in s_names:
                    type_result["already_mirrored"].append(name)
                    continue

                try:
                    # Export definition
                    definition = {}
                    parts = []
                    try:
                        export_resp = fabric_api(
                            "POST", f"/workspaces/{p_id}/items/{pid}/getDefinition",
                            timeout=120,
                        )
                        if export_resp and isinstance(export_resp, dict):
                            definition = export_resp.get("definition", {})
                            parts = definition.get("parts", [])
                    except Exception as ex:
                        logger.warning(f"RTI sync: could not export definition for {name}: {ex}")

                    # Rewrite connections in definition parts
                    if parts and conn_map:
                        try:
                            parts = _rewrite_definition_parts(parts, conn_map)
                            definition = dict(definition)
                            definition["parts"] = parts
                        except Exception as rw_err:
                            logger.warning(f"RTI sync: connection rewrite failed for {name}: {rw_err}")

                    if dry_run:
                        type_result["synced"].append(name)
                        continue

                    create_payload = {"displayName": name, "type": t}
                    if parts:
                        create_payload["definition"] = definition

                    # KQL Database: remap parentEventhouseItemId to the secondary Eventhouse
                    # KQL Database requires creationPayload (not definition) — they are mutually exclusive
                    if t == "KQLDatabase":
                        # Remove definition for KQL Database — the API doesn't support both
                        if "definition" in create_payload:
                            del create_payload["definition"]

                        # Find parent Eventhouse in primary — match by name convention
                        parent_eh_id = None
                        if name in p_eventhouses:
                            parent_eh_id = p_eventhouses[name]
                        else:
                            for eh_name, eh_id in p_eventhouses.items():
                                parent_eh_id = eh_id
                                break

                        if parent_eh_id and parent_eh_id in eventhouse_id_map:
                            s_eh_id = eventhouse_id_map[parent_eh_id]
                            create_payload["creationPayload"] = {
                                "databaseType": "ReadWrite",
                                "parentEventhouseItemId": s_eh_id,
                            }
                            logger.info(f"RTI sync: KQL DB '{name}' → secondary Eventhouse {s_eh_id}")
                        elif s_eventhouses:
                            # Fallback: use any available secondary Eventhouse
                            fallback_eh = next(iter(s_eventhouses.values()))
                            create_payload["creationPayload"] = {
                                "databaseType": "ReadWrite",
                                "parentEventhouseItemId": fallback_eh,
                            }
                            logger.info(f"RTI sync: KQL DB '{name}' → fallback Eventhouse {fallback_eh}")
                        else:
                            logger.warning(f"RTI sync: No Eventhouse in secondary for KQL DB '{name}' — skipping")
                            type_result["failed"].append({
                                "name": name,
                                "error": "No Eventhouse available in secondary workspace. Sync Eventhouses first.",
                            })
                            continue

                    fabric_api("POST", f"/workspaces/{s_id}/items", payload=create_payload, timeout=120)
                    type_result["synced"].append(name)
                    logger.info(f"RTI sync: created {t} '{name}' in secondary")

                except Exception as e:
                    logger.error(f"RTI sync: failed to sync {t} '{name}': {e}")
                    type_result["failed"].append({"name": name, "error": str(e)})

            results[t] = type_result

        # Clear item cache for secondary
        _cache.pop(f"items:{s_id}", None)
        _cache_ttl.pop(f"items:{s_id}", None)

        return jsonify({
            "status": "ok",
            "dry_run": dry_run,
            "results": results,
        })
    except Exception as e:
        logger.exception("Error in RTI sync")
        return jsonify({"error": str(e)}), 500


@app.route('/api/rti/validate', methods=['GET'])
def api_rti_validate():
    """Validate RTI artifacts are correctly synced — check for missing items
    and stale connection strings in secondary definitions."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    RTI_TYPES = ["Eventhouse", "KQLDatabase", "KQLQueryset", "Eventstream"]

    try:
        all_primary = get_workspace_items(p_id)
        all_secondary = get_workspace_items(s_id)

        report = {"status": "pass", "types": {}, "issues": [], "connection_issues": []}

        for art_type in RTI_TYPES:
            p_items = [i for i in all_primary if i.get("type") == art_type]
            s_items = [i for i in all_secondary if i.get("type") == art_type]
            p_names = {i.get("displayName") for i in p_items}
            s_names = {i.get("displayName") for i in s_items}
            missing = sorted(p_names - s_names)
            mirrored = p_names & s_names

            report["types"][art_type] = {
                "primary": len(p_items),
                "secondary": len(s_items),
                "mirrored": len(mirrored),
                "missing_in_secondary": missing,
            }

            for name in missing:
                report["status"] = "fail"
                report["issues"].append({
                    "severity": "error",
                    "type": art_type,
                    "name": name,
                    "message": f"{art_type} '{name}' missing in secondary",
                })

            # Check connection strings in secondary definitions
            for s_item in s_items:
                if s_item.get("displayName") not in mirrored:
                    continue
                try:
                    export_resp = fabric_api(
                        "POST",
                        f"/workspaces/{s_id}/items/{s_item['id']}/getDefinition",
                        timeout=60,
                    )
                    definition = export_resp.get("definition", {})
                    for part in definition.get("parts", []):
                        payload_b64 = part.get("payload", "")
                        if not payload_b64:
                            continue
                        try:
                            import base64 as _b64
                            payload_text = _b64.b64decode(payload_b64).decode("utf-8")
                            if p_id in payload_text:
                                report["connection_issues"].append({
                                    "type": art_type,
                                    "name": s_item.get("displayName"),
                                    "part": part.get("path", "?"),
                                    "message": "Still references primary workspace ID",
                                })
                                if report["status"] == "pass":
                                    report["status"] = "warn"
                        except Exception:
                            pass
                except Exception:
                    pass

        # Informational notes
        kql_dbs = [i for i in all_primary if i.get("type") == "KQLDatabase"]
        if kql_dbs:
            report["issues"].append({
                "severity": "info",
                "type": "KQLDatabase",
                "message": (
                    f"{len(kql_dbs)} KQL Database(s) require continuous-export "
                    "for data replication (schema only is synced via definition)."
                ),
            })

        es_items = [i for i in all_secondary if i.get("type") == "Eventstream"]
        if es_items:
            report["issues"].append({
                "severity": "info",
                "type": "Eventstream",
                "message": (
                    f"{len(es_items)} Eventstream(s) in secondary — source connections "
                    "(Event Hub, Kafka) require manual credential re-authentication."
                ),
            })

        return jsonify(report)
    except Exception as e:
        logger.exception("Error validating RTI")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# RTI DATA MANAGEMENT — Ingest, Query, Export, Replicate Data
# ============================================================================

@app.route('/api/rti/kql-databases', methods=['GET'])
def api_rti_kql_databases():
    """List KQL Databases with their query URIs and table counts."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id:
        return jsonify({"error": "Primary workspace not configured"}), 400

    result = {"primary": [], "secondary": []}

    for role, ws_id in [("primary", p_id), ("secondary", s_id)]:
        if not ws_id:
            continue
        try:
            dbs_resp = fabric_api("GET", f"/workspaces/{ws_id}/kqlDatabases")
            for db in dbs_resp.get("value", []):
                db_info = {
                    "id": db.get("id"),
                    "name": db.get("displayName"),
                    "queryUri": db.get("properties", {}).get("queryServiceUri"),
                    "ingestionUri": db.get("properties", {}).get("ingestionServiceUri"),
                    "databaseName": db.get("properties", {}).get("databaseName", db.get("displayName")),
                    "oneLakeAvailability": db.get("properties", {}).get("oneLakeCachingEnabled"),
                    "tables": [],
                    "row_counts": {},
                }
                # Try to list tables
                query_uri = db_info["queryUri"]
                db_name = db_info["databaseName"]
                if query_uri and db_name:
                    try:
                        tables_resp = _run_kql_command(query_uri, db_name, ".show tables")
                        rows = tables_resp.get("Tables", [{}])[0].get("Rows", [])
                        columns = tables_resp.get("Tables", [{}])[0].get("Columns", [])
                        name_idx = next((i for i, c in enumerate(columns) if c.get("ColumnName") == "TableName"), 0)
                        for row in rows:
                            tname = row[name_idx] if isinstance(row, list) else row.get("TableName", "")
                            db_info["tables"].append(tname)
                        # Get row counts for each table
                        for tname in db_info["tables"]:
                            try:
                                count_resp = _run_kql_query(query_uri, db_name, f"{tname} | count")
                                count_rows = count_resp.get("Tables", [{}])[0].get("Rows", [])
                                if count_rows:
                                    db_info["row_counts"][tname] = count_rows[0][0] if isinstance(count_rows[0], list) else count_rows[0].get("Count", 0)
                            except Exception:
                                db_info["row_counts"][tname] = "?"
                    except Exception as e:
                        db_info["tables_error"] = str(e)
                result[role].append(db_info)
        except Exception as e:
            logger.warning(f"Could not list KQL databases for {role}: {e}")

    return jsonify(result)


@app.route('/api/rti/ingest-sample-data', methods=['POST'])
def api_rti_ingest_sample_data():
    """Ingest sample data into the primary KQL Database for testing."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    if not p_id:
        return jsonify({"error": "Primary workspace not configured"}), 400

    data = request.get_json(silent=True) or {}
    target_db_name = data.get("database", "RTI_Demo_KQLDatabase")

    try:
        # Find the KQL database
        dbs_resp = fabric_api("GET", f"/workspaces/{p_id}/kqlDatabases")
        target_db = None
        for db in dbs_resp.get("value", []):
            if db.get("displayName") == target_db_name:
                target_db = db
                break

        if not target_db:
            return jsonify({"error": f"KQL Database '{target_db_name}' not found in primary"}), 404

        query_uri = target_db.get("properties", {}).get("queryServiceUri")
        db_name = target_db.get("properties", {}).get("databaseName", target_db_name)
        if not query_uri:
            return jsonify({"error": "No queryServiceUri found for this KQL Database"}), 400

        results = {"tables_created": [], "data_ingested": [], "errors": []}

        # 1. Create SensorReadings table
        try:
            _run_kql_command(query_uri, db_name,
                ".create-merge table SensorReadings "
                "(Timestamp: datetime, DeviceId: string, Temperature: real, "
                "Humidity: real, Pressure: real, Location: string, Status: string)"
            )
            results["tables_created"].append("SensorReadings")
            logger.info("Created table SensorReadings")
        except Exception as e:
            results["errors"].append(f"Create SensorReadings: {e}")

        # 2. Create SystemEvents table
        try:
            _run_kql_command(query_uri, db_name,
                ".create-merge table SystemEvents "
                "(Timestamp: datetime, EventType: string, Source: string, "
                "Severity: string, Message: string, CorrelationId: string)"
            )
            results["tables_created"].append("SystemEvents")
            logger.info("Created table SystemEvents")
        except Exception as e:
            results["errors"].append(f"Create SystemEvents: {e}")

        # 3. Ingest sample sensor data (inline)
        sensor_lines = []
        import random
        from datetime import datetime as dt, timedelta as td
        base_time = dt.utcnow() - td(hours=2)
        devices = ["sensor-001", "sensor-002", "sensor-003", "sensor-004", "sensor-005"]
        locations = ["Building-A", "Building-B", "Building-C", "Warehouse-1", "Warehouse-2"]
        for i in range(50):
            ts = (base_time + td(minutes=i * 2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            dev = devices[i % len(devices)]
            temp = round(20 + random.uniform(-5, 15), 1)
            hum = round(40 + random.uniform(-10, 30), 1)
            pres = round(1013 + random.uniform(-10, 10), 1)
            loc = locations[i % len(locations)]
            status = "OK" if temp < 30 else "WARNING"
            sensor_lines.append(f"{ts},{dev},{temp},{hum},{pres},{loc},{status}")

        sensor_data = "\n".join(sensor_lines)
        try:
            _run_kql_command(query_uri, db_name,
                f".ingest inline into table SensorReadings <| {sensor_data}"
            )
            results["data_ingested"].append({"table": "SensorReadings", "rows": len(sensor_lines)})
            logger.info(f"Ingested {len(sensor_lines)} rows into SensorReadings")
        except Exception as e:
            results["errors"].append(f"Ingest SensorReadings: {e}")

        # 4. Ingest sample event data
        event_lines = []
        event_types = ["Startup", "Shutdown", "Alert", "Maintenance", "Deployment", "HealthCheck"]
        sources = ["AppServer", "Database", "LoadBalancer", "Gateway", "Scheduler"]
        severities = ["Info", "Warning", "Error", "Info", "Info", "Info"]
        for i in range(30):
            ts = (base_time + td(minutes=i * 3)).strftime("%Y-%m-%dT%H:%M:%SZ")
            etype = event_types[i % len(event_types)]
            src = sources[i % len(sources)]
            sev = severities[i % len(severities)]
            msg = f"{etype} event from {src} - iteration {i}"
            cid = f"corr-{i:04d}"
            event_lines.append(f"{ts},{etype},{src},{sev},{msg},{cid}")

        event_data = "\n".join(event_lines)
        try:
            _run_kql_command(query_uri, db_name,
                f".ingest inline into table SystemEvents <| {event_data}"
            )
            results["data_ingested"].append({"table": "SystemEvents", "rows": len(event_lines)})
            logger.info(f"Ingested {len(event_lines)} rows into SystemEvents")
        except Exception as e:
            results["errors"].append(f"Ingest SystemEvents: {e}")

        return jsonify(results)
    except Exception as e:
        logger.exception("Error ingesting sample data")
        return jsonify({"error": str(e)}), 500


@app.route('/api/rti/kql-tables', methods=['GET'])
def api_rti_kql_tables():
    """Compare KQL tables between primary and secondary databases."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    db_name_filter = request.args.get("database")

    result = {"databases": []}

    try:
        p_dbs = fabric_api("GET", f"/workspaces/{p_id}/kqlDatabases").get("value", [])
        s_dbs = fabric_api("GET", f"/workspaces/{s_id}/kqlDatabases").get("value", [])
        s_by_name = {db.get("displayName"): db for db in s_dbs}

        for p_db in p_dbs:
            p_name = p_db.get("displayName")
            if db_name_filter and p_name != db_name_filter:
                continue
            s_db = s_by_name.get(p_name)
            if not s_db:
                continue

            p_uri = p_db.get("properties", {}).get("queryServiceUri")
            s_uri = s_db.get("properties", {}).get("queryServiceUri")
            p_dbname = p_db.get("properties", {}).get("databaseName", p_name)
            s_dbname = s_db.get("properties", {}).get("databaseName", p_name)

            db_comparison = {
                "name": p_name,
                "primary_id": p_db.get("id"),
                "secondary_id": s_db.get("id"),
                "tables": [],
            }

            # List tables in primary
            p_tables = {}
            if p_uri:
                try:
                    p_show = _run_kql_command(p_uri, p_dbname, ".show tables")
                    for row in p_show.get("Tables", [{}])[0].get("Rows", []):
                        tname = row[0] if isinstance(row, list) else row.get("TableName", "")
                        p_tables[tname] = True
                except Exception:
                    pass

            # List tables in secondary
            s_tables = {}
            if s_uri:
                try:
                    s_show = _run_kql_command(s_uri, s_dbname, ".show tables")
                    for row in s_show.get("Tables", [{}])[0].get("Rows", []):
                        tname = row[0] if isinstance(row, list) else row.get("TableName", "")
                        s_tables[tname] = True
                except Exception:
                    pass

            # Row counts
            for tname in sorted(set(list(p_tables.keys()) + list(s_tables.keys()))):
                table_info = {"name": tname, "in_primary": tname in p_tables, "in_secondary": tname in s_tables,
                              "primary_rows": 0, "secondary_rows": 0}
                if tname in p_tables and p_uri:
                    try:
                        cr = _run_kql_query(p_uri, p_dbname, f"{tname} | count")
                        rows = cr.get("Tables", [{}])[0].get("Rows", [])
                        table_info["primary_rows"] = rows[0][0] if rows and isinstance(rows[0], list) else 0
                    except Exception:
                        pass
                if tname in s_tables and s_uri:
                    try:
                        cr = _run_kql_query(s_uri, s_dbname, f"{tname} | count")
                        rows = cr.get("Tables", [{}])[0].get("Rows", [])
                        table_info["secondary_rows"] = rows[0][0] if rows and isinstance(rows[0], list) else 0
                    except Exception:
                        pass
                db_comparison["tables"].append(table_info)

            result["databases"].append(db_comparison)

        return jsonify(result)
    except Exception as e:
        logger.exception("Error comparing KQL tables")
        return jsonify({"error": str(e)}), 500


@app.route('/api/rti/replicate-kql-data', methods=['POST'])
def api_rti_replicate_kql_data():
    """Replicate data from primary KQL Database(s) to secondary.

    Modes:
      - "full"        — Clear secondary, copy all data (default for backward compat)
      - "incremental" — Use watermark (datetime column) to sync only new rows

    Pass database="ALL" to replicate ALL KQL Databases in the Eventhouse.
    """
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    target_db_name = data.get("database", "RTI_Demo_KQLDatabase")
    table_filter = data.get("table")  # Optional: sync specific table
    max_rows = data.get("max_rows", 10000)  # Safety limit
    mode = data.get("mode", "full")  # "full" or "incremental"

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    try:
        p_dbs = fabric_api("GET", f"/workspaces/{p_id}/kqlDatabases").get("value", [])
        s_dbs = fabric_api("GET", f"/workspaces/{s_id}/kqlDatabases").get("value", [])

        # Determine which DBs to replicate
        if target_db_name == "ALL":
            db_pairs = []
            s_db_by_name = {db.get("displayName"): db for db in s_dbs}
            for p_db in p_dbs:
                name = p_db.get("displayName", "")
                s_db = s_db_by_name.get(name)
                if s_db:
                    db_pairs.append((name, p_db, s_db))
            if not db_pairs:
                return jsonify({"error": "No matching KQL Database pairs found"}), 404
        else:
            p_db = next((db for db in p_dbs if db.get("displayName") == target_db_name), None)
            s_db = next((db for db in s_dbs if db.get("displayName") == target_db_name), None)
            if not p_db:
                return jsonify({"error": f"KQL Database '{target_db_name}' not found in primary"}), 404
            if not s_db:
                return jsonify({"error": f"KQL Database '{target_db_name}' not found in secondary. Sync artifacts first."}), 404
            db_pairs = [(target_db_name, p_db, s_db)]

        all_results = {"databases_synced": [], "databases_failed": [], "total_rows_copied": 0, "mode": mode}

        for db_name, p_db, s_db in db_pairs:
            p_uri = p_db.get("properties", {}).get("queryServiceUri")
            s_uri = s_db.get("properties", {}).get("queryServiceUri")
            p_dbname = p_db.get("properties", {}).get("databaseName", db_name)
            s_dbname = s_db.get("properties", {}).get("databaseName", db_name)

            if not p_uri or not s_uri:
                all_results["databases_failed"].append({"database": db_name, "error": "Query URIs not available"})
                continue

            db_result = {"database": db_name, "tables_synced": [], "tables_failed": [], "rows_copied": 0}

            # List tables in primary
            p_tables = []
            try:
                p_show = _run_kql_command(p_uri, p_dbname, ".show tables")
                for row in p_show.get("Tables", [{}])[0].get("Rows", []):
                    tname = row[0] if isinstance(row, list) else row.get("TableName", "")
                    if tname:
                        p_tables.append(tname)
            except Exception as e:
                all_results["databases_failed"].append({"database": db_name, "error": f"Could not list tables: {e}"})
                continue

            if not p_tables:
                db_result["tables_synced"] = []
                all_results["databases_synced"].append(db_result)
                continue

            if table_filter:
                p_tables = [t for t in p_tables if t == table_filter]

            for tname in p_tables:
                try:
                    schema_resp = _run_kql_command(p_uri, p_dbname, f".show table {tname} cslschema")
                    schema_rows = schema_resp.get("Tables", [{}])[0].get("Rows", [])
                    if not schema_rows:
                        db_result["tables_failed"].append({"table": tname, "error": "No schema found"})
                        continue
                    schema_str = schema_rows[0][1] if isinstance(schema_rows[0], list) else schema_rows[0].get("Schema", "")

                    try:
                        _run_kql_command(s_uri, s_dbname, f".create-merge table {tname} ({schema_str})")
                    except Exception as create_err:
                        logger.warning(f"Create table {tname} in secondary {db_name}: {create_err}")

                    # Determine query based on mode
                    watermark_key = f"{db_name}.{tname}"
                    ts_col = None
                    old_watermark = None
                    table_mode = mode  # may fall back to full

                    if mode == "incremental":
                        ts_col = _detect_timestamp_column(schema_str)
                        if ts_col:
                            old_watermark = _rti_watermarks.get(watermark_key)
                            if old_watermark:
                                kql_query = f"{tname} | where {ts_col} > datetime({old_watermark}) | order by {ts_col} asc | take {max_rows}"
                                logger.info(f"Incremental query for {watermark_key}: where {ts_col} > {old_watermark}")
                            else:
                                # First incremental run — do a full load this time, then track watermark
                                kql_query = f"{tname} | order by {ts_col} asc | take {max_rows}"
                                logger.info(f"First incremental run for {watermark_key} — full initial load")
                                # Clear secondary for the initial load to ensure clean state
                                try:
                                    _run_kql_command(s_uri, s_dbname, f".clear table {tname} data")
                                except Exception:
                                    pass
                        else:
                            # No datetime column found — fall back to full mode for this table
                            logger.info(f"No datetime column in {watermark_key}, falling back to full mode")
                            table_mode = "full"
                            kql_query = f"{tname} | take {max_rows}"
                    else:
                        kql_query = f"{tname} | take {max_rows}"

                    if table_mode == "full":
                        # Clear existing data in secondary to avoid duplicates
                        try:
                            _run_kql_command(s_uri, s_dbname, f".clear table {tname} data")
                            logger.info(f"Cleared existing data from {db_name}.{tname} in secondary")
                        except Exception as clear_err:
                            logger.warning(f"Could not clear {db_name}.{tname} in secondary (may be empty): {clear_err}")

                    query_resp = _run_kql_query(p_uri, p_dbname, kql_query)
                    data_tables = query_resp.get("Tables", [])
                    if not data_tables:
                        db_result["tables_synced"].append({"table": tname, "rows": 0, "mode": table_mode})
                        continue
                    data_rows = data_tables[0].get("Rows", [])
                    columns = data_tables[0].get("Columns", [])
                    if not data_rows:
                        db_result["tables_synced"].append({"table": tname, "rows": 0, "mode": table_mode})
                        continue

                    # Track new watermark from the last row's timestamp column
                    new_watermark = None
                    if ts_col and columns:
                        ts_idx = next((i for i, c in enumerate(columns) if c.get("ColumnName") == ts_col), None)
                        if ts_idx is not None and data_rows:
                            last_row = data_rows[-1]
                            last_ts = last_row[ts_idx] if isinstance(last_row, list) else last_row.get(ts_col)
                            if last_ts:
                                new_watermark = str(last_ts)

                    batch_size = 500
                    total_ingested = 0
                    for batch_start in range(0, len(data_rows), batch_size):
                        batch = data_rows[batch_start:batch_start + batch_size]
                        csv_lines = []
                        for row in batch:
                            if isinstance(row, list):
                                parts = []
                                for val in row:
                                    if val is None:
                                        parts.append("")
                                    elif isinstance(val, dict):
                                        parts.append(json.dumps(val).replace(",", ";"))
                                    else:
                                        parts.append(str(val).replace(",", ";"))
                                csv_lines.append(",".join(parts))
                        if csv_lines:
                            inline_data = "\n".join(csv_lines)
                            try:
                                _run_kql_command(s_uri, s_dbname,
                                    f".ingest inline into table {tname} <| {inline_data}"
                                )
                                total_ingested += len(csv_lines)
                            except Exception as ingest_err:
                                logger.warning(f"Batch ingest into {db_name}.{tname}: {ingest_err}")

                    # Update watermark on success
                    if new_watermark and total_ingested > 0:
                        _rti_watermarks[watermark_key] = new_watermark
                        _save_rti_watermarks()
                        logger.info(f"Updated watermark for {watermark_key}: {new_watermark}")

                    db_result["tables_synced"].append({
                        "table": tname,
                        "rows": total_ingested,
                        "mode": table_mode,
                        "watermark": new_watermark,
                    })
                    db_result["rows_copied"] += total_ingested
                    all_results["total_rows_copied"] += total_ingested
                    logger.info(f"Replicated {total_ingested} rows for {db_name}.{tname} ({table_mode})")

                except Exception as e:
                    logger.error(f"Failed to replicate table {db_name}.{tname}: {e}")
                    db_result["tables_failed"].append({"table": tname, "error": str(e)})

            all_results["databases_synced"].append(db_result)

        # For backward compat — flatten if single DB
        if len(db_pairs) == 1:
            single = all_results["databases_synced"][0] if all_results["databases_synced"] else {}
            return jsonify({
                "tables_synced": single.get("tables_synced", []),
                "tables_failed": single.get("tables_failed", []),
                "total_rows_copied": all_results["total_rows_copied"],
                "database": db_pairs[0][0],
            })

        return jsonify(all_results)
    except Exception as e:
        logger.exception("Error replicating KQL data")
        return jsonify({"error": str(e)}), 500


@app.route('/api/rti/connections', methods=['GET'])
def api_rti_connections_audit():
    """Audit connection strings in secondary RTI artifacts.
    Checks KQL Querysets and Eventstreams for stale primary references
    (workspace IDs, item IDs, queryServiceUri, ingestionServiceUri)."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    try:
        all_p = get_workspace_items(p_id)
        all_s = get_workspace_items(s_id)

        # Build ID mapping (primary→secondary by name)
        s_by_name = {i.get("displayName", ""): i for i in all_s}
        id_map = {p_id: s_id}
        for p_item in all_p:
            pname = p_item.get("displayName", "")
            pid = p_item.get("id", "")
            s_item = s_by_name.get(pname)
            if s_item and pid:
                sid = s_item.get("id", "")
                if sid and pid != sid:
                    id_map[pid] = sid

        # Build queryServiceUri / ingestionServiceUri maps
        p_kql_dbs = fabric_api("GET", f"/workspaces/{p_id}/kqlDatabases").get("value", [])
        s_kql_dbs = fabric_api("GET", f"/workspaces/{s_id}/kqlDatabases").get("value", [])
        s_kql_by_name = {db.get("displayName"): db for db in s_kql_dbs}

        uri_map = {}
        for p_db in p_kql_dbs:
            name = p_db.get("displayName", "")
            s_db = s_kql_by_name.get(name)
            if not s_db:
                continue
            p_props = p_db.get("properties", {})
            s_props = s_db.get("properties", {})
            for key in ["queryServiceUri", "ingestionServiceUri"]:
                p_val = p_props.get(key, "")
                s_val = s_props.get(key, "")
                if p_val and s_val and p_val != s_val:
                    uri_map[p_val] = s_val

        # Audit secondary artifacts for stale references
        audit = {"artifacts": [], "stale_references": 0, "clean": 0, "uri_map": uri_map, "id_map_size": len(id_map)}

        check_types = ["KQLQueryset", "Eventstream", "KQLDatabase"]
        for s_item in all_s:
            if s_item.get("type") not in check_types:
                continue

            item_report = {
                "name": s_item.get("displayName"),
                "type": s_item.get("type"),
                "id": s_item.get("id"),
                "stale_refs": [],
            }

            try:
                export_resp = fabric_api(
                    "POST", f"/workspaces/{s_id}/items/{s_item['id']}/getDefinition", timeout=60
                )
                definition = export_resp.get("definition", {})
                for part in definition.get("parts", []):
                    payload_b64 = part.get("payload", "")
                    if not payload_b64:
                        continue
                    try:
                        payload_text = base64.b64decode(payload_b64).decode("utf-8")
                        # Check for primary workspace ID
                        if p_id in payload_text:
                            item_report["stale_refs"].append({
                                "part": part.get("path", "?"),
                                "issue": "References primary workspace ID",
                                "value": p_id,
                            })
                        # Check for primary item IDs
                        for old_id, new_id in id_map.items():
                            if old_id == p_id:
                                continue  # Already checked
                            if old_id in payload_text:
                                item_report["stale_refs"].append({
                                    "part": part.get("path", "?"),
                                    "issue": f"References primary item ID ({old_id[:8]}...)",
                                    "value": old_id,
                                })
                        # Check for primary query/ingestion URIs
                        for old_uri, new_uri in uri_map.items():
                            if old_uri in payload_text:
                                item_report["stale_refs"].append({
                                    "part": part.get("path", "?"),
                                    "issue": "References primary cluster URI",
                                    "value": old_uri,
                                })
                    except Exception:
                        pass
            except Exception as e:
                item_report["stale_refs"].append({"part": "N/A", "issue": f"Could not export definition: {e}", "value": ""})

            if item_report["stale_refs"]:
                audit["stale_references"] += len(item_report["stale_refs"])
            else:
                audit["clean"] += 1
            audit["artifacts"].append(item_report)

        return jsonify(audit)
    except Exception as e:
        logger.exception("Error auditing RTI connections")
        return jsonify({"error": str(e)}), 500


@app.route('/api/rti/fix-connections', methods=['POST'])
def api_rti_fix_connections():
    """Fix stale connection strings in secondary RTI artifacts by re-exporting
    definitions, rewriting primary→secondary references, and updating."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id:
        return jsonify({"error": "Both workspaces must be configured"}), 400

    try:
        all_p = get_workspace_items(p_id)
        all_s = get_workspace_items(s_id)

        # Build full replacement map: item IDs + URIs
        s_by_name = {i.get("displayName", ""): i for i in all_s}
        replacements = {p_id: s_id}
        for p_item in all_p:
            pname = p_item.get("displayName", "")
            pid = p_item.get("id", "")
            s_item = s_by_name.get(pname)
            if s_item and pid:
                sid = s_item.get("id", "")
                if sid and pid != sid:
                    replacements[pid] = sid

        # Add URI replacements
        p_kql_dbs = fabric_api("GET", f"/workspaces/{p_id}/kqlDatabases").get("value", [])
        s_kql_dbs = fabric_api("GET", f"/workspaces/{s_id}/kqlDatabases").get("value", [])
        s_kql_by_name = {db.get("displayName"): db for db in s_kql_dbs}
        for p_db in p_kql_dbs:
            name = p_db.get("displayName", "")
            s_db = s_kql_by_name.get(name)
            if not s_db:
                continue
            p_props = p_db.get("properties", {})
            s_props = s_db.get("properties", {})
            for key in ["queryServiceUri", "ingestionServiceUri"]:
                p_val = p_props.get(key, "")
                s_val = s_props.get(key, "")
                if p_val and s_val and p_val != s_val:
                    replacements[p_val] = s_val
                    # Also map the cluster hostname portion (without path)
                    try:
                        from urllib.parse import urlparse
                        p_host = urlparse(p_val).netloc
                        s_host = urlparse(s_val).netloc
                        if p_host and s_host and p_host != s_host:
                            replacements[p_host] = s_host
                    except Exception:
                        pass

        results = {"fixed": [], "failed": [], "skipped": [], "total_replacements": len(replacements)}

        fix_types = ["KQLQueryset", "Eventstream"]
        for s_item in all_s:
            if s_item.get("type") not in fix_types:
                continue

            name = s_item.get("displayName", "")
            sid = s_item.get("id", "")
            try:
                # Export current secondary definition
                export_resp = fabric_api(
                    "POST", f"/workspaces/{s_id}/items/{sid}/getDefinition", timeout=60
                )
                definition = export_resp.get("definition", {})
                parts = definition.get("parts", [])
                if not parts:
                    results["skipped"].append({"name": name, "reason": "No definition parts"})
                    continue

                # Check if any rewrites are needed
                needs_rewrite = False
                for part in parts:
                    payload_b64 = part.get("payload", "")
                    if not payload_b64:
                        continue
                    try:
                        payload_text = base64.b64decode(payload_b64).decode("utf-8")
                        for old_val in replacements:
                            if old_val in payload_text:
                                needs_rewrite = True
                                break
                    except Exception:
                        pass
                    if needs_rewrite:
                        break

                if not needs_rewrite:
                    results["skipped"].append({"name": name, "reason": "No stale references found"})
                    continue

                # Rewrite and update
                rewritten = _rewrite_definition_parts(parts, replacements)
                update_def = dict(definition)
                update_def["parts"] = rewritten

                fabric_api(
                    "POST",
                    f"/workspaces/{s_id}/items/{sid}/updateDefinition",
                    payload={"definition": update_def},
                    timeout=120,
                )
                results["fixed"].append({"name": name, "type": s_item.get("type")})
                logger.info(f"Fixed connections for {s_item.get('type')} '{name}'")

            except Exception as e:
                logger.error(f"Failed to fix connections for '{name}': {e}")
                results["failed"].append({"name": name, "error": str(e)})

        return jsonify(results)
    except Exception as e:
        logger.exception("Error fixing RTI connections")
        return jsonify({"error": str(e)}), 500


@app.route('/api/rti/create-dummy', methods=['POST'])
def api_rti_create_dummy():
    """Create demo RTI artifacts in the primary workspace for testing."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    p_id = _ws_id("primary")
    if not p_id:
        return jsonify({"error": "Primary workspace not configured"}), 400

    token = _ensure_token()
    if not token:
        return jsonify({"error": "No valid token"}), 401

    DUMMY_ARTIFACTS = [
        {"displayName": "RTI_Demo_Eventhouse", "type": "Eventhouse",
         "description": "Demo Eventhouse for Resiliency & Recovery testing"},
        {"displayName": "RTI_Demo_KQLDatabase", "type": "KQLDatabase",
         "description": "Demo KQL Database for Resiliency & Recovery testing"},
        {"displayName": "RTI_Demo_KQLQueryset", "type": "KQLQueryset",
         "description": "Demo KQL Queryset for Resiliency & Recovery testing"},
        {"displayName": "RTI_Demo_Eventstream", "type": "Eventstream",
         "description": "Demo Eventstream for Resiliency & Recovery testing"},
    ]

    try:
        # Get existing items to skip duplicates
        existing_items = get_workspace_items(p_id)
        existing_names = {i.get("displayName") for i in existing_items}

        result = {"created": [], "skipped": [], "failed": [], "eventhouse_id": None}

        # Find existing Eventhouse ID if already present
        for i in existing_items:
            if i.get("displayName") == "RTI_Demo_Eventhouse" and i.get("type") == "Eventhouse":
                result["eventhouse_id"] = i.get("id")
                break

        for artifact in DUMMY_ARTIFACTS:
            name = artifact["displayName"]
            art_type = artifact["type"]

            if name in existing_names:
                result["skipped"].append(name)
                continue

            try:
                create_payload = {
                    "displayName": name,
                    "type": art_type,
                }
                if artifact.get("description"):
                    create_payload["description"] = artifact["description"]

                # KQL Database needs parent Eventhouse
                if art_type == "KQLDatabase" and result.get("eventhouse_id"):
                    create_payload["creationPayload"] = {
                        "databaseType": "ReadWrite",
                        "parentEventhouseItemId": result["eventhouse_id"],
                    }

                resp = fabric_api("POST", f"/workspaces/{p_id}/items",
                                  payload=create_payload, timeout=120)
                logger.info(f"RTI demo: created {art_type} '{name}'")
                result["created"].append(name)

                # Capture Eventhouse ID for KQL Database creation
                if art_type == "Eventhouse" and resp and isinstance(resp, dict):
                    result["eventhouse_id"] = resp.get("id")
                    if not result["eventhouse_id"]:
                        # Re-fetch to get the ID
                        import time as _time
                        _time.sleep(3)
                        fresh_items = fabric_api("GET", f"/workspaces/{p_id}/items?type=Eventhouse")
                        for fi in fresh_items.get("value", []):
                            if fi.get("displayName") == name:
                                result["eventhouse_id"] = fi.get("id")
                                break

                # Brief pause between creations
                import time as _time
                _time.sleep(2)

            except Exception as e:
                logger.error(f"RTI demo: failed to create {art_type} '{name}': {e}")
                result["failed"].append({"name": name, "error": str(e)})

        # Clear cache
        _cache.pop(f"items:{p_id}", None)
        _cache_ttl.pop(f"items:{p_id}", None)

        return jsonify(result)
    except Exception as e:
        logger.exception("Error creating dummy RTI artifacts")
        return jsonify({"error": str(e)}), 500


@app.route('/api/rti/cleanup-dummy', methods=['POST'])
def api_rti_cleanup_dummy():
    """Remove RTI_Demo_* artifacts from both workspaces."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401

    DUMMY_PREFIX = "RTI_Demo_"
    result = {"deleted": [], "failed": []}

    for role in ["primary", "secondary"]:
        ws_id = _ws_id(role)
        if not ws_id:
            continue
        try:
            items = get_workspace_items(ws_id)
            # Delete in reverse dependency order
            delete_order = ["KQLQueryset", "Eventstream", "KQLDatabase", "Eventhouse"]
            to_delete = [i for i in items if i.get("displayName", "").startswith(DUMMY_PREFIX)]
            to_delete.sort(key=lambda i: delete_order.index(i["type"]) if i["type"] in delete_order else 99)

            for item in to_delete:
                try:
                    fabric_api("DELETE", f"/workspaces/{ws_id}/items/{item['id']}", timeout=60)
                    result["deleted"].append(f"{role}:{item['displayName']}")
                    import time as _time
                    _time.sleep(1)
                except Exception as e:
                    result["failed"].append(f"{role}:{item['displayName']}: {e}")

            _cache.pop(f"items:{ws_id}", None)
            _cache_ttl.pop(f"items:{ws_id}", None)
        except Exception as e:
            result["failed"].append(f"{role}: listing failed: {e}")

    return jsonify(result)


# ============================================================================
# ERROR HANDLERS
# ============================================================================
# SCHEDULED RTI REPLICATION
# ============================================================================

_rti_schedule_state: Dict[str, Any] = {
    "enabled": False,
    "interval_minutes": 60,
    "timer": None,
    "last_run": None,
    "last_status": None,
    "run_count": 0,
    "total_rows_copied": 0,
}
_RTI_SCHEDULE_FILE = os.path.join(os.path.dirname(__file__), ".rti_schedule_state.json")

# Watermark state for incremental RTI replication
# Key: "db_name.table_name" → ISO datetime string of last synced row
_rti_watermarks: Dict[str, str] = {}
_RTI_WATERMARK_FILE = os.path.join(os.path.dirname(__file__), ".rti_watermarks.json")


def _load_rti_watermarks():
    global _rti_watermarks
    try:
        if os.path.exists(_RTI_WATERMARK_FILE):
            with open(_RTI_WATERMARK_FILE, "r") as f:
                _rti_watermarks = json.load(f)
    except Exception:
        _rti_watermarks = {}


def _save_rti_watermarks():
    try:
        with open(_RTI_WATERMARK_FILE, "w") as f:
            json.dump(_rti_watermarks, f, indent=2)
    except Exception:
        pass


def _detect_timestamp_column(schema_str: str) -> Optional[str]:
    """Detect the best datetime column for incremental replication.
    Prefers well-known names, falls back to first datetime column."""
    preferred = ["Timestamp", "EventTime", "IngestionTime", "CreatedAt",
                 "UpdatedAt", "ModifiedDate", "timestamp", "eventTime"]
    datetime_cols = []
    for part in schema_str.split(","):
        part = part.strip()
        if ":" in part:
            col_name, col_type = part.split(":", 1)
            col_name = col_name.strip()
            col_type = col_type.strip().lower()
            if col_type == "datetime":
                datetime_cols.append(col_name)
    if not datetime_cols:
        return None
    for name in preferred:
        if name in datetime_cols:
            return name
    return datetime_cols[0]


def _load_rti_schedule():
    try:
        if os.path.exists(_RTI_SCHEDULE_FILE):
            with open(_RTI_SCHEDULE_FILE, "r") as f:
                saved = json.load(f)
            _rti_schedule_state["interval_minutes"] = saved.get("interval_minutes", 60)
            if saved.get("enabled"):
                _start_rti_schedule(_rti_schedule_state["interval_minutes"])
    except Exception:
        pass


def _save_rti_schedule():
    try:
        with open(_RTI_SCHEDULE_FILE, "w") as f:
            json.dump({
                "enabled": _rti_schedule_state["enabled"],
                "interval_minutes": _rti_schedule_state["interval_minutes"],
            }, f)
    except Exception:
        pass


def _rti_schedule_tick():
    """Background timer — replicate KQL data from primary to secondary."""
    if not _rti_schedule_state["enabled"]:
        return
    _rti_schedule_state["run_count"] += 1
    _rti_schedule_state["last_run"] = datetime.now().isoformat()

    p_id = _ws_id("primary")
    s_id = _ws_id("secondary")
    if not p_id or not s_id or not is_authenticated():
        _rti_schedule_state["last_status"] = "Skipped — not ready"
        _schedule_next_rti_replication()
        return

    try:
        # Replicate ALL KQL database pairs using incremental mode
        p_dbs = fabric_api("GET", f"/workspaces/{p_id}/kqlDatabases").get("value", [])
        s_dbs = fabric_api("GET", f"/workspaces/{s_id}/kqlDatabases").get("value", [])
        s_db_by_name = {db.get("displayName"): db for db in s_dbs}

        db_pairs = []
        for p_db in p_dbs:
            name = p_db.get("displayName", "")
            s_db = s_db_by_name.get(name)
            if s_db:
                db_pairs.append((name, p_db, s_db))

        if not db_pairs:
            _rti_schedule_state["last_status"] = f"Run #{_rti_schedule_state['run_count']}: No matching DB pairs found"
            _schedule_next_rti_replication()
            return

        total_rows = 0
        tables_info = []

        for db_name, p_db, s_db in db_pairs:
            p_uri = p_db.get("properties", {}).get("queryServiceUri")
            s_uri = s_db.get("properties", {}).get("queryServiceUri")
            p_dbname = p_db.get("properties", {}).get("databaseName", db_name)
            s_dbname = s_db.get("properties", {}).get("databaseName", db_name)

            if not p_uri or not s_uri:
                tables_info.append(f"{db_name}:NO_URI")
                continue

            p_show = _run_kql_command(p_uri, p_dbname, ".show tables")
            p_tables = []
            for row in p_show.get("Tables", [{}])[0].get("Rows", []):
                tname = row[0] if isinstance(row, list) else row.get("TableName", "")
                if tname:
                    p_tables.append(tname)

            for tname in p_tables:
                try:
                    schema_resp = _run_kql_command(p_uri, p_dbname, f".show table {tname} cslschema")
                    schema_rows = schema_resp.get("Tables", [{}])[0].get("Rows", [])
                    if not schema_rows:
                        continue
                    schema_str = schema_rows[0][1] if isinstance(schema_rows[0], list) else schema_rows[0].get("Schema", "")

                    try:
                        _run_kql_command(s_uri, s_dbname, f".create-merge table {tname} ({schema_str})")
                    except Exception:
                        pass

                    # Incremental: use watermark if datetime column exists
                    watermark_key = f"{db_name}.{tname}"
                    ts_col = _detect_timestamp_column(schema_str)
                    table_mode = "incremental" if ts_col else "full"

                    if ts_col:
                        old_watermark = _rti_watermarks.get(watermark_key)
                        if old_watermark:
                            kql_query = f"{tname} | where {ts_col} > datetime({old_watermark}) | order by {ts_col} asc | take 10000"
                        else:
                            kql_query = f"{tname} | order by {ts_col} asc | take 10000"
                            try:
                                _run_kql_command(s_uri, s_dbname, f".clear table {tname} data")
                            except Exception:
                                pass
                    else:
                        try:
                            _run_kql_command(s_uri, s_dbname, f".clear table {tname} data")
                        except Exception:
                            pass
                        kql_query = f"{tname} | take 10000"

                    query_resp = _run_kql_query(p_uri, p_dbname, kql_query)
                    data_tables = query_resp.get("Tables", [])
                    if not data_tables:
                        continue
                    data_rows = data_tables[0].get("Rows", [])
                    columns = data_tables[0].get("Columns", [])
                    if not data_rows:
                        tables_info.append(f"{db_name}.{tname}:0({table_mode})")
                        continue

                    # Track new watermark
                    new_watermark = None
                    if ts_col and columns:
                        ts_idx = next((i for i, c in enumerate(columns) if c.get("ColumnName") == ts_col), None)
                        if ts_idx is not None:
                            last_row = data_rows[-1]
                            last_ts = last_row[ts_idx] if isinstance(last_row, list) else last_row.get(ts_col)
                            if last_ts:
                                new_watermark = str(last_ts)

                    batch_size = 500
                    rows_ingested = 0
                    for batch_start in range(0, len(data_rows), batch_size):
                        batch = data_rows[batch_start:batch_start + batch_size]
                        csv_lines = []
                        for row in batch:
                            if isinstance(row, list):
                                parts = []
                                for val in row:
                                    if val is None:
                                        parts.append("")
                                    elif isinstance(val, dict):
                                        parts.append(json.dumps(val).replace(",", ";"))
                                    else:
                                        parts.append(str(val).replace(",", ";"))
                                csv_lines.append(",".join(parts))
                        if csv_lines:
                            ingest_cmd = f".ingest inline into table {tname} <|\n" + "\n".join(csv_lines)
                            _run_kql_command(s_uri, s_dbname, ingest_cmd)
                            rows_ingested += len(csv_lines)

                    if new_watermark and rows_ingested > 0:
                        _rti_watermarks[watermark_key] = new_watermark
                        _save_rti_watermarks()

                    total_rows += rows_ingested
                    tables_info.append(f"{db_name}.{tname}:{rows_ingested}({table_mode})")
                except Exception as e:
                    tables_info.append(f"{db_name}.{tname}:FAIL({e})")

        _rti_schedule_state["total_rows_copied"] += total_rows
        _rti_schedule_state["last_status"] = (
            f"Run #{_rti_schedule_state['run_count']}: "
            f"{total_rows} rows copied ({', '.join(tables_info)})"
        )
        logger.info(f"RTI scheduled replication: {total_rows} rows copied")

    except Exception as e:
        _rti_schedule_state["last_status"] = f"Error: {e}"
        logger.exception("RTI scheduled replication error")

    _schedule_next_rti_replication()


def _schedule_next_rti_replication():
    if _rti_schedule_state["enabled"]:
        interval = _rti_schedule_state["interval_minutes"] * 60
        t = threading.Timer(interval, _rti_schedule_tick)
        t.daemon = True
        t.start()
        _rti_schedule_state["timer"] = t


def _start_rti_schedule(interval_minutes: int = 60):
    _stop_rti_schedule()
    _rti_schedule_state["enabled"] = True
    _rti_schedule_state["interval_minutes"] = interval_minutes
    _save_rti_schedule()
    t = threading.Timer(interval_minutes * 60, _rti_schedule_tick)
    t.daemon = True
    t.start()
    _rti_schedule_state["timer"] = t
    logger.info(f"RTI scheduled replication started: every {interval_minutes} min")


def _stop_rti_schedule():
    _rti_schedule_state["enabled"] = False
    if _rti_schedule_state["timer"]:
        _rti_schedule_state["timer"].cancel()
        _rti_schedule_state["timer"] = None
    _save_rti_schedule()


@app.route('/api/rti/schedule', methods=['GET'])
def api_rti_schedule_get():
    return jsonify({
        "enabled": _rti_schedule_state["enabled"],
        "interval_minutes": _rti_schedule_state["interval_minutes"],
        "last_run": _rti_schedule_state["last_run"],
        "last_status": _rti_schedule_state["last_status"],
        "run_count": _rti_schedule_state["run_count"],
        "total_rows_copied": _rti_schedule_state["total_rows_copied"],
    })


@app.route('/api/rti/schedule', methods=['POST'])
def api_rti_schedule_set():
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json() or {}
    enabled = data.get("enabled", False)
    interval = max(15, min(1440, int(data.get("interval_minutes", 60))))
    if enabled:
        _start_rti_schedule(interval)
        return jsonify({"status": "ok", "message": f"RTI replication scheduled: every {interval} min (incremental)"})
    else:
        _stop_rti_schedule()
        return jsonify({"status": "ok", "message": "RTI scheduled replication disabled"})


@app.route('/api/rti/watermarks', methods=['GET'])
def api_rti_watermarks_get():
    """Get current watermark state for all KQL tables."""
    return jsonify({"watermarks": _rti_watermarks})


@app.route('/api/rti/watermarks', methods=['DELETE'])
def api_rti_watermarks_reset():
    """Reset watermarks — next replication will do a full initial load."""
    if not is_authenticated():
        return jsonify({"error": "Not authenticated"}), 401
    data = request.get_json(silent=True) or {}
    table_key = data.get("key")  # Optional: reset specific table e.g. "MyDB.SensorReadings"
    if table_key:
        removed = _rti_watermarks.pop(table_key, None)
        _save_rti_watermarks()
        return jsonify({"status": "ok", "message": f"Watermark reset for {table_key}", "was": removed})
    else:
        count = len(_rti_watermarks)
        _rti_watermarks.clear()
        _save_rti_watermarks()
        return jsonify({"status": "ok", "message": f"All watermarks reset ({count} cleared)"})


# ============================================================================

@app.errorhandler(404)
def not_found(error):
    return render_template('error.html', error='Page not found'), 404


@app.errorhandler(500)
def internal_error(error):
    return render_template('error.html', error='Internal server error'), 500


if __name__ == '__main__':
    _load_schedule()
    _load_azcopy_state()
    _load_azcopy_schedule()
    _load_autosync()
    _load_defcheck()
    _load_rti_schedule()
    _load_rti_watermarks()
    _load_devops_config()
    # Auto-restore Service Principal session if config exists
    if _sp_config.get("client_id") and _sp_config.get("client_secret") and _sp_config.get("tenant_id"):
        logger.info("Found saved Service Principal config — attempting auto-login...")
        _do_sp_login(_sp_config["tenant_id"], _sp_config["client_id"], _sp_config["client_secret"])
    logger.info("Starting Fabric Resiliency & Recovery Dashboard...")
    logger.info("Access dashboard at: http://localhost:5000")
    if _auth_state.get("auth_mode") == "service_principal":
        logger.info("Authenticated via Service Principal.")
    else:
        logger.info("You will be prompted to sign in with your Microsoft account.")
    app.run(debug=True, host='0.0.0.0', port=5000)

