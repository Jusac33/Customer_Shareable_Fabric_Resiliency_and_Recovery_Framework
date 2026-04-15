"""Delete all tables from secondary lakehouses, azcopy full copy, verify tables."""
import requests
import json
import time
import sys

BASE = "http://localhost:5000"

def step(msg):
    print()
    print("=" * 60)
    print("  " + msg)
    print("=" * 60)

# ------------------------------------------------------------------
# Step 1: Discover tables in secondary lakehouses (before)
# ------------------------------------------------------------------
step("STEP 1: Discovering tables in secondary lakehouses (BEFORE)")
r = requests.get(BASE + "/api/bcdr/lakehouse-tables", timeout=120)
if r.status_code == 200:
    print(json.dumps(r.json(), indent=2)[:3000])
else:
    print("lakehouse-tables status: " + str(r.status_code))
    print(r.text[:500])

# ------------------------------------------------------------------
# Step 2: Delete ALL table data from secondary lakehouses
# ------------------------------------------------------------------
step("STEP 2: Deleting ALL tables from secondary lakehouses (azcopy remove)")

r2 = requests.post(BASE + "/api/bcdr/delete-secondary-tables", json={
    "lakehouse": "ALL",
    "dry_run": False
}, timeout=600)
print("Delete status: " + str(r2.status_code))
if r2.status_code == 200:
    print(json.dumps(r2.json(), indent=2))
else:
    print(r2.text[:500])

# ------------------------------------------------------------------
# Step 3: Azcopy full copy all lakehouses
# ------------------------------------------------------------------
step("STEP 3: Azcopy FULL COPY for ALL lakehouses (Tables)")

r3 = requests.post(BASE + "/api/bcdr/azcopy-replicate", json={
    "mode": "copy",
    "lakehouse": "ALL",
    "subpath": "Tables",
    "dry_run": False
}, timeout=1800)
print("Copy status: " + str(r3.status_code))
if r3.status_code == 200:
    data = r3.json()
    for lh in data.get("lakehouses", []):
        name = lh.get("lakehouse", "?")
        for sp in lh.get("subpaths", []):
            lines = sp.get("summary_lines", [])
            summary = "; ".join(lines[-3:]) if lines else sp.get("status", "?")
            print("  " + name + "/" + sp.get("subpath", "?") + ": " + summary)
    if data.get("errors"):
        print("ERRORS: " + json.dumps(data["errors"]))
else:
    print(r3.text[:500])

# ------------------------------------------------------------------
# Step 4: Wait a bit and verify tables in secondary
# ------------------------------------------------------------------
step("STEP 4: Waiting 20s for Fabric table discovery...")
time.sleep(20)

step("STEP 5: Verifying tables in secondary lakehouses (AFTER)")
r4 = requests.get(BASE + "/api/bcdr/lakehouse-tables", timeout=120)
if r4.status_code == 200:
    print(json.dumps(r4.json(), indent=2)[:5000])
else:
    print("ERROR: " + str(r4.status_code))
    print(r4.text[:500])

print()
print("Done!")
