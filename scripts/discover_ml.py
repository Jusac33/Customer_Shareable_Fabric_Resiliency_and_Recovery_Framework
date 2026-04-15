"""Discover ML items and check their OneLake structure."""
import os
import requests
import json

BASE = "http://localhost:5000"
P_WS = os.environ["PRIMARY_WORKSPACE_ID"]
S_WS = os.environ["SECONDARY_WORKSPACE_ID"]

# Get items via the workspace items API (internal)
# Use the inventory page endpoint
r = requests.get(BASE + "/api/bcdr/inventory", timeout=60)
data = r.json()

# Try to find items from inventory
if "primary_items" in data:
    p_items = data["primary_items"]
    s_items = data.get("secondary_items", [])
elif "items" in data:
    p_items = data["items"]
    s_items = data.get("secondary_items", data.get("items_secondary", []))
else:
    # Fall back: call the workspace items endpoint directly
    print("Inventory keys: " + str(list(data.keys())[:10]))
    print("Trying /api/bcdr/workspace-items ...")
    r2 = requests.get(BASE + "/api/bcdr/workspace-items", timeout=30)
    print("workspace-items status: " + str(r2.status_code))
    if r2.status_code == 200:
        data2 = r2.json()
        p_items = data2.get("primary", data2.get("items", []))
        s_items = data2.get("secondary", [])
    else:
        p_items = []
        s_items = []

ml_types = ["MLModel", "MLExperiment"]

print("=== PRIMARY ML ITEMS ===")
p_ml = [i for i in p_items if i.get("type") in ml_types]
for item in p_ml:
    print("  " + item["type"] + ": " + item["displayName"] + " (id: " + item["id"] + ")")

print()
print("=== SECONDARY ML ITEMS ===")
s_ml = [i for i in s_items if i.get("type") in ml_types]
for item in s_ml:
    print("  " + item["type"] + ": " + item["displayName"] + " (id: " + item["id"] + ")")

# Build pairs
print()
print("=== ML PAIRS ===")
s_by = {}
for i in s_ml:
    s_by[i["type"] + ":" + i["displayName"]] = i

pairs = []
for item in p_ml:
    key = item["type"] + ":" + item["displayName"]
    sec = s_by.get(key)
    if sec:
        pairs.append({
            "type": item["type"],
            "name": item["displayName"],
            "primary_id": item["id"],
            "secondary_id": sec["id"],
        })
        print("  " + item["type"] + ": " + item["displayName"])
        print("    P: " + item["id"])
        print("    S: " + sec["id"])

# Check OneLake structure for each pair
print()
print("=== ONELAKE STRUCTURE ===")
for pair in pairs:
    for label, ws, item_id in [("PRIMARY", P_WS, pair["primary_id"]), ("SECONDARY", S_WS, pair["secondary_id"])]:
        r2 = requests.get(BASE + "/api/bcdr/onelake-list",
            params={"workspace_id": ws, "lakehouse_id": item_id, "subpath": ""},
            timeout=60)
        print()
        print("  " + label + " " + pair["type"] + " '" + pair["name"] + "' (" + item_id[:8] + ")")
        if r2.status_code == 200:
            paths = r2.json().get("paths", [])
            for p in paths[:20]:
                is_dir = "(DIR)" if p.get("isDirectory") == "true" else "(FILE)"
                size = p.get("contentLength", "")
                name = p.get("name", "?")
                # strip item id prefix for readability
                display = name.replace(item_id + "/", "")
                extra = " [" + size + " bytes]" if size else ""
                print("    " + is_dir + " " + display + extra)
            if len(paths) > 20:
                print("    ... and " + str(len(paths) - 20) + " more")
            print("    Total: " + str(len(paths)))
        else:
            print("    ERROR " + str(r2.status_code) + ": " + r2.text[:200])
