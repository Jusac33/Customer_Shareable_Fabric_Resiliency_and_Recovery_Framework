"""
Example: Create OneLake Shortcuts

Minimal, self-contained OneLake Shortcuts example demonstrating zero-copy DR pattern.

This example shows:
  - Authenticating to Fabric API using MSAL
  - Creating OneLake Shortcuts in a secondary lakehouse pointing to primary
  - Zero-copy, near-zero RPO disaster recovery pattern

Prerequisites:
  - Azure AD Service Principal credentials
  - Python packages: msal, requests

Run:
  python create_shortcuts.py
"""

import json
import logging
import msal
import requests

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# CONFIGURATION - Update these
TENANT_ID = "your-tenant-id"
CLIENT_ID = "your-service-principal-client-id"
CLIENT_SECRET = "your-service-principal-secret"

PRIMARY_WORKSPACE_GUID = "550e8400-e29b-41d4-a716-446655440000"
PRIMARY_LAKEHOUSE_GUID = "660e8400-e29b-41d4-a716-446655440001"

SECONDARY_WORKSPACE_GUID = "550e8400-e29b-41d4-a716-446655440002"
SECONDARY_LAKEHOUSE_GUID = "660e8400-e29b-41d4-a716-446655440003"

# API endpoint
FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"


def get_auth_token() -> str:
    """Get Bearer token using MSAL"""
    app = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET,
    )
    
    token_response = app.acquire_token_for_client(
        scopes=["https://api.fabric.microsoft.com/.default"]
    )
    
    if "access_token" not in token_response:
        raise Exception(f"Token acquisition failed: {token_response.get('error_description')}")
    
    return token_response["access_token"]


def create_shortcut(
    workspace_guid: str,
    lakehouse_guid: str,
    table_name: str,
    primary_workspace_guid: str,
    primary_lakehouse_guid: str,
    token: str,
) -> bool:
    """
    Create a OneLake shortcut in secondary lakehouse pointing to primary table.
    
    Args:
        workspace_guid: Secondary workspace GUID
        lakehouse_guid: Secondary lakehouse GUID
        table_name: Table name to create shortcut for
        primary_workspace_guid: Primary workspace GUID
        primary_lakehouse_guid: Primary lakehouse GUID
        token: Auth token
        
    Returns:
        True if successful
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    # Shortcut definition
    shortcut = {
        "path": "/Tables",
        "name": table_name,
        "target": {
            "type": "OneLake",
            "oneLake": {
                "workspaceId": primary_workspace_guid,
                "itemId": primary_lakehouse_guid,
                "path": f"/Tables/{table_name}",
            },
        },
    }
    
    endpoint = (
        f"{FABRIC_API_BASE}/workspaces/{workspace_guid}/lakehouses/"
        f"{lakehouse_guid}/shortcuts"
    )
    
    logger.info(f"Creating shortcut for table: {table_name}")
    logger.info(f"  → Points to primary table: {table_name}")
    
    try:
        response = requests.post(endpoint, json=shortcut, headers=headers, timeout=30)
        
        if response.status_code in [200, 201]:
            logger.info(f"✓ Shortcut created successfully: {table_name}")
            return True
        elif response.status_code == 202:
            # Async operation
            logger.info(f"✓ Shortcut creation initiated (async): {table_name}")
            return True
        else:
            logger.error(f"✗ Failed to create shortcut: {response.status_code}")
            logger.error(f"  Response: {response.text}")
            return False
    
    except Exception as e:
        logger.error(f"✗ Error creating shortcut: {str(e)}")
        return False


def create_shortcuts_for_tables(
    primary_workspace_guid: str,
    primary_lakehouse_guid: str,
    secondary_workspace_guid: str,
    secondary_lakehouse_guid: str,
    table_names: list,
    token: str,
) -> dict:
    """
    Create shortcuts for multiple tables.
    
    Args:
        primary_workspace_guid: Primary workspace GUID
        primary_lakehouse_guid: Primary lakehouse GUID
        secondary_workspace_guid: Secondary workspace GUID
        secondary_lakehouse_guid: Secondary lakehouse GUID
        table_names: List of table names
        token: Auth token
        
    Returns:
        Summary dict
    """
    result = {
        "created": [],
        "failed": [],
    }
    
    logger.info(f"Creating shortcut entries for {len(table_names)} tables...")
    
    for table_name in table_names:
        success = create_shortcut(
            secondary_workspace_guid,
            secondary_lakehouse_guid,
            table_name,
            primary_workspace_guid,
            primary_lakehouse_guid,
            token,
        )
        
        if success:
            result["created"].append(table_name)
        else:
            result["failed"].append(table_name)
    
    return result


if __name__ == "__main__":
    try:
        logger.info("Authenticating to Fabric API...")
        token = get_auth_token()
        logger.info("✓ Authentication successful")
        
        # EXAMPLE: Create shortcuts for a few tables
        # In production, you would discover table names dynamically
        table_names = [
            "sales_transactions",
            "customer_master",
            "product_catalog",
        ]
        
        logger.info("\n=== Creating OneLake Shortcuts ===")
        result = create_shortcuts_for_tables(
            PRIMARY_WORKSPACE_GUID,
            PRIMARY_LAKEHOUSE_GUID,
            SECONDARY_WORKSPACE_GUID,
            SECONDARY_LAKEHOUSE_GUID,
            table_names,
            token,
        )
        
        # Summary
        print("\n" + "=" * 70)
        print("SHORTCUT CREATION SUMMARY")
        print("=" * 70)
        print(f"Primary Workspace:          {PRIMARY_WORKSPACE_GUID}")
        print(f"Primary Lakehouse:          {PRIMARY_LAKEHOUSE_GUID}")
        print(f"Secondary Workspace:        {SECONDARY_WORKSPACE_GUID}")
        print(f"Secondary Lakehouse:        {SECONDARY_LAKEHOUSE_GUID}")
        print(f"\nShortcuts Created:          {len(result['created'])}")
        print(f"Shortcuts Failed:           {len(result['failed'])}")
        print("=" * 70)
        
        if result["created"]:
            print(f"\n✓ Created shortcuts to:")
            for table in result["created"]:
                print(f"  - {table}")
        
        if result["failed"]:
            print(f"\n✗ Failed to create shortcuts for:")
            for table in result["failed"]:
                print(f"  - {table}")
        
        print("\n⏱ Zero-Copy DR Pattern Deployed")
        print("  RPO: Near-Zero (reads go to primary, failover redirects to secondary)")
        print("  RTO: Seconds (no data copy overhead)")
        print()
        
        success = len(result["failed"]) == 0
        exit(0 if success else 1)
    
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}", exc_info=True)
        exit(1)
