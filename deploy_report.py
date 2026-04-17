"""Deploy updated report to Fabric workspace using updateDefinition API.

The bundled CrestShield-Claim-report.Report/definition.pbir ships with
placeholder tokens for workspace name, semantic model name, and semantic
model id. This script rewrites those tokens in-memory at deploy time using
live Fabric API lookups, so the customer does not need to edit the .pbir
file before running deploy_report.py.

Required env vars: PRIMARY_WORKSPACE_ID, REPORT_ID.
Optional: SEMANTIC_MODEL_ID (if omitted, the report's current binding is
reused; if that also isn't available, the first SemanticModel in the
workspace is used).
"""
import os
import sys
import json
import base64
import requests

FABRIC_BASE = "https://api.fabric.microsoft.com/v1"

# Placeholder tokens present in the shipped definition.pbir
PLACEHOLDER_WORKSPACE_NAME = "your-workspace-name"
PLACEHOLDER_MODEL_NAME = "YourSemanticModelName"
PLACEHOLDER_MODEL_ID = "your-primary-semanticmodel-id"


def get_token():
    import subprocess
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://api.fabric.microsoft.com", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True, shell=True
    )
    return result.stdout.strip()


def _fabric_get(url, headers):
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def resolve_report_binding(workspace_id, report_id, headers):
    """Look up workspace name + the semantic model the report should bind to.

    Resolution order for the semantic model id:
      1. SEMANTIC_MODEL_ID env var (explicit override)
      2. The report's current datasetReference (read from live report definition)
      3. The first SemanticModel in the workspace (last-resort convenience)
    """
    ws = _fabric_get(f"{FABRIC_BASE}/workspaces/{workspace_id}", headers)
    workspace_name = ws.get("displayName") or ws.get("name") or ""

    model_id = os.environ.get("SEMANTIC_MODEL_ID", "").strip() or None

    if not model_id:
        # Try to read the report's current binding from the live service
        try:
            r = requests.post(
                f"{FABRIC_BASE}/workspaces/{workspace_id}/items/{report_id}/getDefinition",
                headers=headers, timeout=60,
            )
            if r.status_code in (200, 202):
                # 202 = long-running; for simplicity fall through if not immediate
                if r.status_code == 200:
                    parts = r.json().get("definition", {}).get("parts", [])
                    for part in parts:
                        if part.get("path", "").endswith(".pbir"):
                            try:
                                raw = base64.b64decode(part.get("payload", "")).decode("utf-8", errors="replace")
                                pbir = json.loads(raw)
                                conn = pbir.get("datasetReference", {}).get("byConnection", {}).get("connectionString", "")
                                # Extract semanticmodelid=<guid>
                                if "semanticmodelid=" in conn.lower():
                                    tail = conn.split("=")[-1].strip().strip('"').strip("'")
                                    if tail and tail != PLACEHOLDER_MODEL_ID:
                                        model_id = tail
                                        break
                            except Exception:
                                pass
        except Exception:
            pass

    if not model_id:
        # Last-resort: first SemanticModel in the workspace
        items = _fabric_get(f"{FABRIC_BASE}/workspaces/{workspace_id}/items", headers).get("value", [])
        models = [i for i in items if i.get("type") == "SemanticModel"]
        if models:
            model_id = models[0].get("id")
            model_name = models[0].get("displayName", "")
        else:
            raise RuntimeError(
                "Could not determine semantic model id. Set SEMANTIC_MODEL_ID in .env "
                "or ensure the target workspace contains a SemanticModel."
            )
    else:
        # Look up the model's display name for the connection string
        try:
            m = _fabric_get(f"{FABRIC_BASE}/workspaces/{workspace_id}/semanticModels/{model_id}", headers)
            model_name = m.get("displayName") or ""
        except Exception:
            model_name = ""

    return workspace_name, model_name, model_id


def rewrite_pbir_connection(raw_bytes, workspace_name, model_name, model_id):
    """Substitute placeholders in definition.pbir. Returns updated bytes."""
    text = raw_bytes.decode("utf-8")
    if model_name:
        text = text.replace(PLACEHOLDER_MODEL_NAME, model_name)
    if workspace_name:
        text = text.replace(PLACEHOLDER_WORKSPACE_NAME, workspace_name)
    if model_id:
        text = text.replace(PLACEHOLDER_MODEL_ID, model_id)
    return text.encode("utf-8")


def build_parts(base_path, workspace_name, model_name, model_id):
    parts = []
    for root, dirs, files in os.walk(base_path):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, base_path).replace("\\", "/")
            with open(full_path, "rb") as fh:
                content = fh.read()
            # Rewrite the shipped placeholder connection string in definition.pbir
            if rel_path.endswith("definition.pbir"):
                content = rewrite_pbir_connection(content, workspace_name, model_name, model_id)
            b64 = base64.b64encode(content).decode("utf-8")
            parts.append({
                "path": rel_path,
                "payload": b64,
                "payloadType": "InlineBase64"
            })
    return parts


def main():
    workspace_id = os.environ["PRIMARY_WORKSPACE_ID"]
    report_id = os.environ["REPORT_ID"]
    base_path = os.path.join(os.path.dirname(__file__), "CrestShield-Claim-report.Report")

    token = get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    workspace_name, model_name, model_id = resolve_report_binding(workspace_id, report_id, headers)
    print(f"Binding report to workspace='{workspace_name}', "
          f"semantic model='{model_name}' ({model_id})")

    parts = build_parts(base_path, workspace_name, model_name, model_id)
    print(f"Built {len(parts)} definition parts")
    for p in parts:
        print(f"  {p['path']}")

    payload = {
        "definition": {
            "parts": parts
        }
    }

    url = f"{FABRIC_BASE}/workspaces/{workspace_id}/items/{report_id}/updateDefinition"
    print(f"\nPOSTing to: {url}")
    
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    print(f"Status: {resp.status_code}")

    if resp.status_code == 200:
        print("Report updated successfully!")
        return True
    elif resp.status_code == 202:
        # Long-running operation
        op_url = resp.headers.get("Location") or resp.headers.get("Operation-Location")
        print(f"Long-running operation: {op_url}")
        
        import time
        for i in range(30):
            time.sleep(5)
            poll_resp = requests.get(op_url, headers=headers, timeout=30)
            if poll_resp.status_code == 200:
                data = poll_resp.json()
                status = data.get("status", "")
                print(f"  Poll {i+1}: {status} ({data.get('percentComplete', '?')}%)")
                if status == "Succeeded":
                    print("Report updated successfully!")
                    return True
                elif status in ["Failed", "Cancelled"]:
                    print(f"Operation failed: {data.get('error', {})}")
                    return False
        print("Timeout waiting for operation")
        return False
    else:
        print(f"Error: {resp.text[:500]}")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
