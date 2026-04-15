"""Generate all visuals for the redesigned Claims Command Center page."""
import json
import os

BASE = r"c:\Users\chsaraf\Downloads\Merchant\FABRIC_BCDR\CrestShield-Claim-report.Report\definition\pages\ba5f0c46009d795b2a74\visuals"

CARD_STYLE = {
    "background": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#FFFFFF'"}}}}}} }],
    "border": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#E8ECF0'"}}}}}, "radius": {"expr": {"Literal": {"Value": "8L"}}}}}],
    "dropShadow": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#C8D0D8'"}}}}}, "preset": {"expr": {"Literal": {"Value": "'Custom'"}}}, "shadowBlur": {"expr": {"Literal": {"Value": "8L"}}}, "shadowDistance": {"expr": {"Literal": {"Value": "2L"}}}, "transparency": {"expr": {"Literal": {"Value": "75L"}}}}}]
}

CHART_STYLE = {
    **CARD_STYLE,
    "padding": [{"properties": {"top": {"expr": {"Literal": {"Value": "5L"}}}, "bottom": {"expr": {"Literal": {"Value": "5L"}}}, "left": {"expr": {"Literal": {"Value": "5L"}}}, "right": {"expr": {"Literal": {"Value": "5L"}}}}}]
}

def make_title(text, size="10L", color="'#6B7B8D'"):
    return [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "text": {"expr": {"Literal": {"Value": f"'{text}'"}}}, "fontSize": {"expr": {"Literal": {"Value": size}}}, "fontColor": {"solid": {"color": {"expr": {"Literal": {"Value": color}}}}}}}]

def make_kpi_card(name, vid, x, measure_name, title_text):
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
        "name": vid,
        "position": {"x": x, "y": 84, "z": 1000, "height": 100, "width": 290, "tabOrder": 3},
        "visual": {
            "visualType": "card",
            "query": {"queryState": {"Values": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": measure_name}}, "queryRef": f"gold_claims_routed.{measure_name}", "nativeQueryRef": measure_name}]}}},
            "objects": {
                "labels": [{"properties": {"color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#0F2B46'"}}}}}, "fontSize": {"expr": {"Literal": {"Value": "28L"}}}, "bold": {"expr": {"Literal": {"Value": "true"}}}}}],
                "categoryLabels": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
            },
            "visualContainerObjects": {"title": make_title(title_text), **CARD_STYLE},
            "drillFilterOtherVisuals": True
        }
    }

visuals = {
    "kpiClaimAmount": make_kpi_card("kpiClaimAmount", "kpi1000000000000004", 335, "Total Claim Amount", "Total Claim Amount"),
    "kpiFraudRate": make_kpi_card("kpiFraudRate", "kpi1000000000000005", 640, "Fraud Rate %", "Fraud Rate %"),
    "kpiAvgSeverity": make_kpi_card("kpiAvgSeverity", "kpi1000000000000006", 945, "Avg Severity Score", "Avg Severity Score"),
}

# Clustered bar chart — Claims by Incident Type
visuals["chartBarIncident"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "cht1000000000000007",
    "position": {"x": 30, "y": 198, "z": 1000, "height": 250, "width": 600, "tabOrder": 4},
    "visual": {
        "visualType": "clusteredBarChart",
        "query": {"queryState": {
            "Category": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "incident_type"}}, "queryRef": "gold_claims_routed.incident_type", "nativeQueryRef": "incident_type", "active": True}]},
            "Y": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "Total Claim Amount"}}, "queryRef": "gold_claims_routed.Total Claim Amount", "nativeQueryRef": "Total Claim Amount"}]}
        }},
        "objects": {
            "dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": {"Literal": {"Value": "'#3B82F6'"}}}}}}}],
            "categoryAxis": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": "9L"}}}}}],
            "valueAxis": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": "9L"}}}, "show": {"expr": {"Literal": {"Value": "true"}}}}}],
            "labels": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "fontSize": {"expr": {"Literal": {"Value": "9L"}}}, "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#44546A'"}}}}}}}]
        },
        "visualContainerObjects": {"title": make_title("Claims Amount by Incident Type", "12L", "'#0F2B46'"), **CHART_STYLE},
        "drillFilterOtherVisuals": True
    }
}

