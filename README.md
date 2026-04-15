# Microsoft Fabric BCDR - Python Repository

**Disaster Recovery and Business Continuity for Microsoft Fabric Artifacts**

A complete, production-ready Python repository for implementing Business Continuity and Disaster Recovery (BCDR) across Microsoft Fabric workspaces deployed in two Azure regions. 

## Overview

This repository provides Python scripts and orchestration tools to:

- **Sync all Fabric artifact types** across primary and secondary workspaces (Lakehouses, Warehouses, Notebooks, Pipelines, Semantic Models, Reports, Dataflows, KQL Databases, Eventstreams, ML Models, etc.)
- **Automate failover** from primary to secondary workspace with data validation
- **Orchestrate failback** to restore primary workspace after disaster recovery
- **Support three data replication strategies**:
  - Active Replication (azcopy continuous sync)
  - OneLake Shortcuts (zero-copy, near-zero RPO)
  - GRS Passthrough (metadata-only for Azure GRS storage)
- **Preserve permissions and role assignments** across regions
- **Remap artifact references** automatically (workspace IDs, OneLake paths, connection strings)
- **Dry-run mode** for safe testing before execution

### What This Repo Covers

✅ **Supported Artifact Types**
- Lakehouses (with three data sync options)
- Data Warehouses (schema + incremental data)
- Notebooks & Spark Job Definitions (with code remapping)
- Data Pipelines (with activity and connection remapping)
- Semantic Models & Reports (with dataset rebinding)
- Dataflows Gen2 (with connection remapping)
- Eventhouses (full BCDR with KQL data replication)
- KQL Databases & Querysets (schema + data sync via Kusto REST API)
- Eventstreams (with destination remapping)
- ML Models, Experiments & Environments
- Workspace & item-level permissions

✅ **Operational Features**
- Parallel execution for performance
- Exponential backoff for resilience
- Comprehensive logging (console + file)
- Detailed sync reports in JSON format
- Dry-run mode for safe planning
- Workspace-level and item-level permission sync
- Scheduled KQL data replication (15min–24hr intervals)
- Connection string audit and auto-fix for RTI artifacts
- Data Assurance validation including KQL table/row count checks

### What This Repo Does NOT Cover

❌ **Out of Scope**
- Azure infrastructure DR (Event Hubs, ADLS, SQL DB replication - handle separately)
- Real-time Dashboards, Org Apps, or OneLake Apps (API limitations)
- Premium dataflows (Power Query M script handling only)
- Eventstream source OAuth credential re-authentication (manual step required)
- Application-level failover/DNS updates (app team responsibility)
- Capacity autoscale or isolation configurations
- Monitor/Cost Management DR

---

## Architecture

```
Primary Region (East US 2)          Secondary Region (Central US)
┌─────────────────────────────────┐ ┌─────────────────────────────────┐
│ Fabric Workspace (Primary)       │ │ Fabric Workspace (Secondary)     │
│ ├─ Lakehouses                   │ │ ├─ Lakehouses                    │
│ ├─ Warehouses                   │ │ ├─ Warehouses                    │
│ ├─ Notebooks & Pipelines        │ │ ├─ Notebooks & Pipelines         │
│ ├─ Semantic Models & Reports    │ │ ├─ Semantic Models & Reports     │
│ ├─ Eventhouses & KQL DBs        │ │ ├─ Eventhouses & KQL DBs         │
│ ├─ Dataflows & Eventstreams     │ │ ├─ Dataflows & Eventstreams      │
│ └─ Other artifacts              │ │ └─ Other artifacts               │
└─────────────────────────────────┘ └─────────────────────────────────┘
         │                                     │
         │  Option 1: azcopy (active)          │
         │  Option 2: Shortcuts (passive)      │
         │  Option 3: GRS (storage-level)      │
         │  KQL: Inline data replication        │
         └─────────────────────────────────────┘

Failover: Primary → Secondary (during disaster)
Failback: Secondary → Primary (after recovery)
```

---

## Prerequisites

### Azure & Fabric Setup

- **Azure Subscription** with two regions (e.g., East US 2 + Central US)
- **Microsoft Fabric Premium Capacity** in each region
  - Minimum SKU: F2 (25 CUs each recommended for non-prod)
  - Production: F64+ (256 CUs) for adequate concurrent operations
- **Two Fabric Workspaces** (one per region)
  - Both created with Premium Capacity assignment
  - Both in the same Azure AD tenant

### Service Principal & Permissions

1. **Create Service Principal** in Azure AD
   - Tenant ID, Client ID, Client Secret
   - Required roles:
     - `Fabric Workspace Admin` (or `Workspace Member` + custom role)
     - Enough permissions to read/write all artifact types
   - Assign to both primary and secondary workspaces

2. **Verify Service Principal Access**
   ```bash
   python common.py  # Will validate credentials
   ```

### Software Requirements

- **Python 3.9+**
- **azcopy** (for Option 1 data sync)
  - Download: https://aka.ms/downloadazcopy
  - Add to PATH so `azcopy --version` works
- **pip packages**
  ```bash
  pip install -r requirements.txt
  # msal, requests, pandas
  ```

### Network & Connectivity

- Network connectivity to `api.fabric.microsoft.com` and `onelake.dfs.fabric.microsoft.com`
- If using corporate proxy, configure in environment
- Service Principal must authenticate via Azure AD in both clouds

---

## Configuration

### 1. Environment Variables

Copy and update the template:

```bash
cp .env.template .env
```

Edit `.env`:
```bash
PRIMARY_WORKSPACE_ID=550e8400-e29b-41d4-a716-446655440000
SECONDARY_WORKSPACE_ID=550e8400-e29b-41d4-a716-446655440100
PRIMARY_CAPACITY_ID=fabric-capacity-primary
SECONDARY_CAPACITY_ID=fabric-capacity-secondary
TENANT_ID=your-tenant.onmicrosoft.com
CLIENT_ID=your-sp-client-id
CLIENT_SECRET=your-sp-client-secret
NUM_THREADS=5
RESPONSE_BACKOFF=2
```

