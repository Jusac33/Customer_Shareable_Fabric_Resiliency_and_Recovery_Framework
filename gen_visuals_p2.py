"""Generate visuals for the redesigned Claims Deep Dive page (page 2).

Layout:
- Header bar + title
- Row 1: 3 KPI cards (High Severity Claims, Fraud Flagged, Avg Policy Risk Score)
- Row 2: Stacked bar (Claims by State) | Matrix (Severity by Coverage Type)
- Row 3: Claims detail table (full width)
"""
import json
import os

BASE = r"c:\Users\chsaraf\Downloads\Merchant\FABRIC_BCDR\CrestShield-Claim-report.Report\definition\pages\claimsAnalytics\visuals"

CARD_STYLE = {
    "background": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#FFFFFF'"}}}}}}}],
    "border": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#E8ECF0'"}}}}}, "radius": {"expr": {"Literal": {"Value": "8L"}}}}}],
    "dropShadow": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#C8D0D8'"}}}}}, "preset": {"expr": {"Literal": {"Value": "'Custom'"}}}, "shadowBlur": {"expr": {"Literal": {"Value": "8L"}}}, "shadowDistance": {"expr": {"Literal": {"Value": "2L"}}}, "transparency": {"expr": {"Literal": {"Value": "75L"}}}}}]
}

def make_title(text, size="12L", color="'#0F2B46'"):
    return [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "text": {"expr": {"Literal": {"Value": f"'{text}'"}}}, "fontSize": {"expr": {"Literal": {"Value": size}}}, "fontColor": {"solid": {"color": {"expr": {"Literal": {"Value": color}}}}}}}]

visuals = {}

# Header bar
visuals["headerBar"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "p2h1000000000000001",
    "position": {"x": 0, "y": 0, "z": 5000, "height": 72, "width": 1280, "tabOrder": 0},
    "visual": {
        "visualType": "shape",
        "objects": {
            "general": [{"properties": {"keepLayerOrder": {"expr": {"Literal": {"Value": "true"}}}}}],
            "line": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
            "fill": [{"properties": {"fillColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#1A365D'"}}}}}}}]
        },
        "drillFilterOtherVisuals": True
    }
}

# Title
visuals["headerTitle"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "p2t1000000000000002",
    "position": {"x": 30, "y": 14, "z": 6000, "height": 44, "width": 700, "tabOrder": 1},
    "visual": {
        "visualType": "textbox",
        "objects": {"general": [{"properties": {"paragraphs": [{"textRuns": [{"value": "Claims Deep Dive", "textStyle": {"fontFamily": "Segoe UI Semibold", "fontSize": "22px", "color": "#FFFFFF"}}]}]}}]},
        "drillFilterOtherVisuals": True
    }
}

# Accent line
visuals["accentLine"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "p2a1000000000000003",
    "position": {"x": 0, "y": 72, "z": 4999, "height": 3, "width": 1280, "tabOrder": 0},
    "visual": {
        "visualType": "shape",
        "objects": {
            "line": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
            "fill": [{"properties": {"fillColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#E53E3E'"}}}}}}}]
        },
        "drillFilterOtherVisuals": True
    }
}

# 3 KPI cards — severity/risk focused
kpi_defs = [
    ("kpiHighSeverity", "p2k1000000000000004", 30,  "High Severity Claims", "High Severity Claims"),
    ("kpiFraudFlagged",  "p2k1000000000000005", 440, "Fraud Flagged", "Fraud Flagged"),
    ("kpiRiskScore",     "p2k1000000000000006", 850, "Avg Policy Risk Score", "Avg Policy Risk Score"),
]
for folder_name, vid, x, measure, title_text in kpi_defs:
    visuals[folder_name] = {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
        "name": vid,
        "position": {"x": x, "y": 84, "z": 1000, "height": 100, "width": 395, "tabOrder": 3},
        "visual": {
            "visualType": "card",
            "query": {"queryState": {"Values": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": measure}}, "queryRef": f"gold_claims_routed.{measure}", "nativeQueryRef": measure}]}}},
            "objects": {
                "labels": [{"properties": {"color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#1A365D'"}}}}}, "fontSize": {"expr": {"Literal": {"Value": "28L"}}}, "bold": {"expr": {"Literal": {"Value": "true"}}}}}],
                "categoryLabels": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
            },
            "visualContainerObjects": {"title": make_title(title_text, "10L", "'#6B7B8D'"), **CARD_STYLE},
            "drillFilterOtherVisuals": True
        }
    }

