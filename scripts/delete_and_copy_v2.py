"""Delete all tables from secondary lakehouses, azcopy full copy, verify tables."""
import os
import requests
import json
import time

BASE = "http://localhost:5000"
P_WS = os.environ["PRIMARY_WORKSPACE_ID"]
S_WS = os.environ["SECONDARY_WORKSPACE_ID"]

def step(msg):
    print()
    print("=" * 60)
    print("  " + msg)
    print("=" * 60)

# Step 1: Delete ALL table data from secondary lakehouses
step("STEP 1: Deleting ALL table folders from secondary lakehouses")
r = requests.post(BASE + "/api/bcdr/delete-secondary-tables", json={
    "lakehouse": "ALL",
    "dry_run": False
}, timeout=600)
print("Delete status: " + str(r.status_code))
print(json.dumps(r.json(), indent=2))

# Step 2: Azcopy full copy all lakehouses
step("STEP 2: Azcopy FULL COPY for ALL lakehouses (Tables)")
r2 = requests.post(BASE + "/api/bcdr/azcopy-replicate", json={
    "mode": "copy",
    "lakehouse": "ALL",
    "subpath": "Tables",
    "dry_run": False
}, timeout=1800)
print("Copy status: " + str(r2.status_code))
data = r2.json()
for lh in data.get("lakehouses", []):
    name = lh.get("lakehouse", "?")
    for sp in lh.get("subpaths", []):
        lines = sp.get("summary_lines", [])
        summary = "; ".join(lines[-3:]) if lines else sp.get("status", "?")
        print("  " + name + "/" + sp.get("subpath", "?") + ": " + summary)
if data.get("errors"):
    print("ERRORS: " + json.dumps(data["errors"]))

# Step 3: Verify structure
step("STEP 3: Waiting 20s for Fabric table discovery...")
time.sleep(20)

step("STEP 4: Comparing folder structure")
pairs = requests.get(BASE + "/api/bcdr/azcopy-status", timeout=30).json().get("lakehouse_pairs", [])
for pair in pairs:
    name = pair["name"]
    p_id = pair["primary_id"]
    s_id = pair["secondary_id"]
    print("\n--- " + name + " ---")
    for label, ws, lh_id in [("PRIMARY", P_WS, p_id), ("SECONDARY", S_WS, s_id)]:
        r3 = requests.get(BASE + "/api/bcdr/onelake-list",
            params={"workspace_id": ws, "lakehouse_id": lh_id, "subpath": "Tables"}, timeout=60)
        if r3.status_code == 200:
            paths = r3.json().get("paths", [])
            dirs = sorted([p.get("name","") for p in paths if p.get("isDirectory") == "true"])
            prefix = lh_id + "/Tables/"
            # Show top-level schema dirs only (depth 1 under Tables)
            schema_dirs = []
            for d in dirs:
                clean = d.replace(prefix, "") if prefix in d else d
                if "/" not in clean:
                    schema_dirs.append(clean)
            print("  " + label + " top-level: " + str(schema_dirs))
            print("    Total dirs: " + str(len(dirs)) + ", files: " + str(len([p for p in paths if p.get("isDirectory") != "true"])))
        else:
            print("  " + label + ": ERROR " + str(r3.status_code))

# Step 5: Check Fabric Tables API
step("STEP 5: Verifying tables via Fabric Tables API")
r4 = requests.get(BASE + "/api/bcdr/lakehouse-tables", timeout=120)
if r4.status_code == 200:
    data = r4.json()
    for side in ["primary", "secondary"]:
        print("\n  " + side.upper() + ":")
        for lh_name, lh_data in data.get(side, {}).items():
            tables = [t["name"] for t in lh_data.get("tables", [])]
            print("    " + lh_name + ": " + str(tables))
else:
    print("ERROR: " + str(r4.status_code))

print("\nDone!")