Or set environment variables directly:

```powershell
$env:PRIMARY_WORKSPACE_ID = "550e8400..."
$env:CLIENT_ID = "your-client-id"
# etc.
```

### 2. Artifact Mapping Files

**data/artifact_mapping.csv** — Primary → Secondary artifact ID mappings

| primary_artifact_id | secondary_artifact_id | artifact_type | primary_name | secondary_name |
|---|---|---|---|---|
| 550e8400-... | 660e8400-... | Lakehouse | sales_data | sales_data |

Generate by running initial metadata sync:
```bash
python scripts/sync_workspaces_metadata.py
```

Then populate `artifact_mapping.csv` manually or via post-processing.

**data/connection_mapping.csv** — Connection name mappings

| primary_connection_name | secondary_connection_name | connection_type |
|---|---|---|
| primary_sql_db | secondary_sql_db | SqlServer |
| primary_eventhub | secondary_eventhub | EventHub |

Collect from Workspace Settings → Manage Connections hub.

**data/reference_mapping.csv** — URL/path remapping (workspace IDs, OneLake paths)

| primary_reference | secondary_reference | reference_type |
|---|---|---|
| 550e8400-... (workspace) | 660e8400-... | WorkspaceId |
| onelake.dfs.../550e8400-... | onelake.dfs.../660e8400-... | OneLakeUrl |

### 3. Validate Configuration

```bash
# Test credentials and workspace access
python common.py

# Expected output:
# ✓ Authentication successful
# ✓ Access to primary workspace confirmed
# ✓ Access to secondary workspace confirmed
```

---

## Usage

### Initial DR Setup (Production Deployment)

Run scripts in this order on first deployment:

```bash
# 1. Inventory and validate
python scripts/sync_workspaces_metadata.py
# → Generates: data/primary_artifact_manifest.json, sync_plan.json

# 2. Sync code artifacts (no data)
python scripts/sync_notebooks_and_pipelines.py

# 3. Sync BI layer
python scripts/sync_semantic_models_and_reports.py

# 4. Sync data sources (Lakehouses and Warehouses)
python scripts/sync_lakehouses.py --strategy ONELAKE_SHORTCUTS
python scripts/sync_warehouses.py

# 5. Sync other artifact types  
python scripts/sync_dataflows.py
python scripts/sync_kql_databases.py
python scripts/sync_eventstreams.py
python scripts/sync_ml_models_and_experiments.py

# 6. Sync permissions
python scripts/sync_permissions.py

# 7. Validate everything is replicated
python scripts/sync_workspaces_metadata.py  # Check sync_plan.json
```

### Ongoing Maintenance Sync

Run daily/weekly to keep secondary current:

```bash
# Quick metadata check
python scripts/sync_workspaces_metadata.py

# Incremental sync of changed artifacts
python scripts/sync_notebooks_and_pipelines.py
python scripts/sync_semantic_models_and_reports.py
python scripts/sync_lakehouses.py --strategy ONELAKE_SHORTCUTS

# Permissions audit
python scripts/sync_permissions.py
```

### Testing & Dry-Runs

**Dry-run mode** prints planned actions without executing:

```bash
python scripts/sync_notebooks_and_pipelines.py --dry-run
python scripts/failover.py --dry-run      # Fail-over simulation
python scripts/failback.py --dry-run      # Fail-back simulation
```

### Failover (Activate Secondary)

Invoke when primary workspace experiences a disaster:

```bash
# Step 1: Verify secondary is current
python scripts/sync_workspaces_metadata.py

# Step 2: Execute failover
python scripts/failover.py

# Step 3: Update application connection strings to secondary endpoints
# (Manual step - app teams update config, DNS, etc.)
```

**Output**: `data/failover_log.json` with execution summary

### Failback (Return to Primary)

Once primary is restored:

```bash
# Sync all changes made in secondary back to primary
python scripts/failback.py

# Verify primary is re-established
python scripts/sync_workspaces_metadata.py

# Resume normal operations on primary
```

**Output**: `data/failback_log.json` with execution summary

---

## Script Overview

| Script | Artifacts Covered |
|---|---|
| common.py | Auth, utilities, API helpers |
| sync_notebooks_and_pipelines.py | Code artifacts, connections |
| sync_workspaces_metadata.py | Workspace inventory & planning |
| sync_lakehouses.py | Lakehouse data + metadata |
| sync_ml_models_and_experiments.py | ML models & environments |
| sync_semantic_models_and_reports.py | BI & analytics layer |
| sync_permissions.py | RBAC & access control |
| sync_dataflows.py | Shared/derived data sources |
| sync_kql_databases.py | Real-time analytics (Eventhouse) |
| sync_eventstreams.py | Streaming (Eventstreams) |
| failover.py / failback.py | Orchestration |

---

## Output & Artifacts

All scripts generate outputs in `data/` and `logs/`:

### Sync Reports (data/)

- `primary_artifact_manifest.json` — Full primary workspace inventory
- `secondary_artifact_manifest.json` — Secondary inventory (for comparison)
- `sync_plan.json` — Detailed diff: missing, in-sync, type mismatches
- `artifact_mapping.csv` — Primary → secondary ID mappings (generated/updated)
- `lakehouse_sync_report.json` — Lakehouse sync details
- `warehouse_sync_report.json` — Warehouse schema sync
- `code_sync_report.json` — Notebooks and pipelines
- `bi_sync_report.json` — Semantic models and reports
- `dataflow_sync_report.json` — Dataflows
- `kql_sync_report.json` — KQL databases and querysets
- `eventstream_sync_report.json` — Eventstreams
- `ml_sync_report.json` — ML models and environments
- `permissions_audit.json` — Permission assignments
- `failover_log.json` — Failover execution log
- `failback_log.json` — Failback execution log

### Logs (logs/)

Timestamped log files for each script:
- `sync_workspaces_metadata_YYYYMMDD_HHMMSS.log`
- `sync_lakehouses_YYYYMMDD_HHMMSS.log`
- etc.

