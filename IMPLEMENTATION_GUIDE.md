# Fabric BCDR — End-to-End Implementation Guide

## Table of Contents

1. [What This Project Does](#1-what-this-project-does)
2. [Architecture Overview](#2-architecture-overview)
3. [Repository Structure](#3-repository-structure)
4. [Prerequisites](#4-prerequisites)
5. [Step-by-Step Setup](#5-step-by-step-setup)
6. [Authentication Flow](#6-authentication-flow)
7. [Workspace Pair Configuration](#7-workspace-pair-configuration)
8. [Artifact Replication Engine](#8-artifact-replication-engine)
9. [Lakehouse Data Sync (Delta Tables)](#9-lakehouse-data-sync-delta-tables)
10. [Schema-Enabled Lakehouses & Table Registration](#10-schema-enabled-lakehouses--table-registration)
11. [Auto-Sync Watcher](#11-auto-sync-watcher)
12. [Scheduled Sync](#12-scheduled-sync)
13. [Operational Drift Analysis](#13-operational-drift-analysis)
14. [Data Assurance Validation](#14-data-assurance-validation)
15. [Replication Lag Calculation](#15-replication-lag-calculation)
16. [Regional Topology & Multi-Workspace Pairs](#16-regional-topology--multi-workspace-pairs)
17. [Managed Failover & Failback](#17-managed-failover--failback)
18. [Data Gateways](#18-data-gateways)
19. [CLI Sync Scripts (Standalone)](#19-cli-sync-scripts-standalone)
20. [API Reference](#20-api-reference)
21. [Dashboard Pages](#21-dashboard-pages)
22. [Security Considerations](#22-security-considerations)
23. [OneLake Security — Data Access Roles (RLS/CLS)](#23-onelake-security--data-access-roles-rlscls)
24. [ML Model & Experiment BCDR](#24-ml-model--experiment-bcdr)
25. [Real-Time Intelligence (RTI) BCDR](#25-real-time-intelligence-rti-bcdr)
26. [Roadmap — Data Warehouse BCDR](#26-roadmap--data-warehouse-bcdr)
27. [Environment BCDR](#27-environment-bcdr)
28. [Delta-Only Sync (Permissions, RTI, Definitions)](#28-delta-only-sync-permissions-rti-definitions)
29. [Bulk Item Definition APIs (Beta)](#29-bulk-item-definition-apis-beta)
30. [Out-of-Definition Settings — Gap Analysis](#30-out-of-definition-settings--gap-analysis)
31. [Script Consolidation & Delegation](#31-script-consolidation--delegation)
32. [Test Coverage](#32-test-coverage)

---

## 1. What This Project Does

This project implements **Business Continuity and Disaster Recovery (BCDR)** for Microsoft Fabric workspaces. It provides two modes of operation:

| Mode | Entry Point | Purpose |
|------|------------|---------|
| **Web Dashboard** | `app.py` (Flask) | Interactive browser-based control center with real-time monitoring, one-click replication, auto-sync, drift analysis, and failover simulation |
| **CLI Scripts** | `scripts/*.py` | Standalone Python scripts for automated/scheduled sync of specific artifact types using a Service Principal |

### What Does BCDR Mean Here?

You have a **Primary** Fabric workspace (production, e.g. East US 2) and a **Secondary** workspace (DR target, e.g. Central US). This project:

1. **Replicates artifacts** — Copies Lakehouses, Notebooks, SemanticModels, Reports, DataPipelines, Environments, DataAgents, Ontologies from primary → secondary
2. **Rewrites connections** — Automatically updates workspace IDs, item IDs, and connection strings inside SemanticModel/Report definitions so they point to the secondary workspace's lakehouses
3. **Syncs data** — Generates a PySpark notebook deployed to the secondary workspace. Supports two sync engines: `fast_copy` (default, uses `notebookutils.fs.cp` — stays within Microsoft's network, no bytes leave OneLake) and `spark_cdf` (legacy, uses Delta Change Data Feed + Spark merge). Auto-mode runs a full copy on first execution and switches to incremental on subsequent runs using per-table watermarks stored as JSON in `Files/_bcdr_sync_state/<lh_name>.json` on the secondary lakehouse
4. **Monitors drift** — Continuously compares primary vs secondary to detect missing items, type mismatches, permission differences, and sensitivity label changes; provides inline Fix/Sync buttons
5. **Tracks lag** — Computes real replication lag from notebook job history, schedule timestamps, and auto-sync events
6. **Enables failover** — Provides pre-flight checklist and dry-run simulation for managed failover from primary → secondary
7. **Syncs permissions (delta-only)** — Detects added, changed, removed, and unchanged workspace role assignments; only applies the delta (POST new, PATCH changed, skip unchanged)
8. **Replicates OneLake security (delta-only)** — Scans Data Access Roles (RLS/CLS) on primary lakehouses, normalizes and compares with secondary, and only PUTs when roles differ
9. **Environment BCDR** — Exports environment definitions (Spark runtime, libraries, compute config), replicates to secondary, and triggers publish to install libraries
10. **Bulk definition sync** — Uses Fabric's new Bulk Export/Import Item Definition APIs (beta) to sync all workspace definitions in 2 API calls instead of 2×N, with automatic per-item fallback

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Fabric BCDR Dashboard                            │
│                     (Flask on localhost:5000)                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐     │
│   │ Command  │    │ Drift    │    │ Data     │    │ Managed  │     │
│   │ Center   │    │ Analysis │    │ Assurance│    │ Failover │     │
│   └────┬─────┘    └────┬─────┘    └────┬─────┘    └────┬─────┘     │
│        │               │               │               │            │
│   ┌────▼───────────────▼───────────────▼───────────────▼────┐      │
│   │              Fabric REST API Layer                        │      │
│   │   • GET  /workspaces/{id}/items  (list artifacts)        │      │
│   │   • POST /workspaces/{id}/items  (create artifact)       │      │
│   │   • POST /workspaces/{id}/items/{id}/getDefinition       │      │
│   │   • GET  /workspaces/{id}/roleAssignments                │      │
│   │   • POST /workspaces/{id}/items/{id}/jobs/instances      │      │
│   └────┬────────────────────────────────────────────┬───────┘      │
│        │                                             │              │
│   ┌────▼─────┐                               ┌──────▼──────┐      │
│   │ PRIMARY  │  ◄── artifact replication ──►  │ SECONDARY   │      │
│   │ Workspace│  ◄── data sync (notebook) ──►  │ Workspace   │      │
│   │ (Prod)   │  ◄── connection rewriting ──►  │ (DR Target) │      │
│   └────┬─────┘                               └──────┬──────┘      │
│        │                                             │              │
│   ┌────▼─────┐                               ┌──────▼──────┐      │
│   │ OneLake  │  ◄── Delta table incremental ─►│ OneLake     │      │
│   │ DFS API  │       sync via PySpark         │ DFS API     │      │
│   └──────────┘                               └─────────────┘      │
│                                                                      │
│   Background Timers:                                                 │
│   • Auto-sync watcher (30s–10min interval)                          │
│   • Scheduled sync (5min–24hr interval)                             │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘

Auth: MSAL interactive browser login → Azure PowerShell public client
      Token persisted to .msal_token_cache.bin for session continuity
```

---

## 3. Repository Structure

```
FABRIC_BCDR/
├── app.py                          # Flask web dashboard (3,200+ lines, self-contained)
├── common.py                       # Shared utilities for CLI sync scripts
├── pyproject.toml                  # Python dependencies (Flask, MSAL, requests, pandas)
├── .env.template                   # Environment variable template for CLI scripts
├── .gitignore                      # Excludes secrets, local state, caches
├── .python-version                 # Python version pinning
├── uv.lock                         # uv lockfile for reproducible installs
│
├── templates/                      # Jinja2 HTML templates
│   ├── base.html                   #   Layout: sidebar nav, header, footer
│   ├── login.html                  #   Microsoft interactive sign-in
│   ├── setup.html                  #   Workspace pair configuration (multi-pair)
│   ├── dashboard.html              #   Command Center — artifact cards, sync controls
│   ├── drift.html                  #   Operational Drift Analysis — collapsible groups
│   ├── integrity.html              #   Data Assurance — table counts, type counts, KQL row counts
│   ├── topology.html               #   Regional Topology — health, lag, all-pairs table
│   ├── inventory.html              #   Workspace Inventory — per-pair artifact breakdown
│   ├── failover.html               #   Managed Failover & Failback — checklist, execute, RPO/RTO, event history
│   ├── gateways.html               #   Data Gateways — on-prem gateway discovery, members, data sources
│   ├── rti.html                    #   Real-Time Intelligence — Eventhouse/KQL sync, data replication, scheduling
│   ├── architecture.html           #   Static architecture diagram
│   └── error.html                  #   Error display
│
├── static/
│   ├── style.css                   # Dashboard CSS (dark sidebar, cards, responsive)
│   ├── script.js                   # Chart.js integration, auto-refresh, notifications
│   └── img/fabric-logo.svg         # Microsoft Fabric logo
│
├── scripts/                        # CLI sync pipeline (uses common.py + Service Principal)
│   ├── sync_workspaces_metadata.py #   Full inventory sync
│   ├── sync_notebooks_and_pipelines.py
│   ├── sync_semantic_models_and_reports.py
│   ├── sync_lakehouses.py          #   3 strategies: azcopy / shortcuts / GRS
│   ├── sync_warehouses.py
│   ├── sync_permissions.py         #   Delta-only workspace + item + OneLake DAR sync
│   ├── sync_dataflows.py
│   ├── sync_eventstreams.py        #   Thin wrapper → delegates to rti/sync_rti.py
│   ├── sync_kql_databases.py       #   Thin wrapper → delegates to rti/sync_rti.py
│   ├── sync_ml_models_and_experiments.py  # Delegates env sync to sync_environments.py
│   ├── sync_environments.py        #   Environment BCDR with getDefinition + publish
│   ├── bulk_sync.py                #   Bulk Export/Import Definition APIs (beta) with per-item fallback
│   ├── failover.py                 #   Orchestrated failover (pause → sync → validate → activate)
│   └── failback.py                 #   Reverse sync: secondary → primary
│
├── rti/                            # Real-Time Intelligence BCDR modules
│   ├── __init__.py                 #   Package init
│   ├── sync_rti.py                 #   Standalone RTI artifact sync script
│   ├── validate_rti.py             #   Standalone RTI validation script
│   └── create_dummy_rti.py         #   Create dummy RTI artifacts for testing
│
├── examples/                       # Minimal standalone examples
│   ├── clone_lakehouse_simple.py   #   azcopy-based lakehouse clone
│   └── create_shortcuts.py         #   OneLake shortcut creation
│
├── data/                           # Mapping files for CLI scripts
│   ├── artifact_mapping.csv        #   primary_id → secondary_id
│   ├── connection_mapping.csv      #   connection name remapping
│   └── reference_mapping.csv       #   workspace/capacity/URL remapping
│
├── logs/                           # Sync script log output directory
│
├── .workspace_state.json           # (auto-generated) Persisted workspace pairs
├── .msal_token_cache.bin           # (auto-generated) MSAL token cache
├── .sync_schedule.json             # (auto-generated) Scheduled sync state
├── .autosync_state.json            # (auto-generated) Auto-sync watcher state
├── .dr_events.json                 # (auto-generated) Failover/failback event history
└── .rti_schedule.json              # (auto-generated) RTI scheduled replication state
```

---

## 4. Prerequisites

| Requirement | Why |
|-------------|-----|
| **Python 3.12+** | Runtime for Flask dashboard and CLI scripts |
| **uv** (recommended) or pip | Dependency management — `uv run python app.py` handles everything |
| **Microsoft Fabric account** | Access to at least one Fabric workspace |
| **Two Fabric workspaces** | Primary (production) and Secondary (DR target) in different regions |
| **Browser** | MSAL interactive login opens a browser tab for Microsoft sign-in |
| **Fabric capacity** | Both workspaces need active F2+ or Trial capacity |

### For CLI Scripts Only (Optional)
| Requirement | Why |
|-------------|-----|
| **Azure AD Service Principal** | Automated headless execution |
| **azcopy** | If using active replication strategy for lakehouses |
| **pandas** | Data manipulation in sync scripts |

---

## 4a. Configuration — CLI Scripts Only

> **Dashboard users: skip this section entirely.**
> If you are using the web dashboard (`app.py`), you sign in with your Microsoft account and select workspaces interactively from the UI — no configuration files or environment variables are needed.
>
> The steps below are **only required if you plan to run the standalone CLI scripts** in `scripts/` (e.g. for automated/scheduled headless execution with a Service Principal).

### Step A — Set Environment Variables

Copy `.env.template` to `.env` and fill in your values:

```bash
cp .env.template .env
```

Then open `.env` and set:

```env
# Your primary Fabric workspace GUID (find in Fabric portal URL)
PRIMARY_WORKSPACE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Your secondary (DR target) Fabric workspace GUID
SECONDARY_WORKSPACE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Capacity IDs for both workspaces
PRIMARY_CAPACITY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
SECONDARY_CAPACITY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Service Principal credentials
TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
CLIENT_SECRET=your-client-secret-value
```

> **How to find your Workspace ID:** Open the workspace in the Fabric portal — the GUID is in the browser URL after `/groups/`.

### Step B — Populate Artifact Mapping CSVs

The files in `data/` must be filled in for your workspaces before running CLI sync scripts.

**`data/artifact_mapping.csv`** — Maps each primary artifact to its secondary counterpart:
```
primary_artifact_id,secondary_artifact_id,artifact_type,primary_name,secondary_name
<primary-lakehouse-guid>,<secondary-lakehouse-guid>,Lakehouse,bronze_lakehouse,bronze_lakehouse
...
```

**`data/reference_mapping.csv`** — Maps workspace-level IDs used inside artifact definitions (e.g. connection strings inside semantic models):
```
primary_reference,secondary_reference,reference_type
<primary-workspace-id>,<secondary-workspace-id>,WorkspaceId
<primary-lakehouse-id>,<secondary-lakehouse-id>,Lakehouse_bronze
...
```

> **Tip:** Run `scripts/sync_workspaces_metadata.py` first — it discovers all artifacts in both workspaces and can generate these CSV files automatically.

### Step C — (check_data.py only) Set Additional Variable

```env
SEMANTIC_MODEL_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

### Step D — (deploy_report.py only) Set Report ID

```env
REPORT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

### Step E — Update the Report Connection String

`CrestShield-Claim-report.Report/definition.pbir` contains a connection string that must point to your workspace and semantic model before running `deploy_report.py`:

```json
"connectionString": "Data Source=powerbi://api.powerbi.com/v1.0/myorg/<your-workspace-name>;initial catalog=<YourSemanticModelName>;integrated security=ClaimsToken;semanticmodelid=<your-primary-semanticmodel-id>"
```

---


## 5. Step-by-Step Setup

### Step 1 — Clone the Repository
```bash
git clone https://github.com/Jusac33/Customer_Shareable_Fabric_Resiliency_and_Recovery_Framework.git
cd Customer_Shareable_Fabric_Resiliency_and_Recovery_Framework
```

### Step 2 — Install Dependencies
```bash
# Using uv (recommended):
uv sync

# Or using pip:
pip install flask flask-cors msal requests pandas
```

### Step 3 — Start the Dashboard
```bash
# Using uv:
uv run python app.py

# Or directly:
python app.py
```

The dashboard starts on **http://localhost:5000**.

### Step 4 — Sign In
1. Open http://localhost:5000 in your browser
2. Click **"Sign in with Microsoft"**
3. A browser tab opens for Microsoft interactive login
4. Sign in with your Fabric account credentials
5. Once authenticated, you're redirected to the Setup page

### Step 5 — Configure Workspace Pairs
1. On the **Setup** page, you see all Fabric workspaces you have access to
2. Select a **Primary Workspace** (your production workspace)
3. Select a **Secondary Workspace** (your DR target)
4. Optionally give the pair a **label** (e.g., "Claims Processing")
5. Click **"Add Pair & Continue"**
6. You can add multiple pairs later from the setup page

### Step 6 — Start Using the Dashboard
You're now on the **Command Center** showing:
- Artifact type cards with sync percentages
- One-click replication buttons per artifact type
- Lakehouse data sync controls
- Auto-sync and scheduled sync toggles

---

## 6. Authentication Flow

### Where Does Tenant ID Come In?

| Mode | Where to provide Tenant ID |
|------|---------------------------|
| **Dashboard — interactive login** | **Nowhere** — not required. MSAL's browser login resolves the tenant automatically from your Microsoft account. |
| **Dashboard — Service Principal login** | Enter it directly in the **SP Login form** in the dashboard UI (along with Client ID and Client Secret). Nothing to configure in advance. |
| **CLI scripts (Service Principal)** | Set `TENANT_ID=` in your `.env` file (see Section 4a). |

---

### Interactive Login Flow

```
User clicks "Sign In"
       │
       ▼
POST /api/auth/start
       │
       ▼
Background thread: msal.PublicClientApplication.acquire_token_interactive()
  • Uses Azure PowerShell public client ID (1950a258-...)
  • Opens system browser for Microsoft login  ← tenant resolved here automatically
  • Scope: https://api.fabric.microsoft.com/.default
       │
       ▼
Frontend polls GET /api/auth/status every 2 seconds
       │
       ▼
On success: token + user info stored in _auth_state
  • Token cached to .msal_token_cache.bin (persists across restarts)
  • Silent token refresh via acquire_token_silent() on expiry
       │
       ▼
Redirect to /setup (workspace configuration)
```

**Key implementation details:**
- No Service Principal or app registration required — uses Azure PowerShell's public client
- Token cache is encrypted on disk via MSAL's `SerializableTokenCache`
- A separate OneLake token (`storage.azure.com` scope) is acquired for DFS table discovery
- All API calls go through `fabric_api()` which handles 202 long-running operations, 429 rate limiting, and token refresh

---

## 7. Workspace Pair Configuration

The system supports **multiple workspace pairs** — e.g., "Claims Processing" (East US 2 ↔ Central US) and "Risk Analytics" (West Europe ↔ North Europe) simultaneously.

### Data Model
```json
{
  "pairs": [
    {
      "id": "a1b2c3d4",
      "label": "Claims Processing",
      "primary_id": "3e48aa35-...",
      "primary_name": "crestshield-smartclaims",
      "secondary_id": "e7804219-...",
      "secondary_name": "crestshield_Secondary"
    }
  ],
  "active_pair": "a1b2c3d4"
}
```

### How It Works
- Stored in `.workspace_state.json` (auto-created, gitignored)
- **Active pair** determines which workspaces are used by all dashboard operations
- Sidebar dropdown lets you switch between pairs (only visible when 2+ pairs exist)
- Switching clears the API cache so fresh data is loaded
- Old single-pair format auto-migrates on first load
- `_ws_id("primary")` / `_ws_id("secondary")` always returns the active pair's workspace IDs

---

## 8. Artifact Replication Engine

### What Gets Replicated

| Artifact Type | How | Connection Rewriting |
|--------------|-----|---------------------|
| **Lakehouse** | `POST /items` with `enableSchemas` detection — auto-detects `defaultSchema` on primary and passes `creationPayload: {enableSchemas: true}` when needed | N/A |
| **Notebook** | Export definition → create in secondary | N/A |
| **SemanticModel** | Export PBIR definition → rewrite workspace/item IDs → create | Yes — workspace IDs, lakehouse IDs, SQL endpoints |
| **Report** | Export PBIR definition → rewrite dataset references → create → rebind to secondary SemanticModel via Power BI API | Yes — SemanticModel bindings |
| **DataPipeline** | Export definition → create in secondary | N/A |
| **DataAgent** | `POST /items` (no definition export available) | N/A |
| **Ontology** | `POST /items` (no definition export available) | N/A |
| **Warehouse** | Schema + data sync via `executeQueries` REST API (roadmap — not yet implemented) | Yes — workspace IDs, SQL endpoints |
| **SQLEndpoint** | System-managed (auto-created with Lakehouse) | Skipped |

### Replication Flow (per artifact)

```
1. Export definition from primary:
   POST /workspaces/{primary_id}/items/{item_id}/getDefinition
   → Returns { "definition": { "parts": [ { "path": "...", "payload": "base64..." } ] } }

2. Build connection map (for SemanticModel/Report):
   • Map primary workspace ID → secondary workspace ID
   • Map each primary item ID → secondary item ID (matched by displayName)
   • Map SQL endpoint server names

3. Rewrite definition parts:
   • Base64-decode each part's payload
   • String-replace all primary references with secondary equivalents
   • Base64-encode back

4. Create in secondary:
   POST /workspaces/{secondary_id}/items
   Body: { "displayName": "...", "type": "...", "definition": { "parts": [...] } }

5. Handle 202 Long-Running Operation:
   • Poll the Location header URL until completion
   • Retry on 429 (Too Many Requests) with backoff
```

### One-Click Replication
On the dashboard, each artifact type card has a **"CICD"** button that calls `POST /api/bcdr/replicate` with the type. This replicates all missing items of that type.

Individual items can also be replicated via `POST /api/bcdr/replicate-item`.

### Folder Structure Mirroring
When replicating artifacts, the engine discovers the folder hierarchy in the primary workspace and mirrors it in the secondary. Items are placed in the same folder path they occupy in the primary workspace. This uses:
- `_get_workspace_folders()` — fetches all folders from a workspace
- `_ensure_folder_structure()` — recreates the folder tree in secondary
- `_get_folder_id_for_item()` — maps each item to its secondary folder

### Report Rebinding
After replicating a Report, the engine rebinds it to the corresponding SemanticModel in the secondary workspace via the Power BI rebind API:
```
POST https://api.powerbi.com/v1.0/myorg/groups/{secondary_workspace_id}/reports/{report_id}/Rebind
Body: { "datasetId": "<secondary_semantic_model_id>" }
```
The rebind function (`_rebind_report_to_secondary()`) looks up the primary report's `datasetId`, maps it to the matching SemanticModel in the secondary workspace by name, and calls the Power BI Rebind API. This ensures reports render against secondary data.

### Background Replication with Live Progress Tracking
Replication runs asynchronously in a background thread to avoid blocking the dashboard:

1. `POST /api/bcdr/replicate` starts a `threading.Thread` running `_run_replicate_background()`
2. Progress is tracked in `_sync_progress` dict (protected by `_sync_lock`)
3. Frontend polls `GET /api/bcdr/replicate/progress` or `GET /api/bcdr/replicate/progress/<type>` to display real-time progress
4. Progress state includes: `status` (idle/running/completed/failed), `current`, `total`, `current_item`, `created`, `skipped`, `failed`, `error`

This allows the user to start replication and navigate away — the dashboard header badge shows the running status.

---

## 8a. Semantic Model & Report Resilient Sync

SemanticModel and Report items use `getDefinition` / `updateDefinition` APIs. When the secondary already has a stale item whose internal bindings (e.g., lakehouse IDs in `expressions.tmdl`) differ from the remapped definition, `updateDefinition` may fail with a validation error.

**Delete-and-Recreate Fallback**: If `updateDefinition` fails for a SemanticModel or Report, the engine automatically:
1. Deletes the stale secondary item
2. Creates a fresh item with the correct remapped definition
3. Logs the fallback action

This ensures sync succeeds even when the secondary item has residual bindings from prior manual or failed sync runs.

---

## 9. Lakehouse Data Sync (Delta Tables)

Artifact replication only copies **metadata** (the lakehouse definition). The actual **Delta table data** requires a separate sync mechanism using a PySpark notebook deployed to the secondary workspace.

### Deploy Sync Artifacts
Click **"Deploy Notebook + Pipeline"** on the Lakehouse Data Sync page. This:

1. **Generates a per-lakehouse PySpark notebook** via `_generate_per_lh_sync_notebook_ipynb()` in `app.py` with:
   - Discovery of all Delta tables and Files via OneLake DFS paths (from **primary** only)
   - Support for schema-enabled lakehouses (`dbo.schema.table`)
   - **Dual sync engine** — `fast_copy` (default) or `spark_cdf` (legacy), selected at deploy time

2. **Creates a Data Pipeline** (`BCDR_Sync_Pipeline`) that triggers the notebook

3. **Deploys both** to the secondary workspace via the Fabric API

---

### Sync Engine: `fast_copy` (Default)

Uses `notebookutils.fs.cp` — a server-side OneLake copy that stays entirely within Microsoft's network. No bytes leave the data plane. Recommended by Microsoft's [Fabric DR guidance](https://learn.microsoft.com/en-us/fabric/security/experience-specific-guidance) as **Approach 1**.

#### Auto-Mode Logic

The notebook automatically selects full or incremental on a **per-table basis**:

```python
state = _fc_load_state()   # Files/_bcdr_sync_state/<lh_name>.json on secondary

for display_name in discover_tables(src):
    last_ms = _fc_get_last_ms(state, display_name)

    if SYNC_MODE == 'full' or last_ms == 0:
        stats = fast_copy_full(src, dst, display_name)     # first run
    else:
        stats = fast_copy_incremental(src, dst, display_name, last_ms)  # subsequent

    state[display_name]["last_sync_ms"]  = <now_ms>
    state[display_name]["last_sync_iso"] = <now_iso>

_fc_save_state(state)
sync_files_section()
```

#### `fast_copy_full()`
Full copy of an entire Delta table directory from primary → secondary:
```python
def fast_copy_full(src_root, dst_root, table_key):
    notebookutils.fs.cp(f"{src_root}/{table_key}", f"{dst_root}/{table_key}", recurse=True)
```

#### `fast_copy_incremental()`
Copies only files modified after the last sync watermark (`since_ms`):

```python
def fast_copy_incremental(src_root, dst_root, table_key, since_ms):
    for f in notebookutils.fs.ls(f"{src_root}/{table_key}"):
        if f.modifyTime > since_ms:
            notebookutils.fs.cp(f.path, f"{dst_root}/{table_key}/{f.name}")
    # Copy only new _delta_log commits
    for entry in notebookutils.fs.ls(f"{src_root}/{table_key}/_delta_log"):
        if entry.modifyTime > since_ms:
            notebookutils.fs.cp(entry.path, f"{dst_root}/{table_key}/_delta_log/{entry.name}")
```

#### Watermark State File
Per-table watermarks are stored as JSON in the secondary lakehouse's **Files** section — not in Tables — to avoid polluting the catalog:

```
Files/_bcdr_sync_state/<lh_name>.json
```

Example state file:
```json
{
  "gold_claims_summary": {
    "last_sync_ms": 1712934000000,
    "last_sync_iso": "2026-04-12T14:00:00Z"
  },
  "gold_severity_scores": {
    "last_sync_ms": 1712934000000,
    "last_sync_iso": "2026-04-12T14:00:00Z"
  }
}
```

Force a full re-sync any time by setting `SYNC_MODE = 'full'` in the notebook config cell.

---

### Sync Engine: `spark_cdf` (Legacy)

Uses Delta Change Data Feed (CDF) + Spark read/write. Requires `delta.enableChangeDataFeed = true` on primary tables.

```python
# Auto-enables CDF on primary if not present
enable_cdf(src_path)

# 1. Get current Delta version of source table
current_ver = get_delta_version(src_path)

# 2. Load last synced version from version watermark
last_synced = get_sync_state(control_path, lakehouse_name, table_name)

# 3. Never synced or full mode → full overwrite
if last_synced < 0:
    df = spark.read.format("delta").load(src_path)
    df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(dst_path)

# 4. CDF incremental — new versions only
elif current_ver > last_synced:
    changes = spark.read.format("delta") \
        .option("readChangeFeed", "true") \
        .option("startingVersion", last_synced + 1) \
        .load(src_path)
    rows = changes.filter(F.col("_change_type").isin("insert", "update_postimage"))
    rows.drop("_change_type", "_commit_version", "_commit_timestamp") \
        .write.format("delta").mode("append").save(dst_path)

# 5. Update Delta version watermark
update_sync_state(control_path, lakehouse_name, table_name, current_ver)
```

Sync state for `spark_cdf` is stored in `Files/_bcdr_sync_control` (separate from fast_copy state).

---

### Files Section Sync
Both engines call `sync_files_section()` which uses `_incremental_cp()` with the same `since_ms` watermark — ensuring non-Delta files (CSVs, Parquets, models, etc.) in the `Files/` area are also incrementally synced.

### Run Sync
Click **"Run Sync"** to trigger the notebook in the secondary workspace. The notebook runs as a Spark job via the Fabric API:
```
POST /workspaces/{secondary_id}/items/{notebook_id}/jobs/instances?jobType=RunNotebook
```

### Choosing an Engine

| Criterion | `fast_copy` | `spark_cdf` |
|-----------|-------------|-------------|
| CDF required on source | No | Yes |
| Spark session required | No (notebookutils only) | Yes |
| Network traffic | Stays in OneLake | Stays in OneLake |
| File-level granularity | Yes (modifyTime filter) | No (Delta version-based) |
| First supported | This implementation | Original implementation |
| Recommended | **Yes (default)** | Legacy fallback |

---

## 10. Schema-Enabled Lakehouses & Table Registration

### The Problem
Fabric lakehouses can be **schema-enabled** (supporting named schemas like `dbo.smartclaims.table_name`). When a primary lakehouse has `defaultSchema: "dbo"`, the secondary must also be created with schema support — otherwise Delta tables synced via the notebook won't appear in the lakehouse catalog and show as "Unidentified" in the Fabric UI.

### Automatic Schema Detection
The `replicate_items_by_type()` function detects schema-enabled lakehouses automatically:
1. Fetches lakehouse properties from the primary via `GET /workspaces/{id}/lakehouses/{id}`
2. Checks for `defaultSchema` in the response
3. If present, passes `creationPayload: {"enableSchemas": true}` when creating the secondary lakehouse

### Registration Notebooks
After Delta table data is synced, tables must be **registered** in the lakehouse catalog so they appear in SQL analytics endpoints, notebooks, and the Fabric UI.

The deploy process generates **per-lakehouse registration notebooks** (one for bronze, silver, gold, etc.) that:
1. Discover all schema/table combinations from the OneLake DFS path
2. Execute `CREATE SCHEMA IF NOT EXISTS` for each schema
3. Execute `CREATE TABLE IF NOT EXISTS ... USING DELTA LOCATION` for each table
4. Run as the final cell in the master orchestration notebook

### Sync Control Location
The sync state tracker (`_bcdr_sync_control`) is written to the lakehouse **Files** section (`Files/_bcdr_sync_control`), not the Tables section. This prevents the control file from appearing as an "Unidentified" table in the catalog.

---

## 11. Auto-Sync Watcher

The auto-sync watcher runs in the background and **automatically replicates new artifacts** from primary to secondary.

### How It Works
1. Background timer fires every N seconds (configurable: 30s to 10 minutes)
2. Fetches fresh item lists from both workspaces (bypasses cache)
3. Compares primary items vs secondary items by `(displayName, type)`
4. For each new item in primary not in secondary:
   - Exports its definition (if available)
   - Rewrites connection strings (for SemanticModel/Report)
   - Creates it in the secondary workspace
5. Logs results and updates state

### Dashboard Controls
- Toggle on/off from the Command Center
- Configure interval (30s / 1min / 2min / 5min / 10min)
- Status shows: check count, replicated count, last status message
- State persisted to `.autosync_state.json` (survives restarts)

### What Won't Auto-Sync
- `Warehouse` and `SQLEndpoint` (not supported by Fabric API for creation)
- Data inside lakehouses (use the notebook sync for that)

---

## 12. Scheduled Sync

Separate from auto-sync, the **scheduled sync** triggers the `BCDR_Data_Replication` notebook on a configurable interval.

| Setting | Range | Purpose |
|---------|-------|---------|
| Interval | 5 min – 24 hours | How often the notebook runs |
| Toggle | On/Off | Enable/disable from dashboard |

This handles **data** replication (Delta tables), while auto-sync handles **artifact** replication (metadata).

State persisted to `.sync_schedule.json`.

---

## 13. Operational Drift Analysis

**Page: `/drift`**

Compares primary and secondary workspaces to detect:

| Category | What It Detects |
|----------|----------------|
| **IN_SYNC** | Items present in both with matching type |
| **MISSING_IN_SECONDARY** | Items in primary but not in secondary |
| **EXTRA_IN_SECONDARY** | Items in secondary but not in primary |
| **TYPE_MISMATCH** | Same name but different artifact types |
| **WorkspacePermission** | Role assignment differences (via `/roleAssignments` API) — users/groups with different roles or missing entirely |
| **SensitivityLabel** | Sensitivity label differences on items (via item detail API) |

### Inline Actions on Drift Page

| Status | Artifact Type | Action | What It Does |
|--------|--------------|--------|--------------|
| **Missing** | WorkspacePermission | **Fix** (user-plus icon) | Calls `/api/bcdr/sync-permission` to add the missing role assignment to secondary |
| **Mismatch** | WorkspacePermission | **Fix** (user-shield icon) | Calls `/api/bcdr/sync-permission` to PATCH the role in secondary to match primary |
| **Def Changed** | Hashable types (Notebook, Pipeline, SemanticModel, Report, SparkJobDefinition, Dataflow, Eventstream) | **Sync** | Syncs the individual item definition to secondary |
| **In Sync** | Hashable types | **Check** (search icon) | Re-checks definition hash to detect drift |

### Display
- Items grouped by artifact type in collapsible sections
- Each group shows mini-badges (synced / missing / mismatch / extra counts)
- Expand to see individual items with primary/secondary IDs
- Actionable Fix/Sync/Check buttons appear inline next to artifact names

---

## 14. Data Assurance Validation

**Page: `/integrity`**

Validates data consistency between primary and secondary:

| Check | Method | What It Validates |
|-------|--------|-------------------|
| **Table Count** | OneLake DFS `Tables/` recursive listing via `_delta_log` detection | Same number of Delta tables in each lakehouse pair |
| **Lakehouse Existence** | Name matching | Secondary has a lakehouse for every primary lakehouse |
| **KQL Database Table Count** | KQL `.show tables` command via Kusto REST API | Same number of KQL tables in each KQL Database pair |
| **KQL Database Row Count** | KQL `{table} \| count` query per table | Same row counts in each table across primary and secondary KQL Databases |
| **Item Count** | Fabric API `/workspaces/{id}/items` | Same artifact counts per type (SemanticModel, Report, Notebook, Eventhouse, KQLDatabase, KQLQueryset, Eventstream, etc.) |

### KQL Database Assurance Detail
For each KQL Database pair (matched by name):
1. Compares table count via `.show tables` on both primary and secondary Kusto query URIs
2. For each table, runs `{table} | count` on both sides and compares row counts
3. Reports per-table variance (e.g., "KQL DB: MyDB → SensorReadings: Row Count: 50 vs 50 — pass")
4. Missing KQL Databases in secondary are flagged as "fail"

### Artifact Type Coverage
The type count validation includes RTI artifact types:
- `Eventhouse`, `KQLDatabase`, `KQLQueryset`, `Eventstream`
- These appear alongside `SemanticModel`, `Report`, `Notebook`, `DataPipeline`, `DataAgent`, `Ontology`

Results displayed in a table with Pass/Warning/Fail status per check.

---

## 15. Replication Lag Calculation

The topology page shows **real replication lag** (not a fake estimate). It's computed from the most recent sync event across three sources:

```
Priority:
  1. Notebook job history — GET /workspaces/{id}/items/{nb_id}/jobs/instances
     → Looks for last "Completed" job of BCDR_Data_Replication notebook
     → Uses endTimeUtc as the sync timestamp

  2. Scheduled sync — _schedule_state["last_run"]
     → Timestamp of last triggered notebook run

  3. Auto-sync watcher — _autosync_state["last_check"]
     → Timestamp of last watcher pass (if enabled)

Lag = minutes_since(most_recent_of_above)
```

### Status Thresholds
| Lag | Status |
|-----|--------|
| ≤ 15 min, 0 drift | `HEALTHY` |
| ≤ 60 min | `NEEDS_SYNC` |
| > 60 min | `STALE` |
| Never synced | `NEEDS_SYNC` |
| No SecondaryConfigured | `NO_DR` |

The lag source is displayed on the topology page (e.g., "Notebook job run", "Scheduled sync", "Auto-sync watcher").

---

## 16. Regional Topology & Multi-Workspace Pairs

**Page: `/topology`**

Shows the active workspace pair with:
- **Region cards** — Health %, item count, status, last heartbeat for primary/secondary
- **Topology diagram** — Visual link with lag pill (colored: green ≤15m, orange ≤60m, red >60m)
- **Replication summary** — Primary/secondary item counts, lag, artifact drift, last data sync time

### All Workspace Pairs Table
When 2+ pairs are configured, an **"ALL WORKSPACE PAIRS"** table appears showing:
- Pair label, primary name, secondary name
- Item counts (primary / secondary)
- Sync percentage
- Active indicator

### Pair Switching
- Sidebar dropdown (visible when 2+ pairs)
- Selecting a pair reloads all dashboard data for that pair
- API: `POST /api/workspace-pairs/active`

---

## 17. Managed Failover & Failback

**Page: `/failover`** (dashboard) | **CLI: `scripts/failover.py` and `scripts/failback.py`**

The project provides failover/failback through both the web dashboard and standalone CLI scripts.

### Pre-Failover Checklist
| Check | Source |
|-------|--------|
| Primary workspace accessible | Live API health check |
| Secondary workspace healthy | Live API health check |
| Replication lag < 15 min | Real lag from topology API |
| Artifact mappings valid | Static check |
| Secondary capacity available | Static check |
| Secondary artifact count parity | Live artifact count comparison (CLI) |

### Failover Execution (Dashboard)
The failover page provides both **dry-run simulation** and **live execution**:

1. **Run Failover Dry-Run** — Simulates all steps with streamed console output. No actual changes are made.
2. **Execute Failover** — Performs a managed failover:
   - Validates secondary workspace readiness
   - Runs a final delta sync (notebook trigger)
   - Swaps workspace pair roles (primary ↔ secondary)
   - Records a DR event with timestamp and type
   - Returns the new active workspace configuration

### Failover Execution (CLI — `scripts/failover.py`)
The CLI failover script performs a full orchestrated failover:

```
Step 1: Validate secondary currency (artifact count comparison + sync plan check)
Step 2: Pause primary workspace
        ├── Cancel all running jobs (GET /jobs/instances → POST .../cancel)
        ├── Disable all schedules (PATCH /jobScheduler {enabled: false})
        └── Save schedule manifest (for re-enablement on secondary)
Step 3: Final data sync (imports and runs sync modules: lakehouses, notebooks, semantic models, etc.)
Step 4: Validate secondary resources (per-type accessibility check)
Step 5: Activate secondary
        ├── Map primary schedule manifest to secondary item IDs via artifact_mapping.csv
        └── Enable schedules on secondary counterparts (PATCH /jobScheduler {enabled: true})
```

**Schedulable artifact types** handled: `DataPipeline`, `Notebook`, `SparkJobDefinition`, `DataflowsGen2`

**Fabric APIs used for pause/activate:**
| Operation | API Endpoint |
|-----------|-------------|
| List job instances | `GET /workspaces/{ws}/items/{id}/jobs/instances` |
| Cancel running job | `POST /workspaces/{ws}/items/{id}/jobs/instances/{jobId}/cancel` |
| Get schedule | `GET /workspaces/{ws}/items/{id}/jobScheduler` |
| Enable/disable schedule | `PATCH /workspaces/{ws}/items/{id}/jobScheduler` |

The schedule manifest is persisted in `data/failover_log.json` so the failback script knows which schedules to re-enable on primary.

### Failback Execution (Dashboard)
After the disaster is resolved, **Execute Failback** reverses the process:
1. Validates original primary is healthy and accessible
2. Runs reverse delta sync (secondary → primary)
3. Swaps workspace pair roles back to original
4. Records a failback DR event

### Failback Execution (CLI — `scripts/failback.py`)

```
Step 1: Pause secondary workspace
        ├── Cancel all running jobs on secondary
        ├── Disable all schedules on secondary
        └── Save secondary schedule manifest
Step 2: Reverse sync (secondary → primary)
        ├── Lakehouses: reverse_sync_lakehouses() — FAST_COPY or ACTIVE_REPLICATION
        │   • FAST_COPY: incremental file walk (modifyTime > failover_timestamp_ms)
        │   • ACTIVE_REPLICATION: azcopy with --include-after=<failover_ISO>
        └── Covers: notebooks/pipelines, semantic models/reports, dataflows, permissions
Step 3: Validate primary readiness
        ├── Per-type artifact count comparison between primary and secondary
        └── Flags parity issues (e.g., "Notebook: primary=5, secondary=7")
Step 4: Reactivate primary
        ├── Load original schedule manifest from data/failover_log.json
        └── Re-enable those schedules on primary
Step 5: Decommission secondary to standby
        ├── Verify all schedules are disabled
        └── Leave all artifacts intact for next DR cycle
```

### Reverse Lakehouse Sync Strategies

The failback script uses `reverse_sync_lakehouses()` in `scripts/sync_lakehouses.py` to copy only the **delta data written to secondary after failover** back to primary — avoiding re-copying V1 data already on primary.

#### FAST_COPY (Default)

Uses `notebookutils.fs.cp` with a per-file `modifyTime` filter. Generates a temporary notebook, runs it on primary or secondary, then deletes the notebook:

```python
# Only copy files whose modifyTime > failover_timestamp_ms (epoch ms)
for table in tables:
    for f in notebookutils.fs.ls(f"{src}/{table}"):
        if f.modifyTime > failover_ts_ms:
            notebookutils.fs.cp(f.path, f"{dst}/{table}/{f.name}")
    # Copy only new _delta_log commit entries
    for entry in notebookutils.fs.ls(f"{src}/{table}/_delta_log"):
        if entry.modifyTime > failover_ts_ms:
            notebookutils.fs.cp(entry.path, f"{dst}/{table}/_delta_log/{entry.name}")
```

The `failover_timestamp_ms` is read from `data/failover_log.json` (written by `failover.py` at failover time).

#### ACTIVE_REPLICATION

Uses `azcopy copy` with `--include-after` to transfer only files written after the failover timestamp:

```bash
azcopy copy "<secondary_onelake_path>" "<primary_onelake_path>" \
  --recursive \
  --include-after="2026-04-12T14:00:00Z"
```

The ISO timestamp comes from `failover_log.json["failover_timestamp"]`.

### `failover_log.json` Structure

`failover.py` writes this after successful failover:

```json
{
  "timestamp": "2026-04-12T14:00:00Z",
  "failover_timestamp": "2026-04-12T14:00:00Z",
  "primary_workspace_id": "3e48aa35-...",
  "secondary_workspace_id": "fa100c31-...",
  "lakehouse_strategy": "FAST_COPY",
  "schedule_manifest": { ... }
}
```

`failback.py` reads `failover_timestamp` from this file to anchor the incremental reverse sync.

### CLI Usage
```bash
# Failover (dry run)
python scripts/failover.py --dry-run

# Failover (live)
python scripts/failover.py

# Failover skipping validation checks
python scripts/failover.py --skip-validation

# Failback (dry run) — uses FAST_COPY by default
python scripts/failback.py --dry-run

# Failback (live) — FAST_COPY uses notebookutils.fs.cp incremental
python scripts/failback.py

# Failback using azcopy --include-after instead
python scripts/failback.py --lakehouse-strategy ACTIVE_REPLICATION

# Failback with dry run and explicit strategy
python scripts/failback.py --lakehouse-strategy FAST_COPY --dry-run
```

### DR Event History
All failover/failback events are persisted to `.dr_events.json` (gitignored) and displayed in the event history timeline on the failover page. Each event records:
- Timestamp, event type (failover/failback/simulation)
- Source and target workspace IDs
- Operator and status

CLI scripts persist their own logs to `data/failover_log.json` and `data/failback_log.json` respectively.

### DR State Machine
The system tracks the current DR state:
- `NORMAL` — Primary is active (default)
- `FAILOVER_IN_PROGRESS` — Failover executing
- `FAILED_OVER` — Secondary is active
- `FAILBACK_IN_PROGRESS` — Failback executing

### Recovery Metrics
- **RTO** — Estimated recovery time (45 seconds for metadata failover)
- **RPO** — Live value from actual replication lag (e.g., "3 min" from last notebook job)

### RPO/RTO Gauges
The failover page displays live RPO/RTO gauges showing current values against targets, color-coded green/orange/red based on thresholds.

---

## 18. Data Gateways

**Page: `/gateways`**

Discovers and displays all on-premises data gateways accessible to the authenticated user.

### What It Shows
- **Gateway list** — All gateways with name, ID, type (Personal/Standard), public key
- **Members** — Users/groups assigned to each gateway
- **Data sources** — Connections configured through each gateway (type, server, database, credentials)
- **Status** — Gateway online/offline status

### How It Works
The page calls:
1. `GET /gateways` — Lists all gateways
2. `GET /gateways/{id}/members` — Lists gateway members
3. `GET /gateways/{id}/datasources` — Lists data sources for each gateway

This is useful for BCDR planning to identify which on-prem data sources would need re-pointing during failover.

---

## 19. CLI Sync Scripts (Standalone)

The `scripts/` directory contains independent Python scripts that use `common.py` and a Service Principal for automated/headless sync.

### Setup for CLI Scripts
```bash
cp .env.template .env
# Edit .env with your workspace IDs, tenant ID, SP credentials
```

### Running Scripts
```bash
# Sync all notebooks and pipelines
python scripts/sync_notebooks_and_pipelines.py

# Sync semantic models with definition remapping
python scripts/sync_semantic_models_and_reports.py

# Sync lakehouses with active replication strategy
python scripts/sync_lakehouses.py --strategy ACTIVE_REPLICATION

# Dry run (no changes)
python scripts/sync_lakehouses.py --strategy ONELAKE_SHORTCUTS --dry-run

# Override workspace IDs (all scripts support these flags)
python scripts/sync_notebooks_and_pipelines.py \
  --primary-workspace 3e48aa35-... \
  --secondary-workspace e7804219-...

# Full failover orchestration
python scripts/failover.py --dry-run

# Failover skipping row-count validation
python scripts/failover.py --skip-validation

# Failback after recovery
python scripts/failback.py
```

### Global CLI Flags (All Scripts)
| Flag | Purpose |
|------|---------|
| `--primary-workspace` | Override the primary workspace GUID from `.env` |
| `--secondary-workspace` | Override the secondary workspace GUID from `.env` |
| `--dry-run` | Simulate without making changes (where supported) |
| `--skip-validation` | Skip row-count validation before failover (`failover.py` only) |
| `--lakehouse-strategy` | Reverse sync strategy for failback: `FAST_COPY` (default) or `ACTIVE_REPLICATION` (`failback.py` only) |

### Script Coverage

| Script | Artifact Types |
|--------|---------------|
| `sync_workspaces_metadata.py` | All (inventory snapshot) |
| `sync_notebooks_and_pipelines.py` | Notebook, DataPipeline, SparkJobDefinition |
| `sync_semantic_models_and_reports.py` | SemanticModel, Report |
| `sync_lakehouses.py` | Lakehouse (4 strategies: `FAST_COPY` / `ACTIVE_REPLICATION` / `ONELAKE_SHORTCUTS` / `GRS_PASSTHROUGH`). Forward sync deploys `fast_copy` engine notebooks; reverse sync via `reverse_sync_lakehouses()` |
| `sync_warehouses.py` | Warehouse (roadmap — schema/data sync via `executeQueries` API) |
| `sync_permissions.py` | All (delta-only workspace permissions + item permissions + OneLake Data Access Roles RLS/CLS) |
| `sync_dataflows.py` | DataflowsGen2 |
| `sync_eventstreams.py` | Eventstream (delegates to `rti/sync_rti.py`) |
| `sync_kql_databases.py` | KQLDatabase, KQLQueryset (delegates to `rti/sync_rti.py`) |
| `sync_ml_models_and_experiments.py` | MLModel, SparkJobDefinition (delegates Environment sync to `sync_environments.py`) |
| `sync_environments.py` | Environment (full getDefinition → updateDefinition → publish flow) |
| `bulk_sync.py` | All definition-supported types via Bulk Export/Import APIs (beta) with per-item fallback |
| `failover.py` | Orchestrated failover (cancel jobs → disable schedules → sync → validate → activate secondary) |
| `failback.py` | Reverse sync (pause secondary → reverse sync → validate parity → reactivate primary → standby secondary). Accepts `--lakehouse-strategy FAST_COPY\|ACTIVE_REPLICATION` |

### Environment Variable Tuning
These are configured in `.env` and control CLI script performance and resilience:

| Variable | Default | Purpose |
|----------|---------|---------|
| `NUM_THREADS` | `5` | Max parallel threads for artifact sync operations |
| `RESPONSE_BACKOFF` | `2` | Base seconds for exponential backoff on rate-limiting (429) |
| `MAX_RETRIES` | `3` | Max retry attempts for failed API calls |
| `OPERATION_TIMEOUT_SECONDS` | `300` | Max wait time (seconds) for long-running operations (202 polling) |
| `GIT_INTEGRATION_ENABLED` | `False` | Enable Git-based artifact sync (experimental) |

### Mapping Files (`data/`)
The `data/` directory contains CSV mapping files used by CLI scripts for ID remapping during replication:

**`artifact_mapping.csv`** — Maps primary artifact IDs to secondary artifact IDs:
```csv
primary_artifact_id,secondary_artifact_id,artifact_type,primary_name,secondary_name
550e8400-...,550e8400-...,Lakehouse,sales_data,sales_data
```

**`connection_mapping.csv`** — Maps primary connection names to secondary:
```csv
primary_connection_name,secondary_connection_name,connection_type
primary_sql_db,secondary_sql_db,SqlServer
primary_adls_connection,secondary_adls_connection,AzureDataLakeStorage
```

**`reference_mapping.csv`** — Maps workspace IDs, capacity IDs, OneLake URLs, and SQL endpoints:
```csv
primary_reference,secondary_reference,reference_type
550e8400-...,550e8400-...,WorkspaceId
primary-sql-server.database.windows.net,secondary-sql-server.database.windows.net,SqlEndpoint
```

These files are loaded by `common.py` functions (`load_artifact_mapping()`, `load_connection_mapping()`, `load_reference_mapping()`) and used during definition rewriting. The dashboard (`app.py`) builds equivalent mappings dynamically from live API data instead.

### Artifact Type Differences: CLI vs Dashboard
The CLI scripts and the web dashboard support slightly different artifact types:

| Artifact Type | CLI (`common.py`) | Dashboard (`app.py`) |
|--------------|-------------------|---------------------|
| Lakehouse | ✓ | ✓ |
| Warehouse | ✓ | ✓ (skipped — API limitation) |
| Notebook | ✓ | ✓ |
| DataPipeline | ✓ | ✓ |
| SemanticModel | ✓ | ✓ |
| Report | ✓ | ✓ |
| DataflowsGen2 | ✓ | — |
| KQLDatabase | ✓ | ✓ (via RTI page) |
| KQLQueryset | ✓ | ✓ (via RTI page) |
| Eventstream | ✓ | ✓ (via RTI page) |
| Eventhouse | — | ✓ (via RTI page) |
| MLModel | ✓ | — |
| SparkJobDefinition | ✓ | — |
| Environment | ✓ | — |
| DataAgent | — | ✓ |
| Ontology | — | ✓ |

---

## 20. API Reference

### Authentication
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/start` | Start interactive browser login |
| GET | `/api/auth/status` | Poll login status |
| GET | `/logout` | Clear auth state |

### Workspace Management
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/workspaces` | List all accessible workspaces |
| POST | `/api/workspaces/select` | Add or update a workspace pair |
| GET | `/api/workspace-pairs` | List all configured pairs + active pair |
| POST | `/api/workspace-pairs/active` | Switch active pair |
| DELETE | `/api/workspace-pairs/<id>` | Remove a pair |

### Status & Monitoring
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Primary/secondary health metrics |
| GET | `/api/topology` | Regional topology + all-pairs summary |
| GET | `/api/inventory` | Artifact inventory with per-pair breakdown |
| GET | `/api/bcdr/status` | BCDR status with artifact type cards |
| GET | `/api/sync-plan` | Operational drift analysis |
| GET | `/api/logs` | Sync event logs |

### Replication
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/bcdr/replicate` | Replicate all items of a type |
| POST | `/api/bcdr/replicate-item` | Replicate a single item by ID |
| GET | `/api/bcdr/replicate/progress` | Get replication progress for all types |
| GET | `/api/bcdr/replicate/progress/<type>` | Get replication progress for a specific type |
| POST | `/api/bcdr/bulk-sync` | Bulk sync all definitions via Fabric Bulk Export/Import APIs (beta) with per-item fallback |
| POST | `/api/bcdr/deploy-sync` | Deploy notebook + pipeline to secondary |
| POST | `/api/bcdr/run-sync` | Trigger the data replication notebook |
| GET | `/api/bcdr/lakehouse-tables` | Compare lakehouse table schemas |

### Delta Detection & Permissions
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/bcdr/delta-check` | SHA-256 hash check of all hashable artifact definitions |
| POST | `/api/bcdr/delta-check-item` | Targeted single-item definition hash check by name |
| POST | `/api/bcdr/sync-item` | Sync a single artifact by name to secondary |
| POST | `/api/bcdr/sync-permission` | Add or update a workspace role assignment in secondary |
| GET | `/api/bcdr/defcheck` | Get auto definition check schedule state |
| POST | `/api/bcdr/defcheck` | Enable/disable/configure auto definition check |

### Automation
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/bcdr/schedule` | Get scheduled sync state |
| POST | `/api/bcdr/schedule` | Start/stop scheduled sync |
| GET | `/api/bcdr/autosync` | Get auto-sync watcher state |
| POST | `/api/bcdr/autosync` | Enable/disable auto-sync |

### Failover & Failback
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/failover/status` | Get current DR state, checklist, RPO/RTO, recent events |
| POST | `/api/failover/simulate` | Run failover dry-run |
| POST | `/api/failover/execute` | Execute live failover (swap primary ↔ secondary) |
| POST | `/api/failback/execute` | Execute failback (restore original primary) |
| GET | `/api/failover/events` | Get all DR event history |
| POST | `/api/refresh` | Clear all caches |

### Gateways
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/gateways` | List all on-premises data gateways, members, and data sources |

### Real-Time Intelligence (RTI)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/rti/status` | RTI artifact counts and sync status (Eventhouse, KQLDatabase, KQLQueryset, Eventstream) |
| POST | `/api/rti/sync` | Sync RTI artifacts from primary to secondary (all types or specific type via `{"type": "Eventhouse"}`) |
| GET | `/api/rti/validate` | Validate RTI artifacts are correctly synced — checks for missing items and stale connection strings |
| GET | `/api/rti/kql-databases` | List KQL Databases with query URIs, table lists, and row counts |
| POST | `/api/rti/ingest-sample-data` | Ingest sample data (SensorReadings + SystemEvents) into a primary KQL Database for testing |
| GET | `/api/rti/kql-tables` | Compare KQL tables and row counts between primary and secondary databases |
| POST | `/api/rti/replicate-kql-data` | Replicate data from primary KQL Database(s) to secondary. Pass `database=ALL` for all DBs |
| GET | `/api/rti/connections` | Audit secondary RTI artifacts for stale primary references (workspace IDs, cluster URIs) |
| POST | `/api/rti/fix-connections` | Fix stale connection strings in secondary by rewriting primary→secondary references |
| POST | `/api/rti/create-dummy` | Create dummy RTI artifacts (Eventhouse, KQL Database, Queryset, Eventstream) for testing |
| POST | `/api/rti/cleanup-dummy` | Remove dummy RTI artifacts created by create-dummy |
| GET | `/api/rti/schedule` | Get RTI scheduled replication state (enabled, interval, last run, run count) |
| POST | `/api/rti/schedule` | Enable/disable scheduled KQL data replication (`{"enabled": true, "interval_minutes": 60}`) |

### Key Response Schemas

**`GET /api/bcdr/status`** — includes capacity health detection:
```json
{
  "types": { "Lakehouse": { "primary": 3, "secondary": 2, "pct": 67 }, ... },
  "capacity_status": {
    "primary": "ok",           // "ok" | "capacity_inactive" | "auth_error" | "not_configured"
    "secondary": "ok"
  },
  "overall_pct": 85
}
```
Capacity status is determined by making a lightweight API call per workspace and detecting `CapacityNotActive` errors or 401/403 auth failures.

**`GET /api/topology`** — includes all-pairs summary:
```json
{
  "primary": { "name": "...", "health": 95, "item_count": 42, "status": "HEALTHY", ... },
  "secondary": { "name": "...", "health": 88, "item_count": 38, ... },
  "status": "HEALTHY",
  "replication_lag": 5.2,
  "lag_source": "notebook_job",
  "artifact_drift": 4,
  "last_sync_ts": "2026-04-02T10:30:00Z",
  "all_pairs": [
    { "label": "Claims", "primary_name": "...", "secondary_name": "...",
      "primary_items": 42, "secondary_items": 38, "sync_pct": 90, "active": true }
  ]
}
```

**`GET /api/failover/status`** — includes sync metrics and recent events:
```json
{
  "dr_state": "NORMAL",
  "checklist": [ { "label": "...", "ok": true } ],
  "rpo_minutes": 5.2,
  "rto_seconds": 45,
  "sync_pct": 90,
  "primary_items": 42,
  "secondary_items": 38,
  "events": [ { "ts": "...", "type": "failover", "status": "success" } ]
}
```

**`POST /api/failover/execute`** — returns new workspace configuration:
```json
{
  "ok": true,
  "rto_seconds": 45,
  "new_primary": "<former secondary workspace id>",
  "new_secondary": "<former primary workspace id>",
  "steps": [ "Validated secondary readiness", "Ran final delta sync", "Swapped roles" ]
}
```

---

## 21. Dashboard Pages

| Page | URL | Purpose |
|------|-----|---------|
| **Command Center** | `/` | Artifact type cards, sync controls, schedule, auto-sync |
| **Operational Drift** | `/drift` | Live comparison of primary vs secondary artifacts |
| **Data Assurance** | `/integrity` | Table count validation, type count validation |
| **Architecture** | `/architecture` | Static BCDR architecture diagram |
| **Regional Topology** | `/topology` | Health, lag, all-pairs overview |
| **Workspace Inventory** | `/inventory` | Per-pair artifact breakdown with type counts |
| **Managed Failover** | `/failover` | Failover/failback execution, checklist, RPO/RTO gauges, event history |
| **Data Gateways** | `/gateways` | On-premises gateway discovery, members, data sources |
| **Real-Time Intelligence** | `/rti` | RTI BCDR — Eventhouse/KQL sync, data replication, scheduling, connection audit |
| **OneLake Security** | `/security` | Data Access Roles (RLS/CLS) scan, sync status, replication to secondary |
| **Login** | `/login` | Microsoft interactive sign-in |
| **Setup** | `/setup` | Workspace pair configuration (add/remove/switch) |

### Architecture Notes
- **Most pages** use client-side JavaScript that calls `/api/*` endpoints and renders data dynamically.
- **`/integrity` (Data Assurance)** is an exception — it performs all OneLake DFS queries and item count comparisons **server-side** in the route handler and passes pre-computed `assurance_data` JSON to the template. There is no `/api/integrity` endpoint.
- **`/drift` (Operational Drift)** uses a hybrid approach — the route handler pre-fetches `sync_plan` data server-side and passes it to the template, though a `/api/sync-plan` endpoint also exists for API access.
- **Auth guard** — `_require_setup()` implements a redirect chain: unauthenticated users → `/login`, authenticated users with no pairs → `/setup`.

---

## 22. Security Considerations

| Area | Implementation |
|------|---------------|
| **Authentication** | MSAL interactive login — no hardcoded credentials |
| **Token storage** | `.msal_token_cache.bin` — gitignored, local only |
| **Workspace state** | `.workspace_state.json` — gitignored, contains no secrets |
| **No Service Principal in dashboard** | Uses delegated user permissions (what you can see, the dashboard can see) |
| **Secret files** | `.env`, `*.bin`, `*.json` state files all in `.gitignore` |
| **API calls** | Bearer token in Authorization header, HTTPS only |
| **Session** | Flask `secret_key` is random per startup (no persistent sessions) |
| **CORS** | Enabled via flask-cors (for local development) |

### What's Not Stored
- No passwords or client secrets in code
- No hardcoded workspace IDs in `app.py` (all from user selection)
- No sensitive data cached to disk (only token cache and workspace pair metadata)

---

## 23. OneLake Security — Data Access Roles (RLS/CLS)

**Page: `/security`** | **API: `/api/security/*`**

Full BCDR support for OneLake Data Access Roles, which enforce Row-Level Security (RLS) and Column-Level Security (CLS) on Lakehouse tables. The system scans roles on primary lakehouses, compares them with secondary, and replicates them with automatic ID remapping.

### How OneLake Data Access Roles Work

OneLake Data Access Roles are defined per-lakehouse and control access at the table and column level:

- **RLS (Row-Level Security)**: Restricts which tables a role can access via `decisionRules` with `Path` attribute constraints (e.g., `/Tables/smartclaims/gold_claims_summary`)
- **CLS (Column-Level Security)**: Restricts which columns within a table a role can read via `columnSecuritySchema` entries with `Permit` effect and specific column lists
- **Members**: Roles can have `fabricItemMembers` (workspace item access) and `microsoftEntraMembers` (Azure AD users/groups)

### Fabric REST API Endpoints Used

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/workspaces/{ws}/items/{id}/dataAccessRoles` | Read all Data Access Roles from a lakehouse |
| `PUT` | `/workspaces/{ws}/items/{id}/dataAccessRoles` | Atomic write of all roles to a lakehouse (create/update) |

> **Important:** These use the **Core Items API** (`/items/{id}/dataAccessRoles`), not the legacy lakehouse-specific API (`/lakehouses/{id}/dataAccessRoles`).

### Smart Scan Logic

The scan endpoint (`GET /api/security/rls-status`) optimizes API calls:

1. Fetches all lakehouses from both primary and secondary workspaces
2. For each primary lakehouse, reads Data Access Roles via `GET .../dataAccessRoles`
3. **Only checks the secondary** if the primary has actual roles (bronze/silver with no roles → secondary not queried)
4. Parses each role into: `name`, `tables` (OneLake paths), `cls_columns` (permitted columns per table), `members`
5. Detects `UniversalSecurityFeatureDisabledForWorkspace` errors and flags them as `security_disabled`

### Replication Flow

The replicate endpoint (`POST /api/security/replicate`) performs selective, targeted replication:

```
For each primary lakehouse:
  1. GET Data Access Roles from primary
  2. Skip if no roles (bronze, silver → skipped)
  3. Check secondary security status
     → If disabled, attempt auto-enable via PUT with DefaultReader role
     → If auto-enable fails (preview limitation), show portal instructions
  4. Remap fabricItemMembers.sourcePath:
     - Replace primary workspace ID → secondary workspace ID
     - Replace primary lakehouse ID → secondary lakehouse ID
  5. Atomic PUT all remapped roles to secondary lakehouse
```

### OneLake Security Preview Limitation

OneLake security is a **preview feature** that must be enabled per-workspace in the Fabric portal:

1. Open any lakehouse in the workspace in Fabric portal
2. Click **"Manage OneLake security (preview)"**
3. Enable security and click **Continue**

The REST API returns `UniversalSecurityFeatureDisabledForWorkspace` if the feature is not enabled. The dashboard provides an **"Enable Now"** button that attempts auto-enablement and falls back to portal instructions.

### Dashboard — Security Page

**URL:** `/security`

| Tab | What It Shows |
|-----|---------------|
| **Data Access Roles** | Sync status overview table (per-lakehouse: primary roles, secondary roles, In Sync / Drift / No Roles badge). Detailed role cards with table paths, CLS constraints, member counts. Auto-scans on page load. |
| **Replicate to Secondary** | One-click replication of all Data Access Roles from primary to secondary. Log output with per-role status. Auto-refreshes sync table after completion. |

### Example: CrestRLS Role

The CrestShield SmartClaims deployment uses this role on `gold_lakehouse`:

| Property | Value |
|----------|-------|
| **Role Name** | CrestRLS |
| **Tables** | `/Tables/smartclaims/gold_claims_summary`, `/Tables/smartclaims/gold_severity_scores` |
| **CLS** | Permit Read on 21 columns of `gold_claims_summary` (claim_year, claim_month, incident_type, claim_amount, ...) |
| **Members** | fabricItemMembers with ReadAll access |

A second **DefaultReader** role grants `*` (all tables) with ReadAll access to all fabricItemMembers.

### CLI Script

`scripts/sync_permissions.py` provides standalone **delta-only** permission sync:

```bash
# Sync all (Data Access Roles + workspace permissions + item permissions)
python scripts/sync_permissions.py

# Dry run
python scripts/sync_permissions.py --dry-run
```

The script runs three delta-sync phases:

**Phase 1 — Workspace Permissions (delta):**
- Builds lookup dicts keyed by `principal_id` on both primary and secondary
- **ADD** — Principal only on primary → `POST /roleAssignments` on secondary
- **CHANGE** — Same principal, different role → `PATCH /roleAssignments/{id}` on secondary
- **UNCHANGED** — Same principal + role → skip entirely
- **REMOVED** — Principal only on secondary → logged but **not auto-deleted** (safety)
- Returns counts: `permissions_added`, `permissions_updated`, `permissions_unchanged`, `permissions_removed_detected`

**Phase 2 — Item Permissions (delta):**
- For each primary item with a secondary counterpart (via `artifact_mapping.csv`)
- Fetches existing secondary permissions first → builds `(principal_id, role)` key set
- Only POSTs permissions missing from secondary; skips existing ones
- Returns counts: `item_permissions_added`, `item_permissions_unchanged`, `item_permissions_failed`

**Phase 3 — OneLake Data Access Roles (delta):**
- For each primary lakehouse with Data Access Roles
- Remaps workspace/item IDs in `fabricItemMembers.sourcePath`
- **Normalizes** both primary remapped roles and secondary existing roles (sorts lists, deterministic key ordering)
- **Compares** normalized role sets → if identical, **skips the PUT entirely**
- Only PUTs when roles actually differ
- Returns counts: `lakehouses_unchanged`, `roles_synced`, `roles_failed`

**Summary output:**
```
======================================================================
PERMISSIONS SYNC SUMMARY (DELTA)
======================================================================
--- OneLake Data Access Roles ---
  Lakehouses Processed:      3
  Lakehouses Unchanged:      2
  Lakehouses Skipped:        0
  Roles Synced:              1
  Roles Failed:              0
  Security Auto-Enabled:     0
--- Workspace Permissions ---
  Added:                     1
  Updated:                   0
  Unchanged:                 5
  Removed (detected only):   0
  Failed:                    0
--- Item Permissions ---
  Items Processed:           12
  Permissions Added:         2
  Permissions Unchanged:     18
  Permissions Failed:        0
======================================================================
```

---

## 24. ML Model & Experiment BCDR

### MLExperiment — Full Replication Support

MLExperiment items have **full API support** and replicate successfully:

| API | Supported | Used For |
|-----|-----------|----------|
| Create Item | ✅ | Create experiment in secondary |
| Get Definition | ✅ | Export experiment definition (runs, metadata) |
| Update Definition | ✅ | Update existing secondary experiment |
| Delete | ✅ | Cleanup |

**Replication flow for MLExperiment:**
1. Export definition from primary via `GET .../items/{id}/getDefinition`
2. Remap any workspace/item references in the definition
3. Create or update in secondary via definition APIs
4. Copy OneLake experiment artifacts (runs, metrics, model files) via azcopy

**Current status:** MLExperiment `severity_classifier` successfully replicates to secondary.

### MLModel — Platform Limitation (Cannot Replicate)

MLModel items have **severely limited API support**:

| API | Supported | Notes |
|-----|-----------|-------|
| Create Item | ✅ | Creates empty shell only — no definition payload |
| Get Definition | ❌ | **Not supported** per Microsoft docs |
| Update Definition | ❌ | **Not supported** per Microsoft docs |
| Delete | ✅ | — |
| List / Get | ✅ | — |

**Per [Microsoft Item Management Overview](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/item-management-overview):**

> MLModel: Create ✅ | Get Definition ❌ | Update Definition ❌

**Per [Create ML Model API](https://learn.microsoft.com/en-us/rest/api/fabric/mlmodel/items/create-ml-model):**

> "This API does not support create a machine learning model with definition."

### Why MLModel Gets Auto-Deleted (~60 seconds)

When an MLModel is created via the REST API:
1. Only an **empty container** is created (no model versions, no MLflow artifacts)
2. An MLModel is only meaningful when it has **model versions registered via MLflow** (`mlflow.register_model()`)
3. Fabric runs **internal garbage collection** that detects empty MLModel items with no registered versions
4. The empty MLModel is **automatically deleted** within approximately 60 seconds

Additionally, copying OneLake data (model artifacts) into the secondary MLModel via azcopy causes a **UUID mismatch** — the primary's model-version UUIDs don't match the secondary's MLflow state, triggering immediate deletion.

### Current Implementation

The sync script (`scripts/sync_ml_models_and_experiments.py`) handles this gracefully:

```python
# Model doesn't exist — create as EMPTY placeholder (no definition).
# Fabric validates MLModel definitions against the MLExperiment's
# internal MLflow state and deletes mismatches within ~60s.

# Skip azcopy for MLModel — copying data with primary model-version
# UUIDs causes Fabric's MLflow service to detect inconsistency and
# delete the item. MLModel is replicated by definition only;
# actual artifacts live in MLExperiment.
```

The dashboard shows MLModel at **0% mirrored (0/1)** because the empty shell gets garbage-collected.

### Correct Approach for MLModel BCDR

True MLModel replication requires **MLflow-level operations** from within a Fabric Spark session:

1. **In a Fabric notebook in the secondary workspace**, run:
   ```python
   import mlflow
   # Point to the secondary experiment
   mlflow.set_experiment("severity_classifier")
   # Re-register the model from the experiment's best run
   model_uri = "runs:/{run_id}/model"
   mlflow.register_model(model_uri, "crestshield_severity_classifier")
   ```

2. This creates a proper MLModel with registered versions that Fabric's MLflow service recognizes as valid

3. The model artifacts (weights, signatures, metadata) already exist in the replicated MLExperiment — the `register_model` call creates the linkage

### Summary

| Item | REST API Replication | Status | Workaround |
|------|---------------------|--------|------------|
| **MLExperiment** | ✅ Definition + azcopy artifacts | **Working** | None needed |
| **MLModel** | ❌ Empty shell auto-deleted | **Platform limitation** | Use `mlflow.register_model()` from a Fabric notebook in secondary workspace |

---

## 25. Real-Time Intelligence (RTI) BCDR

**Page: `/rti`** | **Module: `rti/`** | **API: `/api/rti/*`**

Full BCDR support for Microsoft Fabric Real-Time Intelligence artifacts: **Eventhouses, KQL Databases, KQL Querysets, and Eventstreams**. Includes artifact replication, KQL data sync, scheduled replication, and connection string management.

### RTI Architecture

```
Primary Workspace                        Secondary Workspace
┌──────────────────────────┐             ┌──────────────────────────┐
│ Eventhouse                │             │ Eventhouse (synced)       │
│  ├─ KQL Database A       │  ──Sync──▶  │  ├─ KQL Database A       │
│  │   ├─ SensorReadings   │  ──Data──▶  │  │   ├─ SensorReadings   │
│  │   └─ SystemEvents     │  ──Data──▶  │  │   └─ SystemEvents     │
│  └─ KQL Database B       │  ──Sync──▶  │  └─ KQL Database B       │
│ KQL Queryset              │  ──Sync──▶  │ KQL Queryset (remapped)  │
│ Eventstream               │  ──Sync──▶  │ Eventstream (remapped)   │
└──────────────────────────┘             └──────────────────────────┘
         │                                         │
         │  Kusto REST API (mgmt + query)          │
         │  Fabric REST API (items + definitions)  │
         └─────────────────────────────────────────┘
```

### RTI Artifact Sync

**Supported artifact types:** Eventhouse, KQLDatabase, KQLQueryset, Eventstream

The sync process for each RTI artifact type:

| Type | Sync Method | Notes |
|------|-------------|-------|
| **Eventhouse** | Create via Fabric Items API (`POST /items` with `type: Eventhouse`) | Container for KQL Databases; created first |
| **KQLDatabase** | Create via Fabric Items API with `creationPayload` containing `parentEventhouseItemId` | Must reference the **secondary** Eventhouse ID |
| **KQLQueryset** | Export definition → remap references → delta compare → update in secondary | Connection strings rewritten to secondary cluster URIs; skips update if definition unchanged |
| **Eventstream** | Export definition → remap destinations → delta compare → update in secondary | Destination workspace/item IDs rewritten; skips update if unchanged; source OAuth manual |

**Sync endpoint:** `POST /api/rti/sync`
- Pass `{"type": "Eventhouse"}` to sync one type, or `{}` to sync all RTI types
- Supports `{"dry_run": true}` for preview
- Items are matched by `displayName` — existing items are updated (if definition changed), missing items are created
- **Delta detection:** For existing items, fetches the secondary definition via `getDefinition` and compares with the remapped primary definition by `(path, payload)` set; skips `updateDefinition` when identical

### KQL Data Replication

**Endpoint:** `POST /api/rti/replicate-kql-data`

Replicates actual data from primary KQL Database tables to secondary using the Kusto REST API:

```
For each table in primary KQL Database:
  1. Export schema via .show table {name} cslschema
  2. Create/merge table in secondary: .create-merge table {name} ({schema})
  3. Clear existing data: .clear table {name} data
  4. Query data from primary: {table} | take {max_rows}
  5. Batch ingest via: .ingest inline into table {name} <| {csv_data}
     (500 rows per batch to respect Kusto command size limits)
```

**Key features:**
- **Clear-before-replicate** — Uses `.clear table {name} data` (Fabric KQL syntax) before re-ingesting to prevent duplicate rows and ensure exact data parity
- **Multi-database support** — Pass `database=ALL` to replicate all KQL Databases in the Eventhouse in a single call
- **Table filtering** — Pass `{"table": "SensorReadings"}` to sync a specific table only
- **Safety limit** — `max_rows` parameter (default: 10,000) prevents accidental full-table exports on large datasets
- **Batch ingestion** — Data is split into 500-row batches for reliable inline ingestion

**Kusto authentication:** Separate MSAL token scope (`https://kusto.kusto.windows.net/.default`) acquired via the same auth flow (interactive or Service Principal). Tokens are cached and refreshed automatically.

### Scheduled KQL Replication

**Endpoints:** `GET/POST /api/rti/schedule`

Configurable timer that automatically replicates all KQL Database data at regular intervals:

| Setting | Range | Default |
|---------|-------|---------|
| Interval | 15 min – 1440 min (24 hrs) | 60 min |
| Enabled | true/false | false |

**How it works:**
1. A Python `threading.Timer` runs the replication function at the configured interval
2. Each run calls the same logic as `POST /api/rti/replicate-kql-data` with `database=ALL`
3. State is persisted to `.rti_schedule.json` (survives server restart)
4. The schedule panel on the RTI page shows: enabled status, interval, last run timestamp, run count, total rows copied

### Connection String Management

RTI artifacts (especially KQL Querysets and Eventstreams) contain embedded references to workspace IDs, item IDs, and Kusto cluster URIs. After sync, these may still point to the primary workspace.

**Audit endpoint:** `GET /api/rti/connections`
- Exports definitions of all secondary KQL Querysets, Eventstreams, and KQL Databases
- Scans for stale primary references: workspace IDs, item IDs, queryServiceUri, ingestionServiceUri
- Returns per-artifact report with specific stale references found

**Fix endpoint:** `POST /api/rti/fix-connections`
- Builds a full replacement map: primary workspace ID → secondary, primary item IDs → secondary (matched by name), primary cluster URIs → secondary
- Re-exports each secondary artifact definition, applies all replacements, and updates the definition via `POST /items/{id}/updateDefinition`

### Sample Data Ingestion

**Endpoint:** `POST /api/rti/ingest-sample-data`

Creates two sample tables for testing:

| Table | Columns | Rows |
|-------|---------|------|
| **SensorReadings** | Timestamp, DeviceId, Temperature, Humidity, Pressure, Location, Status | 50 |
| **SystemEvents** | Timestamp, EventType, Source, Severity, Message, CorrelationId | 30 |

Uses `.create-merge table` (idempotent) + `.ingest inline into table` via the Kusto management endpoint.

### RTI Validation

**Endpoint:** `GET /api/rti/validate`

Checks:
1. **Missing artifacts** — Every primary RTI artifact has a matching secondary by name
2. **Connection string health** — No stale primary references in secondary definitions
3. Returns overall status: `pass`, `warning`, or `fail`

### RTI Dashboard Page

**URL:** `/rti`

The RTI page provides a complete management interface:

| Section | What It Shows |
|---------|---------------|
| **Summary Cards** | 4 cards (Eventhouse, KQL Database, KQL Queryset, Eventstream) with primary/secondary counts and sync % |
| **Artifact Tables** | Per-type collapsible tables showing individual artifacts with IDs, sync status (✓/✗) |
| **KQL Data Section** | Database dropdown, table comparison grid, row counts per table, Ingest Sample Data + Replicate Data buttons |
| **Scheduled Replication** | Enable/disable toggle, interval selector (15min–24hr), last run timestamp, run count, total rows copied |
| **Connection Strings** | Audit button (scans for stale refs), Fix button (rewrites all stale references), per-artifact results |

### Dummy RTI Artifacts (Testing)

**Create:** `POST /api/rti/create-dummy`
- Creates: RTI_Demo_Eventhouse, RTI_Demo_KQLDatabase (parented to the Eventhouse), RTI_Demo_KQLQueryset, RTI_Demo_Eventstream
- Useful for testing the full RTI BCDR workflow without real artifacts

**Cleanup:** `POST /api/rti/cleanup-dummy`
- Removes all dummy artifacts by name from both primary and secondary workspaces

### Standalone CLI Scripts (`rti/`)

| Script | Purpose |
|--------|---------|
| `rti/sync_rti.py` | Sync RTI artifacts from primary to secondary (standalone, no Flask) |
| `rti/validate_rti.py` | Validate RTI sync status (standalone) |
| `rti/create_dummy_rti.py` | Create dummy RTI artifacts for testing |

---

## 26. Roadmap — Data Warehouse BCDR

The following capabilities are planned but not yet fully implemented.

**Current state:** `sync_warehouses.py` contains skeleton code. Warehouses are in the `NOT_SUPPORTED` set in `app.py`.

| Work Item | Approach | Status |
|-----------|----------|--------|
| **WH-1: Schema extraction & replay** | Use `executeQueries` REST API to extract DDL from `INFORMATION_SCHEMA` views + `sys.sql_modules`, replay on secondary | Not started |
| **WH-2: Data sync** | CETAS export from primary (Parquet to OneLake) → `COPY INTO` on secondary; incremental via watermark columns | Not started |
| **WH-3: Security replication** | Extract RLS (`sys.security_policies`), CLS (`sys.database_permissions`), DDM (`sys.masked_columns`), roles (`sys.database_principals`), replay on secondary | Not started |
| **WH-4: Dashboard integration** | Remove from `NOT_SUPPORTED`, wire into status cards, drift detection, failover flow | Not started |

**Key design decision:** The `executeQueries` API (`POST /v1/workspaces/{ws}/warehouses/{wh}/executeQueries`) is token-based and uses the same Azure AD auth as the rest of the tool — no TDS endpoint, ODBC driver, or SQL auth needed.

### Known Limitations
- Eventstream source OAuth credentials require manual re-authentication after failover
- Sensitivity label sync is detection-only (applying labels requires Microsoft Information Protection SDK)
- **MLModel** cannot be replicated via REST API — Fabric auto-deletes empty shells within ~60s. Requires `mlflow.register_model()` from a Fabric notebook in the secondary workspace (see [Section 24](#24-ml-model--experiment-bcdr))
- OneLake security (Data Access Roles) is a preview feature — must be enabled per-workspace in Fabric portal before API calls work (see [Section 23](#23-onelake-security--data-access-roles-rlscls))

---

## 27. Environment BCDR

**Script: `scripts/sync_environments.py`** | **Dashboard: Replication engine supports `Environment` type**

Fabric Environments contain Spark runtime settings, custom libraries (PyPI/conda/wheel/jar), and compute configurations. The sync process handles the full lifecycle including the mandatory **publish** step.

### Why Environments Need Special Handling

Unlike most artifact types, an Environment definition update doesn't take effect immediately. After `updateDefinition`, the Environment must be **published** — a long-running operation that installs libraries, validates dependencies, and bakes the Spark session configuration. Without publishing, the secondary Environment will have stale library versions.

### Sync Flow

```
1. Export definition from primary:
   POST /workspaces/{primary_id}/items/{env_id}/getDefinition
   → Returns Spark settings, libraries (PyPI, conda, wheel, jar), compute config

2. Check if secondary Environment exists (matched by displayName):
   a) Exists → POST /workspaces/{secondary_id}/items/{s_id}/updateDefinition
   b) Missing → POST /workspaces/{secondary_id}/items (create with definition)

3. Publish the Environment:
   POST /workspaces/{secondary_id}/items/{s_id}/staging/publish
   → Returns 200 with publishInfo or 202 (LRO)

4. Poll publish status:
   GET /workspaces/{secondary_id}/items/{s_id}/staging/publishInfo
   → Poll until state != "Running" (typically 2–5 minutes for library installs)

5. Verify publish completed:
   state == "Success" → Environment is live on secondary
   state == "Failed"  → Log error, report partial success
```

### Fabric REST API Endpoints Used

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `POST` | `/workspaces/{ws}/items/{id}/getDefinition` | Export Environment definition |
| `POST` | `/workspaces/{ws}/items/{id}/updateDefinition` | Update definition on secondary |
| `POST` | `/workspaces/{ws}/items/{id}/staging/publish` | Trigger publish after definition update |
| `GET` | `/workspaces/{ws}/items/{id}/staging/publishInfo` | Poll publish status (Running/Success/Failed) |

### CLI Usage

```bash
# Sync all Environments from primary to secondary
python scripts/sync_environments.py

# Dry run (preview only)
python scripts/sync_environments.py --dry-run
```

### Dashboard Integration

The `replicate_items_by_type("Environment")` function in `app.py` includes a post-replication publish hook:
- After creating/updating an Environment in secondary, calls `_poll_environment_publish()`
- Polls every 10 seconds for up to 10 minutes
- Reports publish progress in the sync progress tracker

### What Gets Replicated

| Component | Included in Definition | Notes |
|-----------|----------------------|-------|
| Spark runtime version | ✅ | e.g., Spark 3.5, Runtime 1.3 |
| PyPI libraries | ✅ | Package name + version |
| Conda libraries | ✅ | Conda channels + packages |
| Custom wheel/jar files | ✅ | Base64-encoded in definition parts |
| Compute pool settings | ✅ | Node size, auto-scale config |
| Environment variables | ✅ | Key-value pairs |

### Performance

- Definition export: ~1 second
- Definition update: ~2 seconds
- **Publish: 2–5 minutes** (library installation is the bottleneck)
- Total per-Environment: ~3–6 minutes

---

## 28. Delta-Only Sync (Permissions, RTI, Definitions)

All sync operations now compare primary vs secondary state **before** making changes. This avoids redundant API calls, reduces rate-limiting risk, and provides clear change metrics.

### 28.1 Workspace Permissions — Delta Sync

**File:** `scripts/sync_permissions.py` → `sync_workspace_permissions()`

| Scenario | Detection | Action |
|----------|-----------|--------|
| **New principal** | `principal_id` exists on primary but not secondary | `POST /roleAssignments` |
| **Role changed** | Same `principal_id`, different `role` | `PATCH /roleAssignments/{id}` |
| **Unchanged** | Same `principal_id` + same `role` | Skip (no API call) |
| **Removed** | `principal_id` exists on secondary but not primary | Log warning (not auto-deleted for safety) |

**Key design decision:** Removed principals are **detected but not auto-deleted** to prevent accidental permission loss. The delta report includes `permissions_removed_detected` count so operators can review and manually remove if intended.

### 28.2 Item Permissions — Delta Sync

**File:** `scripts/sync_permissions.py` → `sync_item_permissions()`

For each primary item with a mapping to a secondary item:
1. Fetches existing secondary item permissions via `GET /items/{id}/permissions`
2. Builds a `(principal_id, role)` key set from secondary
3. Only POSTs permissions that are **missing** from secondary
4. Reports `item_permissions_added` vs `item_permissions_unchanged`

### 28.3 OneLake Data Access Roles — Delta Sync

**File:** `scripts/sync_permissions.py` → `sync_data_access_roles()`

For each primary lakehouse with roles:
1. Remaps `fabricItemMembers.sourcePath` (workspace + item IDs)
2. **Normalizes** both sides for comparison:
   - Sorts `decisionRules` by `effect`
   - Sorts `permission` arrays by `attributeName`
   - Sorts `microsoftEntraMembers` by `tenantId + objectId`
   - Sorts `fabricItemMembers` by `sourcePath`
3. Compares normalized primary (remapped) vs secondary role sets
4. If identical → **skips PUT entirely** (log: `[DELTA SKIP]`)
5. If different → applies PUT (log: `[DELTA UPDATE]`)

### 28.4 RTI Artifact Definitions — Delta Sync

**File:** `rti/sync_rti.py` → `sync_artifact_type()`

For each RTI artifact that already exists in secondary:
1. Exports primary definition → remaps connection strings
2. Exports secondary definition via `getDefinition`
3. Compares by `(path, payload)` set (order-independent via `_definitions_equal()`)
4. If identical → **skips `updateDefinition`** (log: `= unchanged, skipping`)
5. If different → applies `updateDefinition`

This applies to: Eventhouse, KQLDatabase, KQLQueryset, Eventstream

### 28.5 Performance Impact

| Scenario | Before (Full Sync) | After (Delta) |
|----------|--------------------|----- ---------|
| 5 workspace permissions, none changed | 5 POST calls | 0 calls (5 skipped) |
| 10 lakehouses with DAR, 2 changed | 10 PUT calls | 2 PUT calls (8 skipped) |
| 20 RTI artifacts, 3 changed | 20 updateDefinition calls | 3 updateDefinition calls |
| 50 item permissions, 5 new | 50 POST calls | 5 POST calls (45 skipped) |

Delta sync reduces API calls by **80–95%** in steady-state operations when most items are already in sync.

---

## 29. Bulk Item Definition APIs (Beta)

**Script:** `scripts/bulk_sync.py` | **API:** `POST /api/bcdr/bulk-sync`

Microsoft Fabric has announced two new **beta** APIs that export/import all workspace item definitions in a single call:

| API | Endpoint | Purpose |
|-----|----------|---------|
| **Bulk Export Item Definitions** | `POST /workspaces/{ws}/items/bulkExportItemDefinitions` | Export all item definitions from a workspace in one LRO |
| **Bulk Import Item Definitions** | `POST /workspaces/{ws}/items/bulkImportItemDefinitions` | Import/update all definitions into a workspace in one LRO |

### The Problem These APIs Solve

Current replication makes **2×N** sequential API calls (one `getDefinition` + one `createItem`/`updateDefinition` per artifact). For a workspace with 50 items, that's 100+ individual API calls, each subject to rate limiting.

With bulk APIs: **2 total calls** — one bulk export, one bulk import.

### Implementation: Automatic Fallback

Since the APIs are still in beta and may not be available on all tenants, our implementation uses a **try-bulk-then-fallback** strategy:

```
Step 1: Try bulk export from primary
        → If 404 or error → fall back to per-item getDefinition
        → If success → got all definitions in one call

Step 2: Remap all workspace/item IDs in definitions
        → Same connection map logic as existing replication

Step 3: Delta comparison
        → For each item, compare remapped primary definition with secondary
        → Skip items where definitions are identical (unchanged)

Step 4: Try bulk import to secondary
        → If 404 or error → fall back to per-item createItem/updateDefinition
        → If success → all items imported in one call
```

### Supported Artifact Types

The bulk sync handles all types that support `getDefinition` / `updateDefinition`:

```
Notebook, DataPipeline, Report, SemanticModel, SparkJobDefinition,
Environment, Eventhouse, KQLDatabase, KQLQueryset, KQLDashboard,
Eventstream, GraphQLApi, Dataflow, CopyJob
```

Types without definition support (`MLModel`, `MLExperiment`, `Lakehouse`, `Warehouse`, `SQLEndpoint`) are excluded.

### CLI Usage

```bash
# Bulk sync all definition-supported items
python scripts/bulk_sync.py

# Preview without executing
python scripts/bulk_sync.py --dry-run

# Sync only specific types
python scripts/bulk_sync.py --type Notebook --type Report
```

### Dashboard API

```
POST /api/bcdr/bulk-sync
Body: { "types": ["Notebook", "Report"], "dryRun": false }
→ Runs in background thread
→ Poll progress via GET /api/bcdr/replicate/progress/BulkSync
```

### Result Summary

```
======================================================================
BULK SYNC SUMMARY
======================================================================
  Mode:        bulk          # or "per-item" if beta APIs not available
  Exported:    45
  Created:     3
  Updated:     5
  Unchanged:   37
  Skipped:     0
  Failed:      0
======================================================================
```

### Key Implementation Files

| File | What |
|------|------|
| `common.py` → `bulk_export_definitions()` | Wrapper for `POST .../bulkExportItemDefinitions` |
| `common.py` → `bulk_import_definitions()` | Wrapper for `POST .../bulkImportItemDefinitions` |
| `scripts/bulk_sync.py` → `bulk_sync()` | Full orchestration: export → remap → delta compare → import |
| `app.py` → `/api/bcdr/bulk-sync` | REST endpoint running bulk sync in background |

---

## 30. Out-of-Definition Settings — Gap Analysis

When replicating artifacts using `getDefinition` / `updateDefinition`, only the **definition** is carried. Several critical operational settings live **outside** the definition and are not replicated by default.

### Cross-Cutting Gaps (All Artifact Types)

| Setting | Priority | API | Status |
|---------|----------|-----|--------|
| **Job Schedules** | P0 | `GET/POST /items/{id}/jobs/{jobType}/schedules` | Failover script handles (pause/resume). Full sync not yet implemented. |
| **Sensitivity Labels** | P1 | `GET /items/{id}` (read), Admin API (write) | Detection in drift page. Applying requires Admin API or MIP SDK. |
| **Item Description** | P2 | `PATCH /items/{id}` with `{description}` | Not yet synced. |
| **Tags** | P2 | Fabric Tags API | Not yet synced. |
| **Endorsement** | P2 | Admin API — set endorsement status | Not yet synced. |

### SemanticModel-Specific Gaps

| Setting | Priority | API | Notes |
|---------|----------|-----|-------|
| **Refresh Schedule** | P0 | `GET/PATCH /groups/{ws}/datasets/{id}/refreshSchedule` | Power BI Datasets API. Not carried in TMDL definition. |
| **DirectQuery Refresh Schedule** | P0 | `GET/PATCH /groups/{ws}/datasets/{id}/directQueryRefreshSchedule` | For DQ models. |
| **Data Source Credentials** | P0 | `POST /groups/{ws}/datasets/{id}/Default.TakeOver` + credential APIs | Credentials are never in definition. Require manual re-auth or SP takeover. |
| **Gateway Binding** | P1 | `POST /groups/{ws}/datasets/{id}/Default.BindToGateway` | Must rebind if using on-prem gateway. |
| **Parameters** | P1 | `POST /groups/{ws}/datasets/{id}/Default.UpdateParameters` | Runtime parameters (server name, database name). |
| **Query Scale-Out** | P2 | `PATCH /groups/{ws}/datasets/{id}` with `queryScaleOutSettings` | Read replica configuration. |

### What IS Carried in SemanticModel TMDL Definition

The following are safe — they're part of the TMDL definition exported via `getDefinition`:
- Tables, columns, measures, calculated columns
- Relationships (all types)
- M expressions (data source connections)
- RLS roles and row filters
- Storage mode (Import/DirectQuery/Dual)
- Calculation groups
- Perspectives, translations
- Data source connection strings (but not credentials)

### Notebook / DataPipeline / SparkJobDefinition Gaps

| Setting | Priority | API | Notes |
|---------|----------|-----|-------|
| **Job Schedule** | P0 | `GET/POST /items/{id}/jobs/{jobType}/schedules` | Unified Fabric Job Scheduler API. |

### Environment — Fully Covered

All Environment settings are part of the definition and replicated by `sync_environments.py`:
- Spark runtime version, PyPI/conda libraries, custom wheel/jar files
- Compute pool settings, environment variables
- **Publish step** ensures libraries are installed

### KQL Database Gaps

| Setting | Priority | Notes |
|---------|----------|-------|
| **Retention policies** | P1 | Set via KQL `.alter table ... policy retention`. Not in item definition. |
| **Materialized views** | P1 | Defined via KQL management commands. |
| **Continuous export** | P1 | Configured via KQL management commands. |

### Lakehouse Gaps

| Setting | Priority | Status |
|---------|----------|--------|
| **OneLake Shortcuts** | P1 | Partially covered — `examples/create_shortcuts.py` demonstrates the API. Not yet automated in sync. |

### Recommended Priority Order for Implementation

1. **P0 — Job Schedules** across all schedulable item types (Notebook, DataPipeline, SparkJobDefinition, Dataflow) using the unified Fabric Job Scheduler API
2. **P0 — SemanticModel Refresh Schedules** via Power BI Datasets API
3. **P1 — Sensitivity Labels** (read + apply via Admin API)
4. **P1 — Gateway Rebinding** for models using on-prem gateways
5. **P2 — Item descriptions, tags, endorsement** (low risk, easy to implement)

---

## 31. Script Consolidation & Delegation

To avoid duplicate logic across multiple sync scripts, several scripts have been consolidated to delegate to canonical implementations.

### Consolidation Map

| Script | Before | After |
|--------|--------|-------|
| `sync_ml_models_and_experiments.py` | Had its own `sync_environments()` function (75 lines) | Imports from `sync_environments.py` via delegation |
| `sync_kql_databases.py` | Had its own `sync_kql_querysets()` function (100+ lines) | Thin wrapper → delegates to `rti/sync_rti.py` |
| `sync_eventstreams.py` | Had its own `sync_eventstreams()` function (90+ lines) | Thin wrapper → delegates to `rti/sync_rti.py` |

### How Delegation Works

**Environment sync delegation** (`sync_ml_models_and_experiments.py`):
```python
from sync_environments import sync_environments as _sync_environments_dedicated
# Calls _sync_environments_dedicated() instead of local implementation
```

**RTI delegation** (`sync_kql_databases.py`, `sync_eventstreams.py`):
```python
from rti.sync_rti import sync_all_rti
# Passes types=["KQLDatabase", "KQLQueryset"] or types=["Eventstream"]
```

### Benefits

- **Single source of truth** — Each sync type has exactly one canonical implementation
- **Delta logic applied once** — Definition comparison, delta detection only needs maintenance in one place
- **Consistent behavior** — RTI sync via CLI (`rti/sync_rti.py`), via thin wrappers (`sync_kql_databases.py`), and via dashboard (`/api/rti/sync`) all use the same code path
- **Reduced maintenance** — Bug fixes and improvements propagate automatically

---

## 32. Test Coverage

The project has two pytest test suites covering the lakehouse sync and failback implementations. Run with:

```bash
python -m pytest scripts/test_failback_sync.py scripts/test_forward_sync_notebook.py -v
```

**Latest result: 64/64 tests passing.**

### `scripts/test_failback_sync.py` — 33 tests

Covers all reverse sync (failback) functionality in `scripts/sync_lakehouses.py` and `scripts/failback.py`:

| Test Class | Tests | What It Covers |
|---|---|---|
| `TestSyncStrategy` | 1 | `SyncStrategy` enum has FAST_COPY, ACTIVE_REPLICATION, ONELAKE_SHORTCUTS, GRS_PASSTHROUGH |
| `TestAzcopyIncremental` | 5 | `sync_lakehouse_via_azcopy()` passes `--include-after=<ISO>` when `since_timestamp` provided; dry-run skips azcopy |
| `TestFastCopyNotebookBuilder` | 8 | `_build_fast_copy_notebook_ipynb()` generates cells with `modifyTime > failover_ts_ms` filter, `_incremental_cp()`, `_last_checkpoint` reset, correct workspace IDs |
| `TestFastCopyDryRun` | 1 | `sync_lakehouse_fast_copy()` skips notebook creation when `dry_run=True` |
| `TestFastCopyNotebookLifecycle` | 3 | `_create_and_run_notebook()` creates notebook, triggers RunNotebook job, always deletes in finally |
| `TestReverseSyncLakehouses` | 6 | `reverse_sync_lakehouses()` matches lakehouses by name, dispatches to correct strategy, handles no-match case |
| `TestFailbackTimestampWiring` | 3 | `failback.py` reads `failover_timestamp` from `data/failover_log.json`, passes to `reverse_sync_artifacts()` |
| `TestEndToEndDryRun` | 2 | Full failback dry-run with both FAST_COPY and ACTIVE_REPLICATION strategies |
| `TestTimestampConversion` | 2 | ISO to epoch ms conversion, epoch ms to ISO conversion correctness |

### `scripts/test_forward_sync_notebook.py` — 31 tests

Covers the forward sync notebook generation in `app.py` (`_generate_per_lh_sync_notebook_ipynb()`):

| Test Class | Tests | What It Covers |
|---|---|---|
| `TestNotebookStructure` | 5 | Notebook is valid `.ipynb` JSON, has 6+ cells, config cell sets `SYNC_ENGINE` and workspace IDs |
| `TestFastCopyEngine` | 11 | `fast_copy` default, `SYNC_ENGINE = "fast_copy"` in config, `fast_copy_full()`, `fast_copy_incremental()`, `_incremental_cp()`, `_fc_load_state()` / `_fc_save_state()`, state path `_bcdr_sync_state`, auto-mode logic (incremental when `last_ms > 0`, full when `last_ms == 0`), `sync_files_section()` uses `_incremental_cp` |
| `TestSparkCDFEngine` | 8 | `spark_cdf` engine generates CDF cells, `get_delta_version()`, `full_sync_table()`, `incremental_sync_table()`, `enable_cdf()`, `SYNC_ENGINE = "spark_cdf"` in config |
| `TestBothEngines` | 4 | Both engines contain `sync_files_section()`, catalog registration cell present, `lh_name` in notebook, `SYNC_MODE` variable present |
| `TestDeploySyncArtifactsUsesDefaultEngine` | 3 | `deploy_sync_artifacts()` calls notebook generator, defaults to `fast_copy`, all 3 lakehouses get notebooks |
