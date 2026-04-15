"""
RTI Dummy Data Generator

Creates sample RTI artifacts (Eventhouse, KQL Database, KQL Queryset, Eventstream)
in the PRIMARY workspace for testing Resiliency & Recovery sync flow.

Usage:
  python rti/create_dummy_rti.py
  python rti/create_dummy_rti.py --workspace-id <guid>
  python rti/create_dummy_rti.py --cleanup    # Remove dummy items

Requires: Authenticated session (run from the dashboard or set env vars)
"""

import argparse
import json
import sys
import os
import time
from typing import Dict, List, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- Dummy artifact definitions ---

DUMMY_ARTIFACTS = [
    {
        "displayName": "RTI_Demo_Eventhouse",
        "type": "Eventhouse",
        "description": "Demo Eventhouse for Resiliency & Recovery testing",
    },
    {
        "displayName": "RTI_Demo_KQLDatabase",
        "type": "KQLDatabase",
        "description": "Demo KQL Database for Resiliency & Recovery testing",
        # KQL Database creation requires a parent Eventhouse
        # The API auto-provisions one if creationPayload is supplied
        "creationPayload": {
            "databaseType": "ReadWrite",
            "parentEventhouseItemId": None,  # Will be filled after Eventhouse creation
        },
    },
    {
        "displayName": "RTI_Demo_KQLQueryset",
        "type": "KQLQueryset",
        "description": "Demo KQL Queryset for Resiliency & Recovery testing",
    },
    {
        "displayName": "RTI_Demo_Eventstream",
        "type": "Eventstream",
        "description": "Demo Eventstream for Resiliency & Recovery testing",
    },
]

DUMMY_PREFIX = "RTI_Demo_"


def create_dummy_rti_artifacts(workspace_id: str, token: str) -> Dict[str, Any]:
    """Create dummy RTI artifacts in the given workspace via Fabric REST API.

    Returns a result dict with created/failed/skipped lists.
    """
    import requests

    FABRIC_API = "https://api.fabric.microsoft.com/v1"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    result = {"created": [], "failed": [], "skipped": [], "eventhouse_id": None}

    # Get existing items to avoid duplicates
    resp = requests.get(f"{FABRIC_API}/workspaces/{workspace_id}/items", headers=headers, timeout=30)
    resp.raise_for_status()
    existing = {i["displayName"]: i for i in resp.json().get("value", [])}

    for artifact in DUMMY_ARTIFACTS:
        name = artifact["displayName"]
        art_type = artifact["type"]

        if name in existing:
            print(f"  ⏭ {art_type} '{name}' already exists — skipping")
            result["skipped"].append(name)
            if art_type == "Eventhouse":
                result["eventhouse_id"] = existing[name]["id"]
            continue

        payload: Dict[str, Any] = {
            "displayName": name,
            "type": art_type,
        }
        if artifact.get("description"):
            payload["description"] = artifact["description"]

        # KQL Database needs creationPayload with parent Eventhouse
        if art_type == "KQLDatabase" and result.get("eventhouse_id"):
            payload["creationPayload"] = {
                "databaseType": "ReadWrite",
                "parentEventhouseItemId": result["eventhouse_id"],
            }

        try:
            print(f"  Creating {art_type}: {name} ...")
            create_resp = requests.post(
                f"{FABRIC_API}/workspaces/{workspace_id}/items",
                headers=headers,
                json=payload,
                timeout=120,
            )

            if create_resp.status_code == 201:
                item = create_resp.json()
                print(f"  ✓ Created {art_type}: {name} (id={item.get('id', '?')})")
                result["created"].append(name)
                if art_type == "Eventhouse":
                    result["eventhouse_id"] = item.get("id")

            elif create_resp.status_code == 202:
                # Long-running operation — poll
                op_url = create_resp.headers.get("Operation-Location", "")
                print(f"  ⏳ {art_type} '{name}' creation accepted (LRO)...")
                if op_url:
                    _poll_operation(op_url, headers, name, art_type)
                result["created"].append(name)
                # Re-fetch to get ID if Eventhouse
                if art_type == "Eventhouse":
                    time.sleep(3)
                    re_resp = requests.get(
                        f"{FABRIC_API}/workspaces/{workspace_id}/items?type=Eventhouse",
                        headers=headers, timeout=30,
                    )
                    for i in re_resp.json().get("value", []):
                        if i["displayName"] == name:
                            result["eventhouse_id"] = i["id"]
                            break

            else:
                err = create_resp.text[:500]
                print(f"  ✗ Failed {art_type} '{name}': {create_resp.status_code} — {err}")
                result["failed"].append({"name": name, "error": err})

            # Brief pause between creations to be nice to the API
            time.sleep(2)

        except Exception as e:
            print(f"  ✗ Exception creating {art_type} '{name}': {e}")
            result["failed"].append({"name": name, "error": str(e)})

    return result