---

## Lakehouse Data Sync Strategies

Choose one strategy based on your RPO/RTO requirements:

### Option 1: Active Replication (azcopy)
```bash
python scripts/sync_lakehouses.py --strategy ACTIVE_REPLICATION
```

**How it works**: azcopy syncs data from primary to secondary continuously

**Pros**:
- Full data copy (point-in-time valid)
- Data immediately available in secondary
- Low RTO (data already in place)

**Cons**:
- Network bandwidth intensive
- RPO depends on sync frequency
- Data transfer costs

**RPO/RTO**: RPO = ~last 24 hours (if daily syncs), RTO = minutes

---

### Option 2: OneLake Shortcuts (Recommended)
```bash
python scripts/sync_lakehouses.py --strategy ONELAKE_SHORTCUTS
```

**How it works**: Creates symbolic references in secondary Lakehouse pointing to primary (zero-copy)

**Pros**:
- Zero data copy overhead
- Near-zero RPO (reads go directly to primary live data)
- Minimal cost
- Instant failover readiness

**Cons**:
- Secondary reads depend on primary availability (until failover)
- After failover, must re-point shortcuts or sync data
- Not suitable if primary completely unavailable

**RPO/RTO**: RPO = near-zero (reads current primary data), RTO = seconds (redirect reads)

---

### Option 3: GRS Passthrough
```bash
python scripts/sync_lakehouses.py --strategy GRS_PASSTHROUGH
```

**How it works**: Metadata-only sync; Azure GRS handles data replication at storage layer

**Pros**:
- No application-level data sync
- Natural data redundancy via geo-replication
- Automatic failover ready

**Cons**:
- Requires GRS-enabled Azure Storage accounts (separate from Fabric)
- Sync is Fabric metadata only
- Tight coupling to Azure storage layer

**RPO/RTO**: RPO = GRS replication lag (~minutes), RTO = seconds

---

## RTO/RPO Reference by Artifact Type

| Artifact Type | Sync Method | RPO | RTO |
|---|---|---|---|
| **Lakehouses** | azcopy | 24 hours | 10-60 min |
| | Shortcuts | Near-zero | Seconds |
| | GRS | Minutes | Seconds |
| **Warehouses** | Schema + CTAS | Hours | 30-120 min |
| **Notebooks** | Definition export | Definition age | Minutes |
| **Pipelines** | Definition export + activities | Definition age | Minutes |
| **Semantic Models** | Definition export | Definition age | Minutes |
| **Reports** | Definition export | Definition age | Minutes |
| **Dataflows** | Definition export | Refresh interval | 5-30 min |
| **KQL Databases** | Continuous export | Export lag | 5-60 min |
| **Eventstreams** | Real-time | Source lag | Seconds |
| **Permissions** | RBAC sync | Last sync | Minutes |

---

## Limitations & Known Issues

### API Limitations

- **Real-time Dashboards, Org Apps**: `getDefinition` API not available; manual export required
- **Premium dataflows**: Limited support for non-M-script components
- **Eventstream source OAuth credentials**: Cannot be exported/re-authenticated programmatically; manual step required after failover

### Operational Constraints

1. **Eventstream sources** (Event Hub, IoT Hub, Kafka):
   - Must be pre-provisioned in secondary region
   - This script handles metadata; source re-pointing requires manual OAuth re-authentication
   - Plan 30-60 minutes for credential setup post-failover

2. **Dataflow connections** (Excel, SharePoint, Power Query):
   - Must be pre-configured in secondary workspace before running sync
   - OAuth connections require manual refresh after failing over
   - Use service principal credentials when possible (more portable)

3. **Connection strings in Notebooks**:
   - Hardcoded primary SQL endpoints / API URLs need remapping via `reference_mapping.csv`
   - Incomplete capture may require manual fixes in secondary notebooks

4. **Capacity limits**:
   - Secondary capacity must have sufficient resource headroom to handle primary workload
   - Monitor secondary capacity during initial data sync
   - Consider burst capacity for large migrations

5. **Cross-workspace queries**:
   - May fail if primary workspace becomes unavailable
   - Alternatives: use Lakehouses + shortcuts, or pre-sync all data

### Azure Infrastructure DR (Out of Scope)

This repository handles **Fabric artifact sync only**. You must handle separately:

- **Azure Storage (ADLS/OneLake backend)**: Use GRS or manual replication
- **SQL Server/Azure SQL DB** used as Mirroring sources: Configure geo-replication
- **Event Hubs**: Configure secondary Event Hub namespace + consumer groups
- **Key Vault secrets**: Replicate or pre-create in secondary region
- **Network security**: Firewall rules, private endpoints, etc.

---

## Troubleshooting

### Authentication Errors

```
Failed to acquire token: unauthorized_client
```

**Fix**: 
- Verify `CLIENT_ID`, `CLIENT_SECRET`, `TENANT_ID` in `.env`
- Confirm Service Principal exists in Azure AD
- Ensure Service Principal has Workspace Admin role in both workspaces

### Workspace Access Denied

```
Cannot access workspace 550e8400-...
```

**Fix**:
- Verify workspace GUID is correct
- Check Service Principal has been added to workspace
- Workspace settings → Manage workspace settings → Workspace members

### azcopy Not Found

```
azcopy not found - install via: https://aka.ms/downloadazcopy
```

**Fix**:
```bash
# Windows
winget install azcopy

# macOS
brew install azcopy

# Linux
wget https://aka.ms/downloadazcopy-v10-linux
tar xvf *.tar.gz
sudo ./azcopy /copy-source-to-usr
```

Verify: `azcopy --version`

### Shortcut Creation Fails

```
Error creating shortcut: 403 Forbidden
```

**Fix**:
- Verify secondary Lakehouse exists (create if missing)
- Confirm secondary Lakehouse ID is correct
- Check Service Principal has Workspace Member role and edit permissions

### Dataflow Sync Fails

```
Connection missing: primary_sql_db
```

