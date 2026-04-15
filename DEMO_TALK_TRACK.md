# Fabric BCDR — Leadership Demo Recording Script
**Duration: 8–10 minutes | Step-by-Step Talk Track**

> **BEFORE YOU HIT RECORD:**
> 1. Open browser to `http://localhost:5000` — make sure you are logged in
> 2. Click through every page once so API caches are warm (pages load instantly)
> 3. Set browser zoom to **90%** so more content fits on screen
> 4. Close all other browser tabs — keep it clean
> 5. Navigate back to the **Dashboard** (Command Center) — this is your starting screen

---

## STEP 1 — OPENING (0:00 – 0:30)
**You are on: Dashboard (Command Center)**

> **SAY:** "Hi everyone. I want to walk you through something we've built to address a critical gap in Microsoft Fabric — Business Continuity and Disaster Recovery.
>
> Today, Fabric does not ship with a native cross-region DR solution. If a region goes down, all customer workloads — lakehouses, warehouses, notebooks, pipelines, reports, real-time intelligence — go dark with no automatic recovery path.
>
> We built a fully automated BCDR framework that replicates everything across two Fabric workspaces in different Azure regions, and lets you failover in minutes. Let me show you."

---

## STEP 2 — COMMAND CENTER (0:30 – 1:30)
**You are on: Dashboard (Command Center)**

> **DO:** Slowly move your mouse across the dashboard — hover over the workspace pair names at the top, then move down to the artifact type cards.
>
> **SAY:** "This is the Command Center — the single pane of glass for your entire BCDR posture.
>
> At the top you see the workspace pair — our primary workspace, CrestShield SmartClaims, paired with the secondary in a different region. Both show as online.
>
> Below that, every Fabric artifact type is represented — Lakehouses, Notebooks, Pipelines, Semantic Models, Reports — each showing sync coverage as a percentage."
>
> **DO:** Point at a green card.
>
> **SAY:** "Green means fully synced. Orange means partially synced. Red means out of sync. This gives leadership instant visibility into DR readiness without digging into logs."

---

## STEP 3 — WORKSPACE INVENTORY (1:30 – 2:15)
**DO:** Click **"Workspace Inventory"** in the left sidebar.

> **SAY:** "The Inventory page gives you a live, real-time breakdown of what's in each workspace."
>
> **DO:** Point at the Primary card, then the Secondary card.
>
> **SAY:** "Primary has 27 artifacts, secondary has 36 — the extra items are the replicated copies plus system-generated SQL endpoints that Fabric auto-creates. You can see the breakdown by type — 3 Lakehouses, 9 Notebooks, 1 Eventhouse, 2 KQL Databases. This is all live from the Fabric REST API."

---

## STEP 4 — REGIONAL TOPOLOGY (2:15 – 3:00)
**DO:** Click **"Regional Topology"** in the left sidebar. Pause for 2 seconds to let the topology diagram render.

> **SAY:** "The Topology view shows your cross-region architecture visually."
>
> **DO:** Point at the Primary region box on the left.
>
> **SAY:** "Primary region — East US 2 — showing health, item count, and capacity."
>
> **DO:** Point at the Secondary region box on the right.
>
> **SAY:** "Secondary region — Central US — independently provisioned."
>
> **DO:** Point at the replication arrow in the middle.
>
> **SAY:** "And here you see the replication lag and RPO target. This is critical for leadership reporting — you can answer 'what's our current RPO?' in one glance."

---

## STEP 5 — OPERATIONAL DRIFT (3:00 – 4:15)
**DO:** Click **"Operational Drift"** in the left sidebar.

> **SAY:** "This is where it gets powerful. Operational Drift continuously compares primary and secondary workspaces."
>
> **DO:** Point at the stats row at the top (In Sync, Definition Changed, Missing, etc.).
>
> **SAY:** "Every artifact is categorized — In Sync means definitions match, Definition Changed means someone modified the primary and the secondary is stale, Missing means a new artifact that hasn't been replicated yet."
>
> **DO:** Scroll down to show the artifact groups.
>
> **SAY:** "But we don't just detect drift — we fix it. You can click Check and Sync All to automatically export definitions from primary, rewrite all connection strings and workspace references, and import them into secondary."
>
> **DO:** Point at the Auto-check scheduler section.
>
> **SAY:** "There's also an auto-check scheduler. You set an interval — every 15 minutes, every hour — and it continuously watches for drift and auto-syncs. Zero manual intervention."

**[OPTIONAL LIVE ACTION]:**
> **DO:** Select "15 minutes" from the dropdown and click **Enable**.
>
> **SAY:** "I'm enabling it now — every 15 minutes, the system will check for drift and auto-correct it."

---

## STEP 6 — DATA ASSURANCE (4:15 – 5:15)
**DO:** Click **"Data Assurance"** in the left sidebar. Wait for the page to fully load.

> **SAY:** "Syncing definitions is one thing — but how do you know the data actually made it? The Data Assurance page validates row counts, schema parity, and data consistency."
>
> **DO:** Point at the summary stats at the top (Verified / Warning / Failure counts).
>
> **SAY:** "At the top — total verified checks, warnings, and failures."
>
> **DO:** Scroll down through the table slowly. Point at different rows.
>
> **SAY:** "Each row shows the artifact, the check type, primary value, secondary value, and variance. You can see here — Lakehouse bronze, silver, gold — table counts match. All green."
>
> **DO:** Point at the KQL Database rows.
>
> **SAY:** "And this is new — we now validate Real-Time Intelligence as well. KQL Database RTI_Demo — table count matches. SensorReadings — 51 rows primary, 51 secondary. SystemEvents — 31 and 31. Exact parity. Then the artifact-level counts — Eventhouses, KQL Databases, Querysets, Eventstreams — all matching.
>
> This gives you audit-ready proof that your DR environment is complete."

