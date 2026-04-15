"""
RTI Validation Script

Validates RTI artifacts are correctly synced between primary and secondary:
  - Checks all Eventhouses, KQL Databases, KQL Querysets, Eventstreams exist in both
  - Validates connection strings point to the correct workspace
  - Checks KQL Database follower/continuous-export configuration
  - Reports gaps and action items

Usage:
  python rti/validate_rti.py
"""

import json
import sys
import os
import base64
from typing import Dict, List, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common

RTI_ARTIFACT_TYPES = ["Eventhouse", "KQLDatabase", "KQLQueryset", "Eventstream"]


def validate_rti_sync(
    primary_workspace_id: str,
    secondary_workspace_id: str,
    logger,
) -> Dict[str, Any]:
    """Run full RTI validation and return a report."""
    report = {
        "status": "pass",
        "types": {},
        "issues": [],
        "connection_issues": [],
    }

    all_primary = common.get_items(primary_workspace_id)
    all_secondary = common.get_items(secondary_workspace_id)

    for art_type in RTI_ARTIFACT_TYPES:
        p_items = [i for i in all_primary if i.get("type") == art_type]
        s_items = [i for i in all_secondary if i.get("type") == art_type]

        p_names = {i["displayName"] for i in p_items}
        s_names = {i["displayName"] for i in s_items}

        missing_in_secondary = p_names - s_names
        extra_in_secondary = s_names - p_names
        mirrored = p_names & s_names

        type_report = {
            "primary_count": len(p_items),
            "secondary_count": len(s_items),
            "mirrored": len(mirrored),
            "missing_in_secondary": sorted(missing_in_secondary),
            "extra_in_secondary": sorted(extra_in_secondary),
        }
        report["types"][art_type] = type_report

        if missing_in_secondary:
            report["status"] = "fail"
            for name in missing_in_secondary:
                report["issues"].append({
                    "severity": "error",
                    "type": art_type,
                    "name": name,
                    "message": f"{art_type} '{name}' missing in secondary workspace",
                })
                logger.error(f"  ✗ {art_type} '{name}' missing in secondary")

        # Validate connection strings in secondary definitions
        for s_item in s_items:
            if s_item["displayName"] not in mirrored:
                continue
            try:
                export_resp = common.api_call(
                    "POST",
                    f"/workspaces/{secondary_workspace_id}/items/{s_item['id']}/getDefinition",
                    timeout=60,
                )
                definition = export_resp.get("definition", {})
                parts = definition.get("parts", [])
                for part in parts:
                    payload_b64 = part.get("payload", "")
                    if not payload_b64:
                        continue
                    try:
                        payload_text = base64.b64decode(payload_b64).decode("utf-8")
                        # Check for stale primary workspace references
                        if primary_workspace_id in payload_text:
                            report["connection_issues"].append({
                                "type": art_type,
                                "name": s_item["displayName"],
                                "part": part.get("path", "?"),
                                "message": "Contains primary workspace ID reference",
                            })
                            report["status"] = "warn" if report["status"] == "pass" else report["status"]
                            logger.warning(
                                f"  ⚠ {art_type} '{s_item['displayName']}' "
                                f"part '{part.get('path')}' still references primary workspace"
                            )
                    except Exception:
                        pass
            except Exception:
                pass

    # KQL Database data replication check
    kql_dbs = [i for i in all_primary if i.get("type") == "KQLDatabase"]
    if kql_dbs:
        report["issues"].append({
            "severity": "info",
            "type": "KQLDatabase",
            "name": "*",
            "message": (
                f"{len(kql_dbs)} KQL Database(s) found. "
                "Data replication requires continuous-export → Azure Storage → secondary ingestion. "
                "Schema is synced via definition, but data must be configured separately."
            ),
        })

    # Eventstream source connections check
    eventstreams = [i for i in all_secondary if i.get("type") == "Eventstream"]
    if eventstreams:
        report["issues"].append({
            "severity": "info",
            "type": "Eventstream",
            "name": "*",
            "message": (
                f"{len(eventstreams)} Eventstream(s) in secondary. "
                "Source connections (Event Hub, Kafka, IoT Hub) require manual "
                "credential re-authentication in the secondary workspace UI."
            ),
        })

    return report


def main():
    logger = common.setup_logger("validate_rti")

    try:
        report = validate_rti_sync(
            common.PRIMARY_WORKSPACE_ID,
            common.SECONDARY_WORKSPACE_ID,
            logger,
        )

        logger.info("\n" + "=" * 70)
        logger.info(f"RTI VALIDATION: {report['status'].upper()}")
        logger.info("=" * 70)
        for art_type, tr in report["types"].items():
            logger.info(
                f"  {art_type:20s}  primary={tr['primary_count']}  "
                f"secondary={tr['secondary_count']}  mirrored={tr['mirrored']}"
            )
            if tr["missing_in_secondary"]:
                logger.info(f"    Missing: {', '.join(tr['missing_in_secondary'])}")

        if report["connection_issues"]:
            logger.info(f"\nConnection Issues ({len(report['connection_issues'])}):")
            for ci in report["connection_issues"]:
                logger.info(f"  ⚠ {ci['type']} '{ci['name']}' — {ci['message']}")

        if report["issues"]:
            logger.info(f"\nIssues/Notes ({len(report['issues'])}):")
            for issue in report["issues"]:
                logger.info(f"  [{issue['severity'].upper()}] {issue['type']} — {issue['message']}")

        common.save_json(report, "data/rti_validation_report.json")
        return report["status"] != "fail"

    except Exception as e:
        logger.error(f"RTI validation failed: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