**Fix**:
- Ensure all connections in `connection_mapping.csv` exist in secondary workspace
- Create missing connections via Workspace Settings → Connections
- For OAuth connections, manually authenticate after creation

### Large Data Sync Timeout

```
Operation timeout after 300 seconds
```

**Fix**:
- Increase `OPERATION_TIMEOUT_SECONDS` in `.env` (e.g., 600 or 1800)
- Reduce `NUM_THREADS` to lighten load
- Run azcopy separately with `--preserve-smb-info` for better performance

---

## Best Practices

### Planning & Preparation

1. **Right-size capacity**: Secondary capacity should match primary for disaster scenarios
   - Monitor actual resource usage in primary to size secondary
   - Ensure sufficient compute concurrency during failover

2. **Pre-test failover**: Run `failover.py --dry-run` monthly
   - Validates all prerequisites are in place
   - Identifies misconfigured mappings
   - Builds team familiarity with process

3. **Document runbooks**: Create step-by-step guides for your team
   - Failover decision criteria & approval workflow
   - Post-failover app team actions (connection string updates, etc.)
   - Failback procedure & validation checklist

4. **Monitor secondary**:
   - Set up alerts on secondary workspace capacity utilization
   - Monitor pipeline execution and refresh failures
   - Validate data freshness regularly

### Operations

1. **Automate metadata syncs**:
   ```bash
   # Schedule daily via cron (Linux) or Task Scheduler (Windows)
   0 2 * * * cd /path/to/fabric-dr && python scripts/sync_workspaces_metadata.py >> logs/cron.log 2>&1
   ```

2. **Version control mapping files**:
   - Check `data/artifact_mapping.csv`, `connection_mapping.csv` into Git
   - Track changes as artifacts are added/removed
   - Tag releases with artifact counts for reference

3. **Maintain changelog**:
   - Document all "new" artifacts created in primary
   - Update mappings before next scheduled sync
   - Review sync_plan.json regularly for drift

4. **Failover validation**:
   - Keep a checklist of critical reports/dashboards to verify post-failover
   - Run smoke tests: refresh semantic models, run sample queries
   - Verify row counts match primary in key tables

### Security

1. **Service Principal credentials**:
   - Store in Azure Key Vault, not in code
   - Rotate credentials periodically
   - Use the same secret in all Fabric workspaces

2. **Limit sync scope**:
   - If possible, create a read-only Service Principal for data validation
   - Use separate SP for failover operations if teams want segregation

3. **Audit logs**:
   - Keep logs for compliance/audit (at least 90 days)
   - Monitor logs for unauthorized access attempts
   - Alert on failed failover/failback operations

---

## FabricGuard Dashboard (Real-Time Monitoring)

A professional, real-time web dashboard for monitoring BCDR operations, replication status, and executing failover commands.

### Overview

**FabricGuard** is a Flask-based web application that provides a command center interface for managing your Fabric BCDR infrastructure. It combines real-time data from sync scripts with interactive controls for failover simulation and workspace management.

### Features

✅ **Command Center Dashboard**
- Live primary/secondary region status cards
- Capacity utilization (SKU level: F2/F4/F64, compute units, %)
- Replication lag monitoring (RPO target vs. actual)
- Sync progress bar with artifact counts (in-sync, missing, mismatched)
- Real-time sentinel event feed with socket-based updates

✅ **Drift Detection**
- Comparison of primary vs. secondary artifacts
- Synchronized artifact counts by type
- Missing artifacts in secondary
- Type mismatches and conflicts

✅ **Data Integrity Validation**
- Row count comparisons between regions
- Data consistency verification
- Schema validation for warehouses
- Last refresh timestamps

✅ **Architecture Visualization**
- Primary/secondary region topology diagrams
- Workspace and artifact inventory display
- Capacity SKU details
- Replication strategy overview

✅ **Failover Simulation**
- Interactive dry-run failover workflow
- Pre-failover checklist validation
- Step-by-step failover execution preview
- Smoke tests simulation (API, Lakehouse, Report, Model tests)
- RTO/RPO estimates

✅ **Sentinel Agent**
- 24-hour event log with filters
- Real-time event feed from sync operations
- Severity levels (Critical, High, Medium, Low, Info)
- Event types (Sync, Health, Error, Warning)
- System health statistics and uptime tracking

### Quick Start

#### 1. Install Flask Dependencies

```bash
pip install flask flask-cors
```

Or reinstall requirements:

```bash
pip install -r requirements.txt
```

#### 2. Update Environment Variables

Ensure `.env` includes:

```bash
# Fabric Workspace IDs
PRIMARY_WORKSPACE_ID=550e8400-...
SECONDARY_WORKSPACE_ID=660e8400-...

# Fabric Capacity IDs
PRIMARY_CAPACITY_ID=770e8400-...
SECONDARY_CAPACITY_ID=880e8400-...

# Azure AD / Service Principal
TENANT_ID=yourtenantid
CLIENT_ID=yourclientid
CLIENT_SECRET=yourclientsecret

# Dashboard
FLASK_ENV=production
FLASK_DEBUG=False
FLASK_PORT=5000
```

#### 3. Run the Dashboard

```bash
python app.py
```

Output:
```
 * Running on http://127.0.0.1:5000
 * Press CTRL+C to quit
```

#### 4. Open in Browser

Navigate to: **http://localhost:5000**

Or access remotely:

```bash
# Allow external connections (development only)
# Edit app.py: app.run(host='0.0.0.0', port=5000, debug=False)

# Then access from another machine:
http://<server-ip>:5000
```

### Dashboard Pages

#### Command Center (/)

Main dashboard with real-time status:

**Top Section**:
- Status banner: "LIVE API" indicator, "ALL SYSTEMS NOMINAL" status
- Workspace inventory: Monitoring X workspaces, Y items protected

**Primary Section**:
1. **Regional Topology Cards** (side-by-side)
   - Primary (East US 2): SKU F4, 256 CUs, Health %, Capacity %, Last Heartbeat
   - Secondary (Central US): SKU F2, 96 CUs, Health %, Capacity %, Last Heartbeat
   - Color-coded: Primary (blue), Secondary (orange)