# Stacked column chart — Claims by State (top 10)
visuals["chartStackedState"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "p2c1000000000000007",
    "position": {"x": 30, "y": 198, "z": 1000, "height": 255, "width": 600, "tabOrder": 4},
    "visual": {
        "visualType": "clusteredColumnChart",
        "query": {"queryState": {
            "Category": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "incident_state"}}, "queryRef": "gold_claims_routed.incident_state", "nativeQueryRef": "incident_state", "active": True}]},
            "Series": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "predicted_severity"}}, "queryRef": "gold_claims_routed.predicted_severity", "nativeQueryRef": "predicted_severity", "active": True}]},
            "Y": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "Total Claims"}}, "queryRef": "gold_claims_routed.Total Claims", "nativeQueryRef": "Total Claims"}]}
        }},
        "objects": {
            "categoryAxis": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": "8L"}}}}}],
            "valueAxis": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": "8L"}}}}}],
            "legend": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "position": {"expr": {"Literal": {"Value": "'Top'"}}}, "fontSize": {"expr": {"Literal": {"Value": "8L"}}}}}]
        },
        "visualContainerObjects": {"title": make_title("Claims by State & Severity"), **CARD_STYLE},
        "drillFilterOtherVisuals": True
    }
}

# Treemap — Claims Amount by Vehicle Make
visuals["chartTreemapVehicle"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "p2c1000000000000008",
    "position": {"x": 650, "y": 198, "z": 1000, "height": 255, "width": 600, "tabOrder": 5},
    "visual": {
        "visualType": "treemap",
        "query": {"queryState": {
            "Group": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "vehicle_make"}}, "queryRef": "gold_claims_routed.vehicle_make", "nativeQueryRef": "vehicle_make", "active": True}]},
            "Values": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "Total Claim Amount"}}, "queryRef": "gold_claims_routed.Total Claim Amount", "nativeQueryRef": "Total Claim Amount"}]}
        }},
        "objects": {
            "labels": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "fontSize": {"expr": {"Literal": {"Value": "9L"}}}}}],
            "categoryLabels": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "fontSize": {"expr": {"Literal": {"Value": "10L"}}}}}]
        },
        "visualContainerObjects": {"title": make_title("Claim Amount by Vehicle Make"), **CARD_STYLE},
        "drillFilterOtherVisuals": True
    }
}

# Detail table — full width at bottom
visuals["detailTable"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "p2t1000000000000009",
    "position": {"x": 30, "y": 467, "z": 1000, "height": 235, "width": 1220, "tabOrder": 6},
    "visual": {
        "visualType": "pivotTable",
        "query": {"queryState": {
            "Rows": {"projections": [
                {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "claim_id"}}, "queryRef": "gold_claims_routed.claim_id", "nativeQueryRef": "claim_id", "active": True},
                {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "incident_type"}}, "queryRef": "gold_claims_routed.incident_type", "nativeQueryRef": "incident_type"},
                {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "incident_state"}}, "queryRef": "gold_claims_routed.incident_state", "nativeQueryRef": "incident_state"},
                {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "predicted_severity"}}, "queryRef": "gold_claims_routed.predicted_severity", "nativeQueryRef": "predicted_severity"},
                {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "rule_engine_decision"}}, "queryRef": "gold_claims_routed.rule_engine_decision", "nativeQueryRef": "rule_engine_decision"},
                {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "settlement_status"}}, "queryRef": "gold_claims_routed.settlement_status", "nativeQueryRef": "settlement_status"},
            ]},
            "Values": {"projections": [
                {"field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "claim_amount"}}, "Function": 0}}, "queryRef": "Sum(gold_claims_routed.claim_amount)", "nativeQueryRef": "Claim Amount"},
                {"field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "avg_severity_score"}}, "Function": 1}}, "queryRef": "Avg(gold_claims_routed.avg_severity_score)", "nativeQueryRef": "Severity Score"},
            ]}
        }},
        "objects": {
            "subTotals": [{"properties": {"rowSubtotals": {"expr": {"Literal": {"Value": "false"}}}}}],
            "grid": [{"properties": {"gridVertical": {"expr": {"Literal": {"Value": "true"}}}, "gridVerticalColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#E8ECF0'"}}}}}, "rowPadding": {"expr": {"Literal": {"Value": "3L"}}}}}],
            "columnHeaders": [{"properties": {"fontColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#FFFFFF'"}}}}}, "backColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#1A365D'"}}}}}, "fontSize": {"expr": {"Literal": {"Value": "9L"}}}}}],
            "values": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": "9L"}}}}}]
        },
        "visualContainerObjects": {"title": make_title("Claims Detail", "12L", "'#0F2B46'"), **CARD_STYLE},
        "drillFilterOtherVisuals": True
    }
}

for name, visual_def in visuals.items():
    folder = os.path.join(BASE, name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "visual.json"), "w", encoding="utf-8") as f:
        json.dump(visual_def, f, indent=2)
    print(f"Created: {name}")

print(f"\nTotal page 2 visuals: {len(visuals)}")
