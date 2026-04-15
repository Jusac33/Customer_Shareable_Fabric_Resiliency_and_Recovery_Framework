"""
Example: Clone Lakehouse Simple

Minimal, self-contained OneLake azcopy example for Fabric DR.

This example demonstrates:
  - Authenticating to Fabric API (though not needed for azcopy)
  - Using azcopy to sync a single Lakehouse between two OneLake workspaces
  - No dependencies on other scripts or mapping files

Prerequisites:
  - azcopy installed: https://aka.ms/downloadazcopy
  - OneLake URI format knowledge

Run:
  python clone_lakehouse_simple.py
"""

import subprocess
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def clone_lakehouse_via_azcopy(
    primary_workspace_guid: str,
    primary_lakehouse_name: str,
    secondary_workspace_guid: str,
    secondary_lakehouse_name: str,
) -> bool:
    """
    Clone a single lakehouse using azcopy.
    
    Args:
        primary_workspace_guid: e.g., "550e8400-e29b-41d4-a716-446655440000"
        primary_lakehouse_name: e.g., "sales_data"
        secondary_workspace_guid: Secondary workspace GUID
        secondary_lakehouse_name: Secondary lakehouse name
        
    Returns:
        True if successful
    """
    
    # Construct OneLake URIs
    source = (
        f"https://onelake.dfs.fabric.microsoft.com/{primary_workspace_guid}/"
        f"{primary_lakehouse_name}.Lakehouse/Tables"
    )
    dest = (
        f"https://onelake.dfs.fabric.microsoft.com/{secondary_workspace_guid}/"
        f"{secondary_lakehouse_name}.Lakehouse/Tables"
    )
    
    logger.info(f"Cloning Lakehouse: {primary_lakehouse_name}")
    logger.info(f"  Source: {source}")
    logger.info(f"  Dest:   {dest}")
    
    # Construct azcopy command
    cmd = [
        "azcopy",
        "sync",
        source,
        dest,
        "--recursive",
        "--trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com",
    ]
    
    try:
        logger.info(f"Executing: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        
        if result.returncode == 0:
            logger.info("✓ Lakehouse clone successful!")
            if result.stdout:
                logger.info(f"Output: {result.stdout}")
            return True
        else:
            logger.error(f"✗ azcopy failed with return code {result.returncode}")
            logger.error(f"Error: {result.stderr}")
            return False
    
    except subprocess.TimeoutExpired:
        logger.error("✗ azcopy timed out after 1 hour")
        return False
    
    except FileNotFoundError:
        logger.error("✗ azcopy not found - install from: https://aka.ms/downloadazcopy")
        return False
    
    except Exception as e:
        logger.error(f"✗ Unexpected error: {str(e)}")
        return False


if __name__ == "__main__":
    # EXAMPLE VALUES - Update with your GUIDs and names
    PRIMARY_WORKSPACE_GUID = "550e8400-e29b-41d4-a716-446655440000"
    PRIMARY_LAKEHOUSE_NAME = "sales_data"
    
    SECONDARY_WORKSPACE_GUID = "550e8400-e29b-41d4-a716-446655440001"
    SECONDARY_LAKEHOUSE_NAME = "sales_data"  # Usually same name
    
    # Run the clone
    success = clone_lakehouse_via_azcopy(
        PRIMARY_WORKSPACE_GUID,
        PRIMARY_LAKEHOUSE_NAME,
        SECONDARY_WORKSPACE_GUID,
        SECONDARY_LAKEHOUSE_NAME,
    )
    
    exit(0 if success else 1)
