"""
Create a dummy Fabric Environment in the primary workspace with:
  - Spark compute settings (driver/executor config, spark_conf env vars)
  - Public library (environment.yml)
Then replicate it to the secondary workspace.
"""

import base64
import json
import os
import subprocess
import sys
import time

PRIMARY_WS = os.environ["PRIMARY_WORKSPACE_ID"]
SECONDARY_WS = os.environ["SECONDARY_WORKSPACE_ID"]
ENV_NAME = "BCDR_Environment"
RESOURCE = "https://api.fabric.microsoft.com"

# ---------- YAML content for Spark compute ----------
SPARK_COMPUTE_YML = """\
enable_native_execution_engine: false
driver_cores: 4
driver_memory: 28g
executor_cores: 4
executor_memory: 28g
dynamic_executor_allocation:
  enabled: true
  min_executors: 1
  max_executors: 2
spark_conf:
  spark.executorEnv.BCDR_ENV: production
  spark.executorEnv.BCDR_REGION: eastus
  spark.executorEnv.BCDR_APP_NAME: CrestShield
  spark.driverEnv.BCDR_ENV: production
  spark.driverEnv.BCDR_REGION: eastus
  spark.driverEnv.BCDR_APP_NAME: CrestShield
runtime_version: "1.3"
"""

# ---------- YAML content for public libraries ----------
ENVIRONMENT_YML = """\
dependencies:
  - numpy==1.26.4
  - pip:
      - requests==2.31.0
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("utf-8")


def az_rest(method: str, url: str, body: dict | None = None) -> dict:
    cmd = [
        r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd", "rest",
        "--method", method,
        "--url", url,
        "--resource", RESOURCE,
    ]
    if body:
        cmd += ["--body", json.dumps(body)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        # Check for 202 in stderr (long-running)
        if "202" in result.stderr:
            print("Long-running operation accepted")
            return {"status": "accepted"}
        raise RuntimeError(result.stderr)
    if result.stdout.strip():
        return json.loads(result.stdout)
    return {}


def create_environment(workspace_id: str, name: str, description: str, definition: dict | None = None) -> dict:
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/environments"
    payload = {"displayName": name, "description": description}
    if definition:
        payload["definition"] = definition
    print(f"Creating environment '{name}' in workspace {workspace_id}...")
    return az_rest("POST", url, payload)


def get_environment_definition(workspace_id: str, env_id: str) -> dict:
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/environments/{env_id}/getDefinition"
    print(f"Getting definition for environment {env_id}...")
    return az_rest("POST", url)


def update_environment_definition(workspace_id: str, env_id: str, definition: dict) -> dict:
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/environments/{env_id}/updateDefinition"
    payload = {"definition": definition}
    print(f"Updating definition for environment {env_id}...")
    return az_rest("POST", url, payload)


def publish_environment(workspace_id: str, env_id: str) -> dict:
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/environments/{env_id}/staging/publish?beta=false"
    print(f"Publishing environment {env_id}...")
    return az_rest("POST", url)


def list_environments(workspace_id: str) -> list:
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/environments"
    resp = az_rest("GET", url)
    return resp.get("value", [])


def main():
    definition = {
        "parts": [
            {
                "path": "Setting/Sparkcompute.yml",
                "payload": _b64(SPARK_COMPUTE_YML),
                "payloadType": "InlineBase64",
            },
            {
                "path": "Libraries/PublicLibraries/environment.yml",
                "payload": _b64(ENVIRONMENT_YML),
                "payloadType": "InlineBase64",
            },
        ]
    }

    # ---- Step 1: Create in Primary ----
    print("=" * 60)
    print("STEP 1: Create dummy environment in PRIMARY workspace")
    print("=" * 60)
    
    existing = list_environments(PRIMARY_WS)
    primary_env = next((e for e in existing if e["displayName"] == ENV_NAME), None)
    
    if primary_env:
        print(f"Environment '{ENV_NAME}' already exists: {primary_env['id']}")
        print("Updating definition...")
        update_environment_definition(PRIMARY_WS, primary_env["id"], definition)
    else:
        result = create_environment(PRIMARY_WS, ENV_NAME,
                                    "Resiliency & Recovery dummy environment with Spark config variables and libraries.",
                                    definition)
        print(f"Created: {json.dumps(result, indent=2)}")
        primary_env = result

    primary_env_id = primary_env.get("id", "unknown")
    print(f"\nPrimary environment ID: {primary_env_id}")

    # Publish primary
    print("\nPublishing primary environment...")
    try:
        pub_result = publish_environment(PRIMARY_WS, primary_env_id)
        print(f"Publish result: {json.dumps(pub_result, indent=2)}")
    except Exception as e:
        print(f"Publish note: {e}")

    # Wait for publish
    print("Waiting 15s for publish to complete...")
    time.sleep(15)

    # ---- Step 2: Get definition from primary ----
    print("\n" + "=" * 60)
    print("STEP 2: Get definition from PRIMARY")
    print("=" * 60)
    
    defn = get_environment_definition(PRIMARY_WS, primary_env_id)
    print(f"Definition parts: {len(defn.get('definition', {}).get('parts', []))}")
    for part in defn.get("definition", {}).get("parts", []):
        print(f"  - {part['path']} ({len(part.get('payload', ''))} chars)")
        decoded = base64.b64decode(part["payload"]).decode("utf-8")
        print(f"    Content:\n{decoded}")

    # ---- Step 3: Replicate to Secondary ----
    print("\n" + "=" * 60)
    print("STEP 3: Replicate to SECONDARY workspace")
    print("=" * 60)

    sec_existing = list_environments(SECONDARY_WS)
    secondary_env = next((e for e in sec_existing if e["displayName"] == ENV_NAME), None)

    replicate_def = defn.get("definition", definition)
    # Remove .platform part if present (it contains workspace-specific metadata)
    replicate_def["parts"] = [p for p in replicate_def.get("parts", []) if p["path"] != ".platform"]

    if secondary_env:
        print(f"Environment '{ENV_NAME}' already exists in secondary: {secondary_env['id']}")
        update_environment_definition(SECONDARY_WS, secondary_env["id"], replicate_def)
        secondary_env_id = secondary_env["id"]
    else:
        result = create_environment(SECONDARY_WS, ENV_NAME,
                                    "Resiliency & Recovery dummy environment with Spark config variables and libraries.",
                                    replicate_def)
        print(f"Created in secondary: {json.dumps(result, indent=2)}")
        secondary_env_id = result.get("id", "unknown")

    # Publish secondary
    print(f"\nSecondary environment ID: {secondary_env_id}")
    print("Publishing secondary environment...")
    try:
        pub_result = publish_environment(SECONDARY_WS, secondary_env_id)
        print(f"Publish result: {json.dumps(pub_result, indent=2)}")
    except Exception as e:
        print(f"Publish note: {e}")

    print("Waiting 15s for publish to complete...")
    time.sleep(15)

    # ---- Step 4: Verify ----
    print("\n" + "=" * 60)
    print("STEP 4: Verify secondary definition")
    print("=" * 60)

    sec_defn = get_environment_definition(SECONDARY_WS, secondary_env_id)
    for part in sec_defn.get("definition", {}).get("parts", []):
        print(f"  - {part['path']} ({len(part.get('payload', ''))} chars)")
        decoded = base64.b64decode(part["payload"]).decode("utf-8")
        print(f"    Content:\n{decoded}")

    print("\n✓ Done! Environment replicated successfully.")


if __name__ == "__main__":
    main()

