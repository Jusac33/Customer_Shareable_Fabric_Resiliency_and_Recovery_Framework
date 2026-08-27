"""
Microsoft Fabric Resiliency & Recovery Shared Configuration & Utilities Module

Provides centralized authentication, configuration, and helper functions
for all Fabric DR synchronization scripts.

Requirements: msal, requests, pandas
"""

import os
import json
import logging
import time
import base64
from typing import Dict, List, Optional, Any
from datetime import datetime
import msal
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
import sys

# ============================================================================
# CONFIGURATION - Update these for your environment
# ============================================================================

# Primary Fabric Workspace Configuration
PRIMARY_WORKSPACE_ID = os.getenv("PRIMARY_WORKSPACE_ID", "your-primary-workspace-guid")
PRIMARY_CAPACITY_ID = os.getenv("PRIMARY_CAPACITY_ID", "your-primary-capacity-id")

# Secondary Fabric Workspace Configuration
SECONDARY_WORKSPACE_ID = os.getenv("SECONDARY_WORKSPACE_ID", "your-secondary-workspace-guid")
SECONDARY_CAPACITY_ID = os.getenv("SECONDARY_CAPACITY_ID", "your-secondary-capacity-id")

# Azure AD Service Principal Credentials
TENANT_ID = os.getenv("TENANT_ID", "your-tenant-id")
CLIENT_ID = os.getenv("CLIENT_ID", "your-service-principal-client-id")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "your-service-principal-secret")

# Entra object (principal) ID of the service principal above.
# This is the enterprise-application / service-principal objectId, NOT the
# application (client) ID.  Fabric roleAssignment APIs identify principals by
# object ID, so connection role grants require this value.
# Entra portal: Enterprise applications -> <your app> -> Object ID
SERVICE_PRINCIPAL_OBJECT_ID = os.getenv("SERVICE_PRINCIPAL_OBJECT_ID", "")

# OneLake URLs
PRIMARY_ONELAKE_URL = f"https://onelake.dfs.fabric.microsoft.com/{PRIMARY_WORKSPACE_ID}"
SECONDARY_ONELAKE_URL = f"https://onelake.dfs.fabric.microsoft.com/{SECONDARY_WORKSPACE_ID}"

# Fabric API Configuration
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
FABRIC_API_SCOPE = "https://api.fabric.microsoft.com/.default"

# Power BI API Configuration
# A small number of operations (notably semantic model ownership takeover) are
# only available on the Power BI REST surface, which needs its own audience.
POWERBI_API_BASE = "https://api.powerbi.com/v1.0/myorg"
POWERBI_API_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

# Artifact Types to Support
ARTIFACT_TYPES_TO_SYNC = [
    "Lakehouse",
    "Warehouse",
    "Notebook",
    "DataPipeline",
    "SemanticModel",
    "Report",
    "DataflowsGen2",
    "KQLDatabase",
    "KQLQueryset",
    "Eventstream",
    "MLModel",
    "SparkJobDefinition",
    "Environment",
]