# Donut chart — Claims by Decision
visuals["chartDonutDecision"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "cht1000000000000008",
    "position": {"x": 650, "y": 198, "z": 1000, "height": 250, "width": 600, "tabOrder": 5},
    "visual": {
        "visualType": "donutChart",
        "query": {"queryState": {
            "Category": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "rule_engine_decision"}}, "queryRef": "gold_claims_routed.rule_engine_decision", "nativeQueryRef": "rule_engine_decision", "active": True}]},
            "Y": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "Total Claims"}}, "queryRef": "gold_claims_routed.Total Claims", "nativeQueryRef": "Total Claims"}]}
        }},
        "objects": {
            "labels": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "labelStyle": {"expr": {"Literal": {"Value": "'Both'"}}}}}]
        },
        "visualContainerObjects": {"title": make_title("Claims by Routing Decision", "12L", "'#0F2B46'"), **CHART_STYLE},
        "drillFilterOtherVisuals": True
    }
}

# Column chart — Claims trend by month
visuals["chartColumnTrend"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "cht1000000000000009",
    "position": {"x": 30, "y": 462, "z": 1000, "height": 240, "width": 745, "tabOrder": 6},
    "visual": {
        "visualType": "lineClusteredColumnComboChart",
        "query": {"queryState": {
            "Category": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "claim_date"}}, "queryRef": "gold_claims_routed.claim_date", "nativeQueryRef": "claim_date", "active": True}]},
            "Y": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "Total Claims"}}, "queryRef": "gold_claims_routed.Total Claims", "nativeQueryRef": "Total Claims"}]},
            "Y2": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "Avg Claim Amount"}}, "queryRef": "gold_claims_routed.Avg Claim Amount", "nativeQueryRef": "Avg Claim Amount"}]}
        }},
        "objects": {
            "columnBorder": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "color": {"solid": {"color": {"expr": {"Literal": {"Value": "'#3B82F6'"}}}}}, "strokeWidth": {"expr": {"Literal": {"Value": "1L"}}}}}],
            "dataPoint": [{"properties": {"fill": {"solid": {"color": {"expr": {"Literal": {"Value": "'#3B82F6'"}}}}}}}],
            "lineStyles": [{"properties": {"strokeWidth": {"expr": {"Literal": {"Value": "3L"}}}}}],
            "categoryAxis": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": "8L"}}}}}],
            "valueAxis": [{"properties": {"fontSize": {"expr": {"Literal": {"Value": "8L"}}}}}]
        },
        "visualContainerObjects": {"title": make_title("Claims Volume & Avg Amount Over Time", "12L", "'#0F2B46'"), **CHART_STYLE},
        "drillFilterOtherVisuals": True
    }
}

# Slicer — rule_engine_decision
visuals["slicerDecision"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "slc1000000000000010",
    "position": {"x": 795, "y": 462, "z": 1000, "height": 240, "width": 455, "tabOrder": 7},
    "visual": {
        "visualType": "slicer",
        "query": {"queryState": {"Values": {"projections": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "gold_claims_routed"}}, "Property": "rule_engine_decision"}}, "queryRef": "gold_claims_routed.rule_engine_decision", "nativeQueryRef": "rule_engine_decision", "active": True}]}}},
        "objects": {
            "data": [{"properties": {"mode": {"expr": {"Literal": {"Value": "'Basic'"}}}}}],
            "selection": [{"properties": {"selectAllCheckbox": {"expr": {"Literal": {"Value": "true"}}}, "singleSelect": {"expr": {"Literal": {"Value": "false"}}}}}],
            "items": [{"properties": {"fontColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#44546A'"}}}}}, "fontSize": {"expr": {"Literal": {"Value": "10L"}}}}}],
            "header": [{"properties": {"fontColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#0F2B46'"}}}}}, "fontSize": {"expr": {"Literal": {"Value": "12L"}}}}}]
        },
        "visualContainerObjects": {"title": make_title("Filter by Decision", "12L", "'#0F2B46'"), **CARD_STYLE},
        "drillFilterOtherVisuals": True
    }
}

# Accent line under header
visuals["accentLine"] = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.8.0/schema.json",
    "name": "acc1000000000000011",
    "position": {"x": 0, "y": 72, "z": 4999, "height": 3, "width": 1280, "tabOrder": 0},
    "visual": {
        "visualType": "shape",
        "objects": {
            "line": [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}],
            "fill": [{"properties": {"fillColor": {"solid": {"color": {"expr": {"Literal": {"Value": "'#3B82F6'"}}}}}}}]
        },
        "drillFilterOtherVisuals": True
    }
}

for name, visual_def in visuals.items():
    folder = os.path.join(BASE, name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "visual.json"), "w", encoding="utf-8") as f:
        json.dump(visual_def, f, indent=2)
    print(f"Created: {name}")

print(f"\nTotal visuals: {len(visuals) + 3} (including headerBar, headerTitle, kpiTotalClaims)")
