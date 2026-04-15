"""Deploy updated report to Fabric workspace using updateDefinition API"""
import os
import sys
import json
import base64
import requests

def get_token():
    import subprocess
    result = subprocess.run(
        ["az", "account", "get-access-token", "--resource", "https://api.fabric.microsoft.com", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True, shell=True
    )
    return result.stdout.strip()

def build_parts(base_path):
    parts = []
    for root, dirs, files in os.walk(base_path):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, base_path).replace("\\", "/")
            with open(full_path, "rb") as fh:
                content = fh.read()
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

    parts = build_parts(base_path)
    print(f"Built {len(parts)} definition parts")
    for p in parts:
        print(f"  {p['path']}")

    payload = {
        "definition": {
            "parts": parts
        }
    }

    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items/{report_id}/updateDefinition"
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