2. **Replication Status**
   - Current Lag (minutes): 2.1 min
   - RPO Target: 15 min
   - Status Badge: HEALTHY / WARNING

3. **Capacity Controls**
   - Primary F4 and Secondary F2 buttons
   - Pause/Resume buttons
   - View Metrics link

4. **Sentinel Agent Feed**
   - Real-time event log scrollable container
   - Latest events with timestamps, severity, source
   - Auto-scrolls with new events

5. **Fabric Workspace Inventory**
   - Primary workspace items with LIVE badges
   - Secondary workspace items
   - Artifact type counts

6. **Sync Progress**
   - Progress bar showing sync percentage
   - Stats: In-Sync X, Missing Y, Mismatched Z

**Auto-Refresh**: Every 30 seconds from `/api/` endpoints
**Clock**: Real-time HH:MM:SS ET display

#### Drift Detection (/drift)

Artifact comparison between regions:

**Statistics Section**:
- In Sync: Count and percentage
- Missing in Secondary: Count
- Type Mismatches: Count
- Artifacts Only in Secondary: Count

**Drift Table**:
- Artifact Name, Type, Status, Primary ID, Secondary ID
- Sync Action buttons per artifact
- Sortable columns

#### Data Integrity (/integrity)

Validation of data consistency:

**Integrity Report Cards**:
Per major artifact (Lakehouse, Warehouse, Semantic Model):
- Primary Rows/Size
- Secondary Rows/Size
- Variance (row count difference)
- Match Status: ✓ Data Consistent, ✓ Schema Verified, etc.

#### Architecture (/architecture)

Topology and replication strategy:

**Diagram Section**:
- Visual layout: Primary Region ↔ Secondary Region
- Workspace boxes with artifact counts
- Replication method and RPO/RTO targets

**Details Sections**:
- Replication Strategy (method, data path, RPO/RTO)
- Sync Components (step-by-step pipeline)
- Failover Process (6-step checklist)

#### Failover Simulation (/failover)

Interactive DR testing:

**Warning Box**: "This is a DRY-RUN simulation. No actual failover will be performed."

**Pre-Failover Checklist**:
- ✓ Primary workspace accessible
- ✓ Secondary workspace healthy
- ✓ Replication lag < 15 min
- ✓ Artifact mappings valid
- ✓ Secondary capacity available

**Failover Steps** (4-part):
1. Pause Primary Workspace
2. Validate Secondary Consistency (per-artifact status)
3. Activate Secondary Workspace
4. Execute Smoke Tests (4 tests displayed)

**Execution Options**:
- "Run Failover Simulation" button → displays logs
- View Last Failover Log button

**RTO/RPO Reference**:
- Estimated Recovery Time: 45 seconds
- Recovery Point Objective: 2.1 minutes (current lag)

#### Sentinel Agent (/sentinel)

Event log and monitoring:

**Filter Controls**:
- Event Type: All, Sync, Health, Error, Warning, Info
- Severity: All, Critical, High, Medium, Low, Info
- Time Range: Last 1/6/24 hours, 7 days

**Statistics Cards** (top):
- Total Events: 247
- Critical Alerts: 0 (System Nominal)
- Warnings: 3 (Investigation Recommended)
- Uptime: 95.8% (Above SLA)

**Event Feed** (scrollable):
Each event shows:
- Timestamp (5:32 PM ET)
- Severity Badge (color-coded)
- Title, Source Module, Message
- Details (workspace, region, artifact, metric)
- Details action button

### API Endpoints

The Flask app exposes 70+ JSON endpoints. Key categories:

| Category | Endpoints | Description |
|----------|-----------|-------------|
| Auth | `/api/auth/status`, `/api/auth/start` | MSAL interactive + Service Principal login |
| BCDR Status | `/api/bcdr/status`, `/api/topology` | Workspace health, replication lag, artifact drift |
| Inventory | `/api/inventory`, `/api/active-pair-info` | Artifact counts by type with mirroring percentage |
| Replication | `/api/bcdr/replicate`, `/api/bcdr/replicate-item` | Background artifact replication with progress polling |
| Lakehouse Data | `/api/bcdr/azcopy-replicate`, `/api/bcdr/azcopy-status` | azcopy full copy / incremental sync for lakehouse data |
| ML Model/Experiment | `/api/bcdr/ml-status`, `/api/bcdr/ml-replicate` | ML item creation + OneLake data sync via azcopy |
| Scheduling | `/api/bcdr/schedule`, `/api/bcdr/azcopy-schedule` | Notebook sync schedule + azcopy incremental schedule |
| Auto-Sync | `/api/bcdr/autosync` | Auto-replicate new artifacts (30s–10min watcher) |
| Drift & Integrity | `/api/sync-plan`, `/api/bcdr/lakehouse-tables` | Operational drift, table-level comparison |
| Failover | `/api/failover/simulate`, `/api/failover/execute`, `/api/failback/execute` | DR simulation and execution |
| Lineage | `/api/lineage`, `/api/lineage/connections` | Dependency graph from artifact definitions |
| RTI | `/api/rti/status`, `/api/rti/sync`, `/api/rti/replicate-kql-data` | Real-Time Intelligence BCDR |
| DevOps | `/api/devops/status`, `/api/devops/trigger` | Azure DevOps pipeline integration |
| Gateways | `/api/gateways` | On-premises data gateway discovery |
| Workspace Pairs | `/api/workspace-pairs`, `/api/workspaces/select` | Multi-pair config, active pair switching |
| Permissions | `/api/bcdr/sync-permission` | Role assignment sync to secondary |

---

## Dashboard Pages (Complete Reference)