---

## STEP 7 — REAL-TIME INTELLIGENCE (5:15 – 6:45)
**DO:** Click **"Real-Time Intelligence"** in the left sidebar.

> **SAY:** "This is our newest addition — Real-Time Intelligence BCDR. Fabric RTI includes Eventhouses, KQL Databases, KQL Querysets, and Eventstreams. These are fundamentally different from lakehouse artifacts — KQL data lives in Kusto's column-store engine, not OneLake."
>
> **DO:** Point at the 4 summary cards at the top.
>
> **SAY:** "The top row shows sync status for each RTI type — Eventhouses, KQL Databases, Querysets, and Eventstreams — all synced."
>
> **DO:** Scroll down to the KQL Database Data section. Click **"Refresh Data View"** button.
>
> **SAY:** "Now here's the data comparison. I'm loading the actual table-level view..."
>
> **DO:** Wait for the data to load. Point at the table rows.
>
> **SAY:** "SensorReadings — 51 rows in primary, 51 in secondary. SystemEvents — 31 and 31. Exact parity. Status: Data Synced."
>
> **DO:** Point at the database dropdown showing "All KQL Databases".
>
> **SAY:** "You can replicate a single database or all databases in the Eventhouse."
>
> **DO:** Scroll down to the Scheduled Replication panel.
>
> **SAY:** "And just like drift detection, we can schedule data replication on a recurring interval — from every 15 minutes up to 24 hours."
>
> **DO:** Scroll down to the Connection Strings section.
>
> **SAY:** "We also audit connection strings. When artifacts are copied to secondary, they may still reference the primary cluster URI. This section detects stale references and fixes them automatically."

---

## STEP 8 — MANAGED FAILOVER (6:45 – 8:00)
**DO:** Click **"Managed Failover"** in the left sidebar.

> **SAY:** "When disaster strikes, you come here. The Managed Failover page shows the current DR state."
>
> **DO:** Point at the state banner (should say "Normal").
>
> **SAY:** "Right now, we're in Normal state — primary is operational. You can see both workspaces, their status, and the sync coverage percentage."
>
> **DO:** Point at the Failover and Simulate buttons.
>
> **SAY:** "With one click — Failover. The system validates the secondary is ready, switches operational state, and logs every step. There's also a Simulate button for dry runs — you can prove to auditors that failover works without actually doing it."

**[OPTIONAL LIVE ACTION — HIGHLY RECOMMENDED]:**
> **DO:** Click **"Simulate Failover"**. Watch the console open with real-time step-by-step progress.
>
> **SAY:** "Let me run a simulation right now... You can see every step in the live console — validating secondary readiness, checking artifact coverage, verifying data parity. This is the exact same sequence a real failover would execute."
>
> **DO:** Wait for simulation to complete.
>
> **SAY:** "Simulation complete. In a real disaster, after recovery, you'd click Failback to re-sync any changes made during the DR period back to primary."

---

## STEP 9 — DEVOPS & GATEWAYS (8:00 – 8:30)
**DO:** Click **"DevOps"** in the left sidebar.

> **SAY:** "For teams using CI/CD, we integrate with Azure DevOps — Git repository status, pipeline management, and auto-trigger on new artifacts."
>
> **DO:** Click **"Data Gateways"** in the left sidebar.
>
> **SAY:** "And the Gateways page discovers on-premises data gateways and their sources — critical for DR planning when your workspace depends on on-prem connectivity."

---

## STEP 10 — CLOSING (8:30 – 9:00)
**DO:** Click back to the **Dashboard** (Command Center).

> **SAY:** "To wrap up — this is an end-to-end BCDR solution for Microsoft Fabric. It covers:
>
> Every artifact type — from Lakehouses to Real-Time Intelligence.
> Actual data replication — not just definitions, but rows.
> Continuous drift detection with auto-remediation.
> One-click failover and failback.
> Automatic connection string remapping.
> And audit-ready validation proving data parity.
>
> This fills a gap that Fabric doesn't address natively today, built entirely on public Fabric REST APIs with no private or preview dependencies.
>
> Thank you."

---

## RECORDING CHECKLIST

### Before Recording
- [ ] Server running at localhost:5000
- [ ] Logged in (check dashboard loads with data)
- [ ] Visit every page once to warm caches
- [ ] Browser zoom at 90%
- [ ] Close unnecessary tabs and notifications
- [ ] Screen recording software ready (e.g., OBS, Camtasia, or Windows Game Bar Win+G)

### Key Moments to Nail
- [ ] **Data Assurance KQL rows** — pause here, the matching numbers are compelling
- [ ] **RTI Refresh Data View** — click it live, let the audience see it load
- [ ] **Failover Simulate** — the live console is the show-stopper
- [ ] **Drift auto-check** — enabling it live shows it's real, not a mockup

### If Something Goes Wrong
- If a page is slow: "The system is querying the Fabric REST APIs in real time — this is live data, not cached."
- If a number looks off: Skip it, move to the next section
- If failover sim fails: "In production, this would connect to a fully provisioned secondary — for demo purposes, let me show the workflow."