def cleanup_dummy_rti_artifacts(workspace_id: str, token: str) -> Dict[str, Any]:
    """Remove all RTI_Demo_* artifacts from the workspace."""
    import requests

    FABRIC_API = "https://api.fabric.microsoft.com/v1"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    result = {"deleted": [], "failed": []}

    resp = requests.get(f"{FABRIC_API}/workspaces/{workspace_id}/items", headers=headers, timeout=30)
    resp.raise_for_status()
    items = resp.json().get("value", [])

    # Delete in reverse dependency order: KQLQueryset, Eventstream, KQLDatabase, Eventhouse
    delete_order = ["KQLQueryset", "Eventstream", "KQLDatabase", "Eventhouse"]
    items_to_delete = [i for i in items if i.get("displayName", "").startswith(DUMMY_PREFIX)]
    items_to_delete.sort(key=lambda i: delete_order.index(i["type"]) if i["type"] in delete_order else 99)

    for item in items_to_delete:
        name = item["displayName"]
        item_id = item["id"]
        try:
            print(f"  Deleting {item['type']}: {name} ...")
            del_resp = requests.delete(
                f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}",
                headers=headers, timeout=60,
            )
            if del_resp.status_code in (200, 204):
                print(f"  ✓ Deleted {name}")
                result["deleted"].append(name)
            else:
                print(f"  ✗ Delete failed {name}: {del_resp.status_code}")
                result["failed"].append(name)
            time.sleep(1)
        except Exception as e:
            print(f"  ✗ Exception deleting {name}: {e}")
            result["failed"].append(name)

    return result


def _poll_operation(op_url: str, headers: dict, name: str, art_type: str, timeout: int = 120):
    """Poll a long-running operation until complete."""
    import requests
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(op_url, headers=headers, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status", "")
                if status in ("Succeeded", "Completed"):
                    print(f"  ✓ {art_type} '{name}' LRO completed")
                    return
                elif status in ("Failed", "Cancelled"):
                    print(f"  ✗ {art_type} '{name}' LRO {status}: {data.get('error', {})}")
                    return
            time.sleep(5)
        except Exception:
            time.sleep(5)
    print(f"  ⚠ {art_type} '{name}' LRO timed out after {timeout}s")


def main():
    parser = argparse.ArgumentParser(description="Create/cleanup dummy RTI artifacts")
    parser.add_argument("--cleanup", action="store_true", help="Remove dummy RTI_Demo_* artifacts")
    parser.add_argument("--workspace-id", help="Target workspace ID (defaults to primary from .workspace_state.json)")
    args = parser.parse_args()

    # Load workspace state
    state_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".workspace_state.json")
    ws_id = args.workspace_id
    if not ws_id:
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
            pairs = state.get("pairs", [])
            active_id = state.get("active_pair")
            pair = next((p for p in pairs if p.get("id") == active_id), pairs[0] if pairs else None)
            if pair:
                ws_id = pair.get("primary_id")
                print(f"Using primary workspace: {pair.get('primary_name')} ({ws_id})")
        except Exception as e:
            print(f"Could not load workspace state: {e}")
            print("Pass --workspace-id explicitly")
            sys.exit(1)

    if not ws_id:
        print("No workspace ID available. Pass --workspace-id or configure workspaces in the dashboard.")
        sys.exit(1)

    # Get token — try MSAL cache
    token = None
    cache_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".msal_token_cache.bin")
    sp_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".sp_config.json")

    # Try service principal first
    if os.path.exists(sp_file):
        try:
            import msal
            with open(sp_file, "r") as f:
                sp = json.load(f)
            app = msal.ConfidentialClientApplication(
                sp["client_id"],
                authority=f"https://login.microsoftonline.com/{sp['tenant_id']}",
                client_credential=sp["client_secret"],
            )
            result = app.acquire_token_for_client(scopes=["https://api.fabric.microsoft.com/.default"])
            if "access_token" in result:
                token = result["access_token"]
                print("Authenticated via Service Principal")
        except Exception:
            pass

    # Fallback to MSAL cache (interactive login)
    if not token and os.path.exists(cache_file):
        try:
            import msal
            cache = msal.SerializableTokenCache()
            with open(cache_file, "r") as f:
                cache.deserialize(f.read())
            app = msal.PublicClientApplication(
                "1950a258-227b-4e31-a9cf-717495945fc2",
                authority="https://login.microsoftonline.com/organizations",
                token_cache=cache,
            )
            accounts = app.get_accounts()
            if accounts:
                result = app.acquire_token_silent(
                    ["https://api.fabric.microsoft.com/.default"], account=accounts[0]
                )
                if result and "access_token" in result:
                    token = result["access_token"]
                    print(f"Authenticated as {accounts[0].get('username', '?')}")
        except Exception:
            pass

    if not token:
        print("No valid token found. Log in via the dashboard first, or set up service principal.")
        sys.exit(1)

    if args.cleanup:
        print(f"\nCleaning up dummy RTI artifacts from workspace {ws_id}...")
        result = cleanup_dummy_rti_artifacts(ws_id, token)
        print(f"\nDeleted: {len(result['deleted'])}  Failed: {len(result['failed'])}")
    else:
        print(f"\nCreating dummy RTI artifacts in workspace {ws_id}...")
        result = create_dummy_rti_artifacts(ws_id, token)
        print(f"\nCreated: {len(result['created'])}  Skipped: {len(result['skipped'])}  Failed: {len(result['failed'])}")

    # Save report
    report_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "rti_dummy_report.json")
    with open(report_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    main()