| Route | Page | Purpose |
|-------|------|---------|
| `/` | Command Center | Real-time overview: health, lag, inventory cards with Sync dropdown per artifact type |
| `/topology` | Regional Topology | Workspace pair cards, replication lag/source, dependency graph, artifact pairs table |
| `/inventory` | Inventory | Side-by-side primary/secondary workspace cards with item type breakdown |
| `/lakehouse` | Lakehouse Replication | azcopy full copy / incremental sync, scheduled azcopy, ML model data panel |
| `/drift` | Drift Detection | In-sync / missing / extra / type mismatch counts with per-artifact Fix buttons |
| `/integrity` | Data Integrity | Table-level row count validation, KQL database checks |
| `/failover` | Failover Simulation | Pre-flight checks, simulate or execute failover/failback |
| `/rti` | Real-Time Intelligence | Eventhouse/KQL database sync, data replication, scheduled KQL sync |
| `/setup` | Setup | Configure workspace pairs, authenticate, select active pair |
| `/gateways` | Data Gateways | On-premises gateway discovery with members and data sources |
| `/devops` | DevOps Integration | Azure DevOps pipeline config, trigger, and run history |
| `/architecture` | Architecture Diagram | Visual topology, replication strategy, failover process |

---

## ML Model & Experiment Sync

ML Models and Experiments store data in OneLake (model weights, MLflow artifacts, experiment runs) and require special handling:

### How It Works

1. **Definition export + remapping**: The MLModel definition contains `ArtifactObjectId` referencing the parent MLExperiment. `_build_connection_map()` remaps this to the secondary experiment ID automatically.

2. **Create in secondary**: If the model doesn't exist in secondary, the API creates it with the remapped definition.

3. **azcopy data copy with `.platform` exclusion**: Model files (weights, config, conda.yaml, MLmodel, etc.) are copied via azcopy. **The `.platform` file is excluded** (`--exclude-pattern=.platform`) because it contains the item's identity (workspace ID, item ID). Overwriting it causes Fabric to delete the item.

4. **Artifact mapping CSV update**: After creation, `artifact_mapping.csv` is updated with the new secondary ID.

### Code Paths

| Trigger | Code Path | Description |
|---------|-----------|-------------|
| Dashboard "Sync" button on MLModel card | `replicate_items_by_type("MLModel")` | Creates item + azcopy data |
| Lakehouse page → ML panel → Full Copy | `api_ml_replicate` | Auto-creates missing items + azcopy data |
| Lakehouse page → ML panel → Incremental Sync | `api_ml_replicate` (mode=sync) | azcopy sync (delta only) |
| Scheduled azcopy sync | `_azcopy_schedule_tick` | Incremental sync for paired items |
| CLI script | `scripts/sync_ml_models_and_experiments.py` | Full sync with definition remapping |

### Critical: `.platform` File Exclusion

Every Fabric item has a `.platform` file in OneLake containing its identity metadata. **Never overwrite this file** during azcopy — it will cause Fabric to delete the target item. All azcopy commands for ML items include:

```
--exclude-pattern=.platform
```

---

## Azcopy Scheduled Incremental Sync

The azcopy schedule provides automated data replication without requiring Fabric notebook/pipeline infrastructure.

### Configuration

| Setting | Range | Default | Description |
|---------|-------|---------|-------------|
| Interval | 5 min – 24 hours | 15 min | How often azcopy runs |
| Include ML | true/false | true | Also sync ML Model/Experiment artifacts |

### How It Works

1. Background `threading.Timer` runs `_azcopy_schedule_tick()` at the configured interval
2. For each lakehouse pair: `azcopy sync` (incremental, `--delete-destination=false`)
3. If "Include ML" is enabled: `azcopy sync` for each ML Model/Experiment pair (with `.platform` exclusion)
4. State persisted to `.azcopy_schedule.json` — survives server restarts
5. Lag calculation includes azcopy runs (`_azcopy_state["last_run"]`)

### Enable from Dashboard

Lakehouse page → Lakehouse Data Replication panel → "Scheduled azcopy Sync" toggle

### API

```
GET  /api/bcdr/azcopy-schedule     → current state
POST /api/bcdr/azcopy-schedule     → { "enabled": true, "interval_minutes": 15, "include_ml": true }
```

---

## Dependency Graph (Lineage)

The topology page includes a dependency graph showing which artifacts reference which. This allows detecting stale connections (secondary items still pointing to primary IDs).

### How It Works

1. `/api/lineage/connections` inspects up to 50 artifact definitions in the secondary workspace
2. For each item, exports its definition via `getDefinition` API (base64 parts)
3. Scans for GUIDs matching any known item ID
4. Builds a connection graph: source → target
5. Non-notebook items (DataAgent, Eventhouse, KQLDatabase, SemanticModel, Report, MLExperiment, MLModel) are inspected first (higher priority)
6. Results cached for 30 minutes (`_LINEAGE_CACHE_TTL = 1800`)

### Visualization

- **Left-to-right HTML card flow** with 4 columns: Data Stores → Compute → SemanticModel → Consumers
- Each node is a card with icon, name, and type
- Connections drawn as SVG bezier curves
- Stale connections (pointing to primary IDs) flagged red

### Grouped Connections Table

Below the graph, connections are grouped by artifact type in collapsible sections. Each type (DataAgent, KQLDatabase, SemanticModel, Report, Notebook, etc.) has its own section with expandable cards showing target references.

---

## Definition Remapping

When replicating artifacts, embedded references to primary workspace items must be rewritten to secondary equivalents.

### `_build_connection_map(primary_ws_id, secondary_ws_id)`

Builds a `Dict[str, str]` replacement map:
- Primary workspace ID → Secondary workspace ID
- Every item ID matched by `displayName` (all types: Lakehouse, MLExperiment, SemanticModel, KQLDatabase, etc.)

### `_rewrite_definition_parts(parts, replacements)`

For each definition part (base64-encoded):
1. Base64 decode
2. String replace all primary references with secondary equivalents
3. Base64 re-encode

This handles:
- Notebook lakehouse references
- Pipeline activity connection strings
- MLModel experiment ID references
- DataAgent datasource artifact/workspace IDs
- SemanticModel data source configurations

---

## Replication Lag Calculation

The topology page shows real-time replication lag based on the most recent data sync event. Sources are checked in priority order:

| Priority | Source | Description |
|----------|--------|-------------|
| 1 | `_schedule_state` | Scheduled notebook sync runs |
| 2 | `_autosync_state` | Auto-sync watcher checks |
| 3 | `_azcopy_state` | Manual azcopy runs (lakehouse/ML) |
| 4 | `_azcopy_schedule_state` | Scheduled azcopy incremental sync |
| 5 | Notebook job history | `BCDR_Data_Replication` notebook completed runs via Fabric API |

The most recent timestamp wins. Lag displayed as minutes since last sync. Status thresholds:
- **HEALTHY**: < 15 min
- **WARNING**: 15–60 min  
- **STALE**: > 60 min or never synced

---

## File Reference

### Scripts (`scripts/`)

| Script | Description |
|--------|-------------|
| `sync_workspaces_metadata.py` | Discovers primary/secondary items, generates mapping CSVs |
| `sync_lakehouses.py` | Lakehouse sync (definition + shortcut/azcopy data) |
| `sync_notebooks_and_pipelines.py` | Notebooks & Pipelines with connection remapping |
| `sync_semantic_models_and_reports.py` | SM + Reports with dataset rebinding |
| `sync_warehouses.py` | Warehouse schema sync |
| `sync_dataflows.py` | Dataflow Gen2 sync |
| `sync_eventstreams.py` | Eventstream sync with destination remapping |
| `sync_kql_databases.py` | KQL Database schema + data replication |
| `sync_ml_models_and_experiments.py` | ML Models, Experiments, Environments, Data Agents |
| `sync_permissions.py` | Workspace role assignments |
| `failover.py` | Execute failover: pause primary → activate secondary |
| `failback.py` | Execute failback: restore primary from secondary |
| `compare_structure.py` | Compare primary/secondary structure |
| `discover_ml.py` | Discover ML items and OneLake structure |

### RTI (`rti/`)

| File | Description |
|------|-------------|
| `sync_rti.py` | Eventhouse, KQL Database, Eventstream sync engine |
| `validate_rti.py` | Validate RTI artifacts post-sync |
| `create_dummy_rti.py` | Create test Eventhouse + KQL DB for validation |

### Data Files (`data/`)

| File | Description |
|------|-------------|
| `artifact_mapping.csv` | Primary → Secondary item ID mapping (8 columns: primary_artifact_id, secondary_artifact_id, artifact_type, primary_name, secondary_name) |
| `connection_mapping.csv` | Connection name mappings between regions |
| `reference_mapping.csv` | URL/path/workspace ID remapping |
| `ml_sync_report.json` | Last ML sync run report |

### State Files (auto-generated)

| File | Description |
|------|-------------|
| `.workspace_state.json` | Active workspace pair configuration |
| `.sync_schedule.json` | Notebook sync schedule (enabled, interval) |
| `.azcopy_schedule.json` | Azcopy sync schedule (enabled, interval, include_ml) |
| `.autosync_state.json` | Auto-sync watcher state |
| `.azcopy_state.json` | Last azcopy run timestamp (persisted for lag calc) |
| `.defcheck_state.json` | Definition check watcher state |
| `.rti_schedule.json` | RTI KQL data replication schedule |
| `.rti_watermarks.json` | KQL table ingestion watermarks |
| `.devops_config.json` | Azure DevOps pipeline configuration |
| `.msal_cache.bin` | MSAL token cache (encrypted) |

---

## Troubleshooting

### ML Model Disappears After azcopy

**Cause**: azcopy copied the `.platform` file from primary, overwriting the secondary item's identity. Fabric detects the item as foreign and deletes it.

**Fix**: All ML azcopy commands now include `--exclude-pattern=.platform`. If you ran an older version, re-create the model via the ML panel (it will auto-create + copy data).

### Dependency Graph Not Loading

**Cause**: The `/api/lineage/connections` API inspects artifact definitions via LRO (long-running operations). On a cold cache this takes 2–4 minutes for 20 items.

**Fix**: The browser timeout is set to 5 minutes. Wait for it to complete. Subsequent loads use the 30-minute cache.

### Replication Lag Shows "Never Synced" After azcopy

**Cause**: Prior to the persistence fix, `_azcopy_state` was lost on server restart.

**Fix**: azcopy state is now persisted to `.azcopy_state.json`. Run any azcopy operation and the lag will update.

### MLModel Shows "MISSING" in Artifact Pairs

**Cause**: The secondary MLModel was deleted (see `.platform` issue above) or was never created.

**Fix**: Go to Lakehouse page → ML panel → click "Full Copy". This will:
1. Export definition from primary
2. Remap experiment IDs and workspace references
3. Create in secondary via Fabric API
4. Copy OneLake data via azcopy (excluding `.platform`)
5. Update `artifact_mapping.csv`

### Schema-Enabled Lakehouse Tables Not Syncing

**Cause**: Schema-enabled lakehouses use `Tables/{schema}/{table}` paths instead of `Tables/{table}`.

**Fix**: The system auto-detects schema-enabled lakehouses via the `defaultSchema` property and handles path resolution accordingly.

#### GET /api/health

System health overview:

```json
{
  "status": "OPERATIONAL",
  "primary_health_percent": 98,
  "secondary_health_percent": 97,
  "replication_lag_minutes": 2.1,
  "last_sync_time": "2024-01-15T17:35:00Z",
  "sync_direction": "PRIMARY_TO_SECONDARY"
}
```

#### GET /api/topology

Regional topology details:

```json
{
  "regions": [
    {
      "id": "primary",
      "name": "East US 2",
      "sku": "F4",
      "capacity_units": 256,
      "health": 98,
      "capacity_percent": 34,
      "lastHeartbeat": "2024-01-15T17:35:22Z"
    },
    {
      "id": "secondary",
      "name": "Central US",
      "sku": "F2",
      "capacity_units": 96,
      "health": 97,
      "capacity_percent": 8,
      "lastHeartbeat": "2024-01-15T17:35:18Z"
    }
  ]
}
```

