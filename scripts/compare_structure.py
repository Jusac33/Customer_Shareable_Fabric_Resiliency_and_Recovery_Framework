"""Compare exact OneLake DFS folder structure between primary and secondary lakehouses."""
import os
import requests
import json
import sys

BASE = "http://localhost:5000"

# Get lakehouse pairs
r = requests.get(BASE + "/api/bcdr/azcopy-status", timeout=30)
pairs = r.json().get("lakehouse_pairs", [])
p_ws = os.environ["PRIMARY_WORKSPACE_ID"]
s_ws = os.environ["SECONDARY_WORKSPACE_ID"]

# Get OneLake token through an internal helper
# We'll use the lakehouse-tables endpoint's logic, but we need raw DFS listing
# Let's call a helper that returns the token
# Actually, let's just create a small endpoint call to list recursively

print("=" * 80)
print("COMPARING PRIMARY vs SECONDARY OneLake folder structure")
print("=" * 80)

for pair in pairs:
    name = pair["name"]
    p_id = pair["primary_id"]
    s_id = pair["secondary_id"]
    
    print("\n" + "-" * 70)
    print("LAKEHOUSE: " + name)
    print("-" * 70)
    
    # Use the app's API to do the DFS listing via a POST
    for label, ws, lh_id in [("PRIMARY", p_ws, p_id), ("SECONDARY", s_ws, s_id)]:
        # Call the internal DFS listing through a helper
        r = requests.get(
            BASE + "/api/bcdr/onelake-list",
            params={"workspace_id": ws, "lakehouse_id": lh_id, "subpath": "Tables"},
            timeout=60
        )
        print("\n  " + label + " (" + lh_id[:8] + "...):")
        if r.status_code == 200:
            paths = r.json().get("paths", [])
            dirs = sorted([p.get("name","") for p in paths if p.get("isDirectory") == "true"])
            files = sorted([p.get("name","") for p in paths if p.get("isDirectory") != "true"])
            
            # Show directory tree (strip lakehouse ID prefix for readability)
            prefix = lh_id + "/Tables/"
            for d in dirs:
                display = d.replace(prefix, "Tables/") if prefix in d else d
                print("    [DIR]  " + display)
            
            file_count = len(files)
            if file_count > 0:
                # Show first few files
                for f in files[:10]:
                    display = f.replace(prefix, "Tables/") if prefix in f else f
                    print("    [FILE] " + display)
                if file_count > 10:
                    print("    ... and " + str(file_count - 10) + " more files")
            
            print("    Total: " + str(len(dirs)) + " dirs, " + str(file_count) + " files")
        elif r.status_code == 404:
            print("    Endpoint not found - need to add /api/bcdr/onelake-list")
            break
        else:
            print("    ERROR " + str(r.status_code) + ": " + r.text[:300])

print("\nDone!")

