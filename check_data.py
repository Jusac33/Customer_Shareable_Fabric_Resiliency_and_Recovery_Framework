import os
import subprocess, json, requests

token = subprocess.check_output(
    ["az", "account", "get-access-token", "--resource",
     "https://analysis.windows.net/powerbi/api", "--query", "accessToken", "-o", "tsv"],
    shell=True
).decode().strip()

ws = os.environ["PRIMARY_WORKSPACE_ID"]
sm = os.environ["SEMANTIC_MODEL_ID"]
url = f"https://api.powerbi.com/v1.0/myorg/groups/{ws}/datasets/{sm}/executeQueries"
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

for col in ["vehicle_make", "coverage_type", "incident_type", "incident_state", "settlement_status"]:
    dax = (
        'EVALUATE TOPN(5, ADDCOLUMNS(VALUES(gold_claims_routed[' + col + ']), '
        '"cnt", CALCULATE(COUNTROWS(gold_claims_routed))), [cnt], DESC)'
    )
    body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
    r = requests.post(url, headers=headers, json=body)
    data = r.json()
    if "error" in data:
        print(f"{col}: ERROR - {json.dumps(data['error'], indent=2)[:200]}")
    else:
        rows = data["results"][0]["tables"][0]["rows"]
        print(f"{col}: {rows[:5]}")
    print()

# Also check gold_policy_risk_profile
for col in ["vehicle_make", "coverage_type"]:
    dax = (
        'EVALUATE TOPN(5, ADDCOLUMNS(VALUES(gold_policy_risk_profile[' + col + ']), '
        '"cnt", CALCULATE(COUNTROWS(gold_policy_risk_profile))), [cnt], DESC)'
    )
    body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
    r = requests.post(url, headers=headers, json=body)
    data = r.json()
    if "error" in data:
        print(f"risk.{col}: ERROR - {json.dumps(data['error'], indent=2)[:200]}")
    else:
        rows = data["results"][0]["tables"][0]["rows"]
        print(f"risk.{col}: {rows[:5]}")
    print()