# Performance & Resilience Settings
NUM_THREADS = int(os.getenv("NUM_THREADS", "5"))
RESPONSE_BACKOFF = int(os.getenv("RESPONSE_BACKOFF", "2"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
OPERATION_TIMEOUT_SECONDS = int(os.getenv("OPERATION_TIMEOUT_SECONDS", "300"))

# Feature Flags
GIT_INTEGRATION_ENABLED = os.getenv("GIT_INTEGRATION_ENABLED", "False").lower() == "true"
DRY_RUN = False  # Will be set by individual scripts


# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logger(script_name: str) -> logging.Logger:
    """
    Configure logging for a script with both console and file handlers.
    
    Args:
        script_name: Name of the script (used in log filename)
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(script_name)
    logger.setLevel(logging.INFO)
    
    # Ensure logs directory exists
    os.makedirs("logs", exist_ok=True)
    
    # File handler with timestamp
    log_filename = f"logs/{script_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    fh = logging.FileHandler(log_filename)
    fh.setLevel(logging.INFO)
    
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    
    # Formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    
    # Avoid duplicate handlers
    if not logger.handlers:
        logger.addHandler(fh)
        logger.addHandler(ch)
    
    return logger


logger = setup_logger("common")


# ============================================================================
# AUTHENTICATION & TOKEN MANAGEMENT
# ============================================================================

class FabricAuthenticator:
    """Handles MSAL authentication for Fabric and Power BI APIs"""
    
    def __init__(self, tenant_id: str, client_id: str, client_secret: str):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_cache = {}
        self.token_expiry = {}
    
    def get_token(self, force_refresh: bool = False, scope: Optional[str] = None) -> str:
        """
        Acquire and cache access token using MSAL.
        
        Tokens are cached per scope, so a Fabric token and a Power BI token can
        be held at the same time without evicting each other.
        
        Args:
            force_refresh: Force token refresh even if cached
            scope: OAuth scope to request. Defaults to FABRIC_API_SCOPE.
            
        Returns:
            Bearer token string
            
        Raises:
            Exception: If token acquisition fails
        """
        scope = scope or FABRIC_API_SCOPE
        token_key = f"token::{scope}"
        
        # Return cached token if valid
        if not force_refresh and token_key in self.token_cache:
            if time.time() < self.token_expiry.get(token_key, 0):
                return self.token_cache[token_key]
        
        try:
            app = msal.ConfidentialClientApplication(
                self.client_id,
                authority=f"https://login.microsoftonline.com/{self.tenant_id}",
                client_credential=self.client_secret,
            )
            
            token_response = app.acquire_token_for_client(scopes=[scope])
            
            if "access_token" not in token_response:
                error_msg = token_response.get("error_description", "Unknown error")
                raise Exception(f"Failed to acquire token: {error_msg}")
            
            # Cache token with 5-minute buffer before actual expiry
            self.token_cache[token_key] = token_response["access_token"]
            self.token_expiry[token_key] = time.time() + (token_response.get("expires_in", 3600) - 300)
            
            return token_response["access_token"]
        
        except Exception as e:
            logger.error(f"Token acquisition error: {str(e)}")
            raise


# Global authenticator instance
_authenticator = None


def get_authenticator() -> FabricAuthenticator:
    """Get or create global authenticator instance"""
    global _authenticator
    if _authenticator is None:
        _authenticator = FabricAuthenticator(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
    return _authenticator


def get_token(scope: Optional[str] = None) -> str:
    """Get valid bearer token for the requested scope (defaults to Fabric API)"""
    return get_authenticator().get_token(scope=scope)


def get_powerbi_token() -> str:
    """Get valid Power BI API bearer token"""
    return get_authenticator().get_token(scope=POWERBI_API_SCOPE)


def get_headers() -> Dict[str, str]:
    """Get standard HTTP headers with auth token"""
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
    }


# ============================================================================
# FABRIC & POWER BI API REST OPERATIONS
# ============================================================================

def get_powerbi_headers() -> Dict[str, str]:
    """Get standard HTTP headers with a Power BI API auth token"""
    return {
        "Authorization": "Bearer " + get_powerbi_token(),
        "Content-Type": "application/json",
    }


class FabricApiError(Exception):
    """
    HTTP error raised by api_call() / powerbi_api_call().

    Subclasses Exception and renders the exact same message as before, so
    callers that catch bare Exception or match on str(e) are unaffected.  The
    status code is exposed separately so callers can tell an expected response
    (e.g. 404 from an endpoint an item type does not implement) apart from a
    genuine failure.
    """

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")


def _execute_rest_call(
    method: str,
    base_url: str,
    endpoint: str,
    payload: Optional[Dict],
    retry: int,
    timeout: int,
    headers_provider,
    retry_fn,
) -> Dict[str, Any]:
    """
    Shared REST executor used by api_call() and powerbi_api_call().

    Handles 202 long-running-operation polling, 429/503 retry with exponential
    backoff, 204/empty bodies, and error surfacing.

    Args:
        method: HTTP method (GET, POST, PUT, PATCH, DELETE)
        base_url: API base URL the endpoint is appended to
        endpoint: API endpoint path (without base URL)
        payload: JSON payload for POST/PUT/PATCH
        retry: Internal retry counter
        timeout: Request timeout in seconds
        headers_provider: Callable returning auth headers for this API
        retry_fn: Callable(method, endpoint, payload, retry, timeout) used to retry

    Returns:
        Response JSON payload or operation result

    Raises:
        Exception: If request fails after retries
    """
    url = f"{base_url}{endpoint}"

    try:
        headers = headers_provider()

        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=timeout)
        elif method.upper() == "POST":
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        elif method.upper() == "PUT":
            response = requests.put(url, json=payload, headers=headers, timeout=timeout)
        elif method.upper() == "PATCH":
            response = requests.patch(url, json=payload, headers=headers, timeout=timeout)
        elif method.upper() == "DELETE":
            response = requests.delete(url, headers=headers, timeout=timeout)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        # Handle 202 Accepted - long-running operation
        if response.status_code == 202:
            operation_url = response.headers.get("Operation-Location")
            return poll_long_running_operation(operation_url, headers)

        # Handle 429 Too Many Requests
        if response.status_code == 429:
            if retry < MAX_RETRIES:
                wait_time = RESPONSE_BACKOFF * (2 ** retry)  # Exponential backoff
                logger.warning(f"Rate limited. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                return retry_fn(method, endpoint, payload, retry + 1, timeout)
            else:
                raise Exception("Max retries exceeded for rate limit")

        # Handle 503 Service Unavailable
        if response.status_code == 503:
            if retry < MAX_RETRIES:
                wait_time = RESPONSE_BACKOFF * (2 ** retry)
                logger.warning(f"Service unavailable. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                return retry_fn(method, endpoint, payload, retry + 1, timeout)
            else:
                raise Exception("Service unavailable after retries")

        # Handle errors
        if response.status_code >= 400:
            error_detail = response.text
            try:
                error_detail = response.json()
            except:
                pass
            raise FabricApiError(response.status_code, error_detail)

        # Handle 204 No Content
        if response.status_code == 204:
            return {}

        # Some action endpoints (e.g. semantic model takeover) answer 200 with
        # an empty body — treat that as success rather than a decode failure.
        if not (response.content or b"").strip():
            return {}

        return response.json()

    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {str(e)}")
        raise


def api_call(
    method: str,
    endpoint: str,
    payload: Optional[Dict] = None,
    retry: int = 0,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Execute Fabric API REST call with exponential backoff and 202 polling.
    
    Args:
        method: HTTP method (GET, POST, PATCH, DELETE)
        endpoint: API endpoint path (without base URL)
        payload: JSON payload for POST/PATCH
        retry: Internal retry counter
        timeout: Request timeout in seconds
        
    Returns:
        Response JSON payload or operation result
        
    Raises:
        Exception: If request fails after retries
    """
    return _execute_rest_call(
        method,
        FABRIC_API_BASE,
        endpoint,
        payload,
        retry,
        timeout,
        get_headers,
        api_call,
    )


def powerbi_api_call(
    method: str,
    endpoint: str,
    payload: Optional[Dict] = None,
    retry: int = 0,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Execute Power BI API REST call with exponential backoff and 202 polling.

    Mirrors api_call() semantics but targets POWERBI_API_BASE with a Power BI
    audience token.  Needed for operations that have no Fabric API equivalent,
    such as semantic model ownership takeover.

    Args:
        method: HTTP method (GET, POST, PATCH, DELETE)
        endpoint: API endpoint path relative to POWERBI_API_BASE
        payload: JSON payload for POST/PATCH
        retry: Internal retry counter
        timeout: Request timeout in seconds

    Returns:
        Response JSON payload ({} when the response has no body)

    Raises:
        Exception: If request fails after retries
    """
    return _execute_rest_call(
        method,
        POWERBI_API_BASE,
        endpoint,
        payload,
        retry,
        timeout,
        get_powerbi_headers,
        powerbi_api_call,
    )


def poll_long_running_operation(
    operation_url: str, 
    headers: Dict[str, str],
    max_wait_seconds: int = OPERATION_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """
    Poll a long-running Fabric API operation until completion.
    
    Args:
        operation_url: URL from Operation-Location header
        headers: HTTP headers with auth
        max_wait_seconds: Maximum time to poll
        
    Returns:
        Final operation result
        
    Raises:
        Exception: If operation fails or times out
    """
    start_time = time.time()
    poll_interval = RESPONSE_BACKOFF
    
    while time.time() - start_time < max_wait_seconds:
        try:
            response = requests.get(operation_url, headers=headers, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                status = data.get("status", "")
                
                if status == "Completed":
                    return data.get("result", {})
                elif status in ["Failed", "Cancelled"]:
                    error = data.get("error", {})
                    raise Exception(f"Operation {status}: {error}")
                else:
                    # Still in progress
                    logger.debug(f"Operation status: {status}. Polling again...")
                    time.sleep(poll_interval)
            else:
                raise Exception(f"Poll request failed: {response.status_code}")
        
        except Exception as e:
            if time.time() - start_time > max_wait_seconds:
                raise Exception(f"Operation polling timeout: {str(e)}")
            time.sleep(poll_interval)
    
    raise Exception(f"Operation timeout after {max_wait_seconds} seconds")


# ============================================================================
# WORKSPACE & ITEM OPERATIONS
# ============================================================================

def get_items(
    workspace_id: str,
    item_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Get all items in a workspace, optionally filtered by type.
    
    Args:
        workspace_id: Fabric workspace GUID
        item_type: Optional filter (e.g., "Lakehouse", "Warehouse")
        
    Returns:
        List of item metadata dicts
    """
    items = []
    continuation_token = None
    
    while True:
        endpoint = f"/workspaces/{workspace_id}/items"
        
        # Add type filter if specified
        if item_type:
            endpoint += f"?type={item_type}"
        
        # Add continuation token if present
        if continuation_token:
            sep = "&" if item_type else "?"
            endpoint += f"{sep}continuationToken={continuation_token}"
        
        try:
            response = api_call("GET", endpoint)
            
            items.extend(response.get("value", []))
            
            # Check for pagination
            continuation_token = response.get("continuationToken")
            if not continuation_token:
                break
        
        except Exception as e:
            logger.error(f"Error fetching items from workspace {workspace_id}: {str(e)}")
            break
    
    return items


def export_item_definition(workspace_id: str, item_id: str) -> Dict[str, Any]:
    """
    Export the full definition of an item.
    
    Args:
        workspace_id: Fabric workspace GUID
        item_id: Item GUID
        
    Returns:
        Item definition dict
    """
    endpoint = f"/workspaces/{workspace_id}/items/{item_id}/getDefinition"
    return api_call("POST", endpoint)


def import_item(
    workspace_id: str,
    display_name: str,
    item_type: str,
    definition: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Import/create a new item in a workspace.
    
    Args:
        workspace_id: Target workspace GUID
        display_name: Display name for the item
        item_type: Item type (e.g., "Lakehouse")
        definition: Item definition payload
        
    Returns:
        Created item metadata
    """
    endpoint = f"/workspaces/{workspace_id}/items"
    payload = {
        "displayName": display_name,
        "type": item_type,
    }
    if definition:
        payload["definition"] = definition
    return api_call("POST", endpoint, payload)


def update_item_definition(
    workspace_id: str,
    item_id: str,
    definition: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Update an existing item's definition.
    
    Args:
        workspace_id: Workspace GUID
        item_id: Item GUID
        definition: Updated definition payload
        
    Returns:
        Updated item metadata
    """
    endpoint = f"/workspaces/{workspace_id}/items/{item_id}/updateDefinition"
    payload = {"definition": definition}
    return api_call("POST", endpoint, payload)


def get_item_permissions(workspace_id: str, item_id: str) -> List[Dict[str, Any]]:
    """
    Get permissions for an item.
    
    Args:
        workspace_id: Workspace GUID
        item_id: Item GUID
        
    Returns:
        List of permission assignments
    """
    endpoint = f"/workspaces/{workspace_id}/items/{item_id}/permissions"
    response = api_call("GET", endpoint)
    return response.get("value", [])


def set_item_permissions(
    workspace_id: str,
    item_id: str,
    principal_id: str,
    principal_type: str,
    role: str,
) -> Dict[str, Any]:
    """
    Set permission for an item.
    
    Args:
        workspace_id: Workspace GUID
        item_id: Item GUID
        principal_id: AAD principal ID (user or group)
        principal_type: "User" or "Group"
        role: "Admin", "Member", "Contributor", or "Viewer"
        
    Returns:
        Permission assignment result
    """
    endpoint = f"/workspaces/{workspace_id}/items/{item_id}/permissions"
    payload = {
        "principal": {"id": principal_id, "type": principal_type},
        "role": role,
    }
    return api_call("POST", endpoint, payload)


# ============================================================================
# MAPPING & REFERENCE DATA HELPERS
# ============================================================================

def load_artifact_mapping(filepath: str = "data/artifact_mapping.csv") -> Dict[str, str]:
    """
    Load primary → secondary artifact ID mapping.
    
    Args:
        filepath: Path to artifact_mapping.csv
        
    Returns:
        Dict mapping primary IDs to secondary IDs
    """
    try:
        if not os.path.exists(filepath):
            logger.warning(f"Artifact mapping file not found: {filepath}")
            return {}
        
        df = pd.read_csv(filepath)
        return dict(zip(df["primary_artifact_id"], df["secondary_artifact_id"]))
    except Exception as e:
        logger.error(f"Error loading artifact mapping: {str(e)}")
        return {}


def load_connection_mapping(filepath: str = "data/connection_mapping.csv") -> Dict[str, str]:
    """
    Load primary → secondary connection name mapping.
    
    Args:
        filepath: Path to connection_mapping.csv
        
    Returns:
        Dict mapping primary connection names to secondary
    """
    try:
        if not os.path.exists(filepath):
            logger.warning(f"Connection mapping file not found: {filepath}")
            return {}
        
        df = pd.read_csv(filepath)
        return dict(zip(df["primary_connection_name"], df["secondary_connection_name"]))
    except Exception as e:
        logger.error(f"Error loading connection mapping: {str(e)}")
        return {}


def load_reference_mapping(filepath: str = "data/reference_mapping.csv") -> Dict[str, str]:
    """
    Load workspace/artifact ID reference mapping (for path/URL rewriting).
    
    Args:
        filepath: Path to reference_mapping.csv
        
    Returns:
        Dict mapping primary references to secondary
    """
    try:
        if not os.path.exists(filepath):
            logger.warning(f"Reference mapping file not found: {filepath}")
            return {}
        
        df = pd.read_csv(filepath)
        return dict(zip(df["primary_reference"], df["secondary_reference"]))
    except Exception as e:
        logger.error(f"Error loading reference mapping: {str(e)}")
        return {}


def save_mapping(data: List[Dict], filepath: str, columns: List[str]):
    """
    Save mapping data to CSV file.
    
    Args:
        data: List of mapping dicts
        filepath: Output filepath
        columns: Column names for CSV
    """
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    df = pd.DataFrame(data, columns=columns)
    df.to_csv(filepath, index=False)
    logger.info(f"Saved {len(data)} records to {filepath}")


def save_json(data: Any, filepath: str):
    """
    Save data as JSON file.
    
    Args:
        data: Data to serialize
        filepath: Output filepath
    """
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info(f"Saved JSON to {filepath}")


# ============================================================================
# DEFINITION REMAPPING (BASE64-AWARE)
# ============================================================================

def remap_definition(
    definition: Dict[str, Any],
    mapping: Dict[str, str],
) -> Dict[str, Any]:
    """
    Remap all primary references to secondary in an item definition.
    
    Handles base64-encoded parts by decoding, replacing, and re-encoding.
    Also replaces any references in the top-level JSON structure.
    
    Args:
        definition: Item definition as returned by export_item_definition()
        mapping: Dict mapping primary IDs/refs to secondary IDs/refs
        
    Returns:
        Definition with all references remapped
    """
    if not mapping:
        return definition

    # Deep copy to avoid mutating the original
    import copy
    result = copy.deepcopy(definition)

    # Remap inside base64-encoded parts, stripping .platform parts
    parts = result.get("definition", {}).get("parts", [])
    filtered_parts = []
    for part in parts:
        # Strip .platform parts — they contain source-item identity and
        # must not be sent when creating a new item in a different workspace
        part_path = part.get("path", "")
        if part_path == ".platform" or part_path.endswith("/.platform"):
            logger.info(f"Stripping .platform part from definition")
            continue

        payload = part.get("payload", "")
        payload_type = part.get("payloadType", "")

        if payload_type == "InlineBase64" and payload:
            try:
                decoded = base64.b64decode(payload).decode("utf-8")
                for primary_ref, secondary_ref in mapping.items():
                    decoded = decoded.replace(primary_ref, secondary_ref)
                part["payload"] = base64.b64encode(decoded.encode("utf-8")).decode("utf-8")
            except Exception as e:
                logger.warning(f"Could not remap part '{part.get('path', '?')}': {e}")

        filtered_parts.append(part)

    # Update parts with filtered list (minus .platform)
    if "definition" in result and "parts" in result["definition"]:
        result["definition"]["parts"] = filtered_parts

    # Also remap any references in the top-level JSON (non-base64 metadata)
    result_str = json.dumps(result)
    for primary_ref, secondary_ref in mapping.items():
        result_str = result_str.replace(primary_ref, secondary_ref)
    result = json.loads(result_str)

    return result


def build_combined_mapping() -> Dict[str, str]:
    """
    Build a combined mapping dict from all mapping CSV files.
    Merges reference_mapping, artifact_mapping, and connection_mapping.
    
    Returns:
        Combined dict of primary→secondary ID/ref mappings
    """
    combined = {}
    combined.update(load_reference_mapping())
    combined.update(load_artifact_mapping())
    combined.update(load_connection_mapping())
    return combined


# ============================================================================
# PARALLEL EXECUTION HELPER
# ============================================================================

def execute_parallel(
    func,
    items: List[Any],
    max_workers: int = NUM_THREADS,
) -> List[Any]:
    """
    Execute a function in parallel over a list of items.
    
    Args:
        func: Function to execute (takes one item as argument)
        items: List of items to process
        max_workers: Max concurrent threads
        
    Returns:
        List of results in original order
    """
    results = [None] * len(items)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(func, item): i for i, item in enumerate(items)}
        
        for future in futures:
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                logger.error(f"Parallel execution error at index {idx}: {str(e)}")
                results[idx] = None
    
    return results


# ============================================================================
# VALIDATION & UTILITY FUNCTIONS
# ============================================================================

def validate_credentials() -> bool:
    """
    Validate that configured credentials are valid.
    
    Returns:
        True if valid, False otherwise
    """
    try:
        token = get_token()
        logger.info("Credentials validated successfully")
        return True
    except Exception as e:
        logger.error(f"Credential validation failed: {str(e)}")
        return False


def validate_workspace_access(workspace_id: str) -> bool:
    """
    Validate access to a workspace.
    
    Args:
        workspace_id: Workspace GUID to test
        
    Returns:
        True if accessible, False otherwise
    """
    try:
        api_call("GET", f"/workspaces/{workspace_id}")
        return True
    except Exception as e:
        logger.error(f"Cannot access workspace {workspace_id}: {str(e)}")
        return False


def get_workspace_info(workspace_id: str) -> Dict[str, Any]:
    """
    Get workspace metadata.
    
    Args:
        workspace_id: Workspace GUID
        
    Returns:
        Workspace metadata dict
    """
    return api_call("GET", f"/workspaces/{workspace_id}")


# ============================================================================
# BULK ITEM DEFINITION APIs  (beta — Fabric Items API)
# ============================================================================
# POST /workspaces/{ws}/items/bulkExportItemDefinitions  → LRO
# POST /workspaces/{ws}/items/bulkImportItemDefinitions  → LRO
# These reduce N single-item getDefinition / updateDefinition calls to 1 call.
# ============================================================================

def bulk_export_definitions(
    workspace_id: str,
    item_ids: Optional[List[str]] = None,
    item_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Bulk export item definitions from a workspace (beta API).

    Args:
        workspace_id: Workspace GUID
        item_ids:     Optional list of item IDs to export (all if None)
        item_types:   Optional list of item types to filter

    Returns:
        Dict with 'itemDefinitions' — list of {id, displayName, type, definition}

    Raises:
        Exception on any API error (caller should fall back to per-item)
    """
    payload: Dict[str, Any] = {}
    if item_ids:
        payload["itemIds"] = item_ids
    if item_types:
        payload["itemTypes"] = item_types

    return api_call(
        "POST",
        f"/workspaces/{workspace_id}/items/bulkExportItemDefinitions",
        payload=payload or None,
        timeout=300,
    )


def bulk_import_definitions(
    workspace_id: str,
    item_definitions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Bulk import / update item definitions into a workspace (beta API).

    Args:
        workspace_id:     Workspace GUID
        item_definitions: List of objects, each with:
            displayName, type, definition {parts: [...]}
            Optionally 'id' when updating an existing item.

    Returns:
        Import result dict from the LRO

    Raises:
        Exception on any API error (caller should fall back to per-item)
    """
    return api_call(
        "POST",
        f"/workspaces/{workspace_id}/items/bulkImportItemDefinitions",
        payload={"itemDefinitions": item_definitions},
        timeout=600,
    )


if __name__ == "__main__":
    # Quick validation
    print("Validating Fabric Resiliency & Recovery configuration...")
    print(f"Primary Workspace: {PRIMARY_WORKSPACE_ID}")
    print(f"Secondary Workspace: {SECONDARY_WORKSPACE_ID}")
    print(f"API Base: {FABRIC_API_BASE}")
    
    if validate_credentials():
        print("✓ Authentication successful")
    else:
        print("✗ Authentication failed")
    
    if validate_workspace_access(PRIMARY_WORKSPACE_ID):
        print(f"✓ Access to primary workspace confirmed")
    else:
        print(f"✗ No access to primary workspace")
    
    if validate_workspace_access(SECONDARY_WORKSPACE_ID):
        print(f"✓ Access to secondary workspace confirmed")
    else:
        print(f"✗ No access to secondary workspace")