#### GET /api/inventory

Artifact inventory by workspace:

```json
{
  "PRIMARY_WORKSPACE": [
    {"name": "sales_data", "type": "Lakehouse", "id": "550e8400-..."},
    {"name": "analytics_dw", "type": "Warehouse", "id": "660e8400-..."}
  ],
  "SECONDARY_WORKSPACE": [
    {"name": "sales_data", "type": "Lakehouse", "id": "770e8400-..."}
  ]
}
```

#### GET /api/logs

Event logs from sync operations:

```json
[
  {
    "timestamp": "2024-01-15T17:35:22Z",
    "severity": "INFO",
    "source": "sync_lakehouses.py:247",
    "title": "Sync Cycle Completed",
    "message": "Lakehouse sync completed in 4.2 seconds",
    "details": {"items": 3, "status": "success"}
  }
]
```

#### GET /api/sync-plan

Latest sync plan comparison:

```json
{
  "inSync": 18,
  "missing": 0,
  "mismatched": 0,
  "lastUpdated": "2024-01-15T17:35:00Z",
  "syncDirection": "PRIMARY_TO_SECONDARY"
}
```

#### POST /api/failover/simulate

Dry-run failover validation:

```bash
curl -X POST http://localhost:5000/api/failover/simulate
```

Response:
```json
{
  "validation": "PASSED",
  "steps":["pause_primary", "validate_secondary", "activate", "smoke_tests"],
  "rto_seconds": 45,
  "rpo_minutes": 2.1
}
```

#### POST /api/refresh

Force cache refresh:

```bash
curl -X POST http://localhost:5000/api/refresh
```

### Deployment

#### Local Development

```bash
python app.py
# http://localhost:5000
```

#### Production Deployment

**Option 1: Gunicorn (Recommended)**

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 --timeout 120 app:app
```

**Option 2: Docker**

Create `Dockerfile`:

```dockerfile
FROM python:3.9
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
```

```bash
docker build -t fabric-bcdr .
docker run -p 5000:5000 --env-file .env fabric-bcdr
```

**Option 3: Azure App Service**

```bash
# Create app service
az appservice plan create --name myplan --resource-group myrg --sku B1 --is-linux
az webapp create --name myapp --resource-group myrg --plan myplan --runtime "python|3.9"

# Deploy
git remote add azure ...
git push azure main
```

### Customization

**Styling**: Modify `static/style.css`
- Dark theme, color scheme, layout spacing
- Override CSS variables in `:root` block

**JavaScript**: Extend `static/script.js`
- Add custom chart types via `buildChartConfig()`
- Add new action handlers in `handleAction()`

**Templates**: Edit `templates/*.html`
- Add new pages by creating new HTML file inheriting from `base.html`
- Add route in `app.py`

### Troubleshooting

**Dashboard shows "Connection Refused"**:
```
Error: Cannot fetch /api/health
```

Fix:
- Ensure app.py is running: `python app.py`
- Check .env variables are set correctly
- Verify Service Principal has workspace access

**Dashboard shows old data (not refreshing)**:
```
Data timestamp: 30 minutes ago
```

Fix:
- API endpoint may be failing silently
- Check browser console for JavaScript errors
- Check Flask logs for API errors
- Manually click Refresh button
- Increase refresh interval in script.js if needed

**"ALL SYSTEMS NOMINAL" but capacity shows 0%**:

Fix:
- Primary/secondary capacity IDs may be incorrect in .env
- Run sync scripts to populate recent data
- Wait 30 seconds for auto-refresh

---

## Examples

See `examples/` folder for minimal, self-contained examples:

### clone_lakehouse_simple.py

Minimal azcopy example - clones one Lakehouse without external dependencies.

```bash
python examples/clone_lakehouse_simple.py
```

### create_shortcuts.py

Minimal API example - authenticates and creates OneLake shortcuts.

```bash
python examples/create_shortcuts.py
```

Both examples have hardcoded values; update with your workspace GUIDs.

---

## Performance Tuning

### Parallel Execution

Adjust `NUM_THREADS` in `.env` based on your needs:

```bash
NUM_THREADS=1      # Sequential (slow, but safe)
NUM_THREADS=5      # Balanced (default)
NUM_THREADS=20     # Aggressive (may hit throttles)
```

Monitor Fabric API throttling (429 responses) and reduce if needed.

### azcopy Performance

For large data syncs, use azcopy concurrency:

```bash
# In sync_lakehouses.py (ACTIVE_REPLICATION), add:
azcopy sync source dest --recursive --trusted-microsoft-suffixes=onelake.dfs.fabric.microsoft.com --concurrency=32
```

Monitor network bandwidth and throttling.

### Batch Processing

For 1000+ artifacts, split runs:

```bash
# Sync lakehouses only
python scripts/sync_lakehouses.py

# Sync notebooks only (separate call)
python scripts/sync_notebooks_and_pipelines.py
```

---

## Contributing

Contributions welcome! Please:

1. Test changes with `--dry-run` first
2. Add unit tests for new functions
3. Update this README with new capabilities
4. Follow PEP 8 style (run `black` + `flake8`)

---

## Support & Community

- **Issues**: File bugs/feature requests on this repo
- **Discussions**: Ask questions in Discussions tab

---

## License

MIT License - See LICENSE file

---

## Helpful Links

- [Fabric API Documentation](https://learn.microsoft.com/en-us/rest/api/fabric/)
- [Fabric Capacity Documentation](https://learn.microsoft.com/en-us/fabric/enterprise/capacity-management)
- [OneLake Shortcuts](https://learn.microsoft.com/en-us/fabric/onelake/onelake-shortcuts)
- [Failover Best Practices](https://learn.microsoft.com/en-us/azure/availability-zones/)
- [Azure CLI for Fabric](https://learn.microsoft.com/en-us/cli/azure/service-page/fabric%20(Microsoft%20Fabric)%20preview)

---

**Last Updated**: March 2026

**Maintainer**: Microsoft Fabric Platform Engineering

**Version**: 1.0.0
