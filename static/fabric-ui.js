/* Fabric Resiliency & Recovery — shared UI helpers.
 * Loaded by base.html before page scripts. No dependencies.
 *   FabricUI.icon(type, size)   official Microsoft Fabric item icon (<img>)
 *   FabricUI.esc(value)         HTML-escape for template-literal rendering
 *   FabricUI.toast(msg, kind)   non-blocking, screen-reader-announced notice
 *   FabricUI.fetchJSON(url, o)  fetch that throws on non-2xx with the server message
 */
(function () {
    'use strict';

    var ICON_BASE = '/static/img/fabric/';

    // Fabric REST item "type" -> official icon file (@fabric-msft/svg-icons v6.1.0, MIT)
    var ITEM_ICONS = {
        Lakehouse: 'lakehouse',
        Warehouse: 'data_warehouse',
        SQLEndpoint: 'data_warehouse',
        WarehouseSnapshot: 'data_warehouse',
        SQLDatabase: 'sql_database',
        MirroredDatabase: 'mirrored_generic_database',
        MirroredWarehouse: 'mirrored_generic_database',
        MirroredAzureDatabricksCatalog: 'mirrored_generic_database',
        MirroredCatalog: 'mirrored_generic_database',
        MirroredOneLake: 'mirrored_generic_database',
        CosmosDBDatabase: 'sql_database',
        SnowflakeDatabase: 'mirrored_generic_database',
        Notebook: 'notebook',
        SparkJobDefinition: 'spark_job_direction',
        Environment: 'environment',
        DataPipeline: 'pipeline',
        Pipeline: 'pipeline',
        CopyJob: 'copy_job',
        Dataflow: 'dataflow_gen2',
        DataflowGen2: 'dataflow_gen2',
        DataflowsGen2: 'dataflow_gen2',
        MountedDataFactory: 'data_factory',
        ApacheAirflowJob: 'data_factory',
        Report: 'report',
        PaginatedReport: 'paginated_report',
        SemanticModel: 'semantic_model',
        Dashboard: 'dashboard',
        Datamart: 'datamart',
        Scorecard: 'scorecard',
        Eventhouse: 'event_house',
        KQLDatabase: 'kql_database',
        KQLQueryset: 'kql_queryset',
        KQLDashboard: 'real_time_dashboard',
        Eventstream: 'eventstream',
        MLModel: 'model',
        MLExperiment: 'experiments',
        DataAgent: 'data_agent',
        OperationsAgent: 'operations_agent',
        UserDataFunction: 'user_data_function',
        VariableLibrary: 'variable_library',
        GraphModel: 'graph_model_instance',
        GraphQuerySet: 'graph_model_instance_queryset',
        Ontology: 'schema_model',
        OrgApp: 'apps',
        Workspace: 'workspace'
    };

    var PRODUCT_ICONS = {
        fabric: 'product_fabric', onelake: 'product_one_lake', powerbi: 'product_power_bi',
        datafactory: 'product_data_factory', dataengineering: 'product_data_engineering',
        datawarehouse: 'product_data_warehouse', rti: 'product_real_time_intelligence',
        datascience: 'product_data_science', purview: 'product_purview', databases: 'product_databases'
    };

    function esc(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function iconUrl(type) {
        return ICON_BASE + (ITEM_ICONS[type] || 'generic_placeholder') + '.svg';
    }

    // size: 'sm' (18) | default (24) | 'lg' (32) | 'xl' (40). label='' marks the icon decorative.
    function icon(type, size, label) {
        var cls = 'fx-icon' + (size ? ' ' + size : '');
        var alt = label === undefined ? '' : label;
        return '<img class="' + cls + '" src="' + iconUrl(type) + '" alt="' + esc(alt) + '"' +
            (alt ? '' : ' aria-hidden="true"') + ' loading="lazy" decoding="async">';
    }

    function productIcon(key, size) {
        return '<img class="fx-icon' + (size ? ' ' + size : '') + '" src="' + ICON_BASE +
            (PRODUCT_ICONS[key] || 'product_fabric') + '.svg" alt="" aria-hidden="true">';
    }

    // ---------------------------------------------------------------- toasts
    var toastHost = null;
    function toast(message, kind, timeoutMs) {
        if (!toastHost) {
            toastHost = document.createElement('div');
            toastHost.className = 'fx-toasts';
            toastHost.setAttribute('role', 'status');
            toastHost.setAttribute('aria-live', 'polite');
            document.body.appendChild(toastHost);
        }
        var k = kind || inferKind(message);
        var iconCls = { ok: 'fa-circle-check', warn: 'fa-triangle-exclamation', bad: 'fa-circle-xmark' }[k] || 'fa-circle-info';
        var el = document.createElement('div');
        el.className = 'fx-toast ' + k;
        if (k === 'bad') el.setAttribute('role', 'alert');
        el.innerHTML = '<i class="fas ' + iconCls + '" aria-hidden="true"></i><div class="fx-toast-msg"></div>' +
            '<button type="button" aria-label="Dismiss notification"><i class="fas fa-xmark" aria-hidden="true"></i></button>';
        el.querySelector('.fx-toast-msg').textContent = String(message);
        var close = function () {
            if (!el.parentNode) return;
            el.classList.add('leaving');
            setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 260);
        };
        el.querySelector('button').addEventListener('click', close);
        toastHost.appendChild(el);
        var ms = timeoutMs || (k === 'bad' ? 9000 : 5000);
        setTimeout(close, ms);
        return el;
    }
    function inferKind(message) {
        var m = String(message || '').toLowerCase();
        if (/error|fail|denied|unable|cannot|invalid/.test(m)) return 'bad';
        if (/warn|partial|skipp|not configured|required/.test(m)) return 'warn';
        if (/success|complete|done|saved|synced|replicated/.test(m)) return 'ok';
        return 'info';
    }
    // Non-blocking replacement for the ~120 alert() calls across pages.
    // confirm() stays native: destructive actions must keep blocking confirmation.
    window.alert = function (message) { toast(message); };

    // ----------------------------------------------------------- checked fetch
    // Turn raw API errors (often embedded JSON) into one actionable sentence.
    function friendlyError(raw, status) {
        var m = String(raw || '');
        var code = (m.match(/"errorCode"\s*:\s*"([^"]+)"/) || [])[1] || '';
        var api = (m.match(/(Fabric|Power BI) API (\d{3})/) || []);
        var st = status || +(api[2] || 0);
        if (/TokenExpired|token has expired/i.test(m) || code === 'TokenExpired')
            return 'Your sign-in expired. Refresh the page or sign in again.';
        if (st === 401 || /Not authenticated/i.test(m)) return 'Not signed in (401). Sign in again.';
        if (st === 403 || /InsufficientPrivileges|Forbidden/i.test(m))
            return 'Access denied (403): this account lacks the permission this check needs.' + (code ? ' [' + code + ']' : '');
        if (st === 429 || /TooManyRequests|throttl/i.test(m)) return 'Fabric is throttling requests (429). Wait a minute and retry.';
        if (/timed out|ConnectTimeout|ReadTimeout|aborted/i.test(m)) return 'The Fabric API did not respond in time. Retry; if it persists, check network or Fabric status.';
        if (/Could not list items in workspace/i.test(m)) return 'Could not list the workspace items, so this check could not run. ' + (code ? '[' + code + ']' : 'Retry shortly.');
        var plain = m.replace(/\{[\s\S]*\}/g, '').replace(/\s+/g, ' ').trim();
        return (plain || ('Request failed' + (st ? ' (' + st + ')' : ''))).slice(0, 220) + (code ? ' [' + code + ']' : '');
    }

    async function fetchJSON(url, options) {
        var resp;
        try { resp = await fetch(url, options); }
        catch (e) { var ne = new Error(friendlyError(e && e.message)); ne.raw = e && e.message; throw ne; }
        var data = null;
        try { data = await resp.json(); } catch (_) { data = null; }
        if (!resp.ok) {
            var raw = (data && (data.error || data.message)) || (resp.status + ' ' + resp.statusText);
            if (resp.status === 401 || /TokenExpired|token has expired/i.test(String(raw))) {
                document.dispatchEvent(new CustomEvent('bcdr:session-expired'));
            }
            var err = new Error(friendlyError(raw, resp.status));
            err.status = resp.status;
            err.raw = raw;
            err.data = data;
            throw err;
        }
        return data;
    }

    // ------------------------------------------------------------------ motion
    var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var REVEAL_SELECTOR = '.card, .metric-card, .type-card, .alert, table.table, [class*="-card"]:not(.fx-revealed), .section-label';

    function revealWithin(root) {
        if (reduceMotion || !root || !root.querySelectorAll) return;
        var nodes = [];
        if (root.matches && root.matches(REVEAL_SELECTOR)) nodes.push(root);
        root.querySelectorAll(REVEAL_SELECTOR).forEach(function (n) { nodes.push(n); });
        var i = 0;
        nodes.forEach(function (n) {
            if (n.classList.contains('fx-revealed') || n.closest('.modal-content, .sync-dropdown')) return;
            n.classList.add('fx-revealed', 'fx-reveal');
            n.style.setProperty('--fx-i', Math.min(i++, 12));
            n.addEventListener('animationend', function done() {
                n.classList.remove('fx-reveal');
                n.style.removeProperty('--fx-i');
                n.removeEventListener('animationend', done);
            });
        });
    }

    // Count-up for numeric metric text set by page code. Cancels itself if the
    // page writes new text mid-animation, so the page's value always wins.
    var COUNT_SELECTOR = '.metric-value, .type-percentage';
    var written = new WeakMap();
    function countUp(el) {
        if (reduceMotion || el.dataset.fxCounting) return;
        var text = el.textContent.trim();
        var m = text.match(/^([^\d-]*)(-?[\d,]*\.?\d+)(.*)$/);
        if (!m) return;
        var target = parseFloat(m[2].replace(/,/g, ''));
        if (!isFinite(target) || target === 0 || Math.abs(target) > 1e7) return;
        var decimals = (m[2].split('.')[1] || '').length;
        var hasComma = m[2].indexOf(',') !== -1;
        var start = performance.now(), dur = 700;
        el.dataset.fxCounting = '1';
        function fmt(v) {
            var s = v.toFixed(decimals);
            return hasComma ? Number(s).toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) : s;
        }
        function frame(now) {
            if (written.has(el) && el.textContent !== written.get(el)) { delete el.dataset.fxCounting; return; }
            var p = Math.min(1, (now - start) / dur);
            var eased = 1 - Math.pow(1 - p, 4);
            var out = m[1] + fmt(target * eased) + m[3];
            if (p >= 1) out = text;
            el.textContent = out;
            written.set(el, out);
            if (p < 1) requestAnimationFrame(frame); else delete el.dataset.fxCounting;
        }
        requestAnimationFrame(frame);
    }

    function observe() {
        var content = document.querySelector('.content') || document.body;
        revealWithin(content);
        content.querySelectorAll(COUNT_SELECTOR).forEach(countUp);
        if (reduceMotion || !window.MutationObserver) return;
        new MutationObserver(function (records) {
            records.forEach(function (r) {
                r.addedNodes.forEach(function (n) {
                    if (n.nodeType !== 1) return;
                    revealWithin(n);
                    if (n.matches && n.matches(COUNT_SELECTOR)) countUp(n);
                    if (n.querySelectorAll) n.querySelectorAll(COUNT_SELECTOR).forEach(countUp);
                });
                if (r.type === 'characterData' || r.type === 'childList') {
                    var t = r.target.nodeType === 1 ? r.target : r.target.parentElement;
                    if (t && t.matches && t.matches(COUNT_SELECTOR) && written.get(t) !== t.textContent) countUp(t);
                }
            });
        }).observe(content, { childList: true, subtree: true, characterData: true });
    }

    // ------------------------------------------------------------ mobile nav
    function initNav() {
        var toggle = document.querySelector('.nav-toggle');
        var backdrop = document.querySelector('.sidebar-backdrop');
        if (!toggle) return;
        var setOpen = function (open) {
            document.body.classList.toggle('nav-open', open);
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) { var first = document.querySelector('.sidebar .nav-link'); if (first) first.focus(); }
        };
        toggle.addEventListener('click', function () { setOpen(!document.body.classList.contains('nav-open')); });
        if (backdrop) backdrop.addEventListener('click', function () { setOpen(false); });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && document.body.classList.contains('nav-open')) { setOpen(false); toggle.focus(); }
        });
    }

    // Escape closes any open page modal (pages use .modal.show or .modal-overlay.active)
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        var open = document.querySelector('.modal.show, .modal-overlay.active');
        if (!open) return;
        open.classList.remove('show', 'active');
        if (open.classList.contains('modal-overlay') || open.classList.contains('modal')) open.style.display = '';
    });

    // Freshness stamp in the footer: shows when the page last loaded data.
    function stampFreshness() {
        var el = document.getElementById('update-time');
        if (!el) return;
        var set = function () {
            el.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            el.setAttribute('datetime', new Date().toISOString());
        };
        set();
        var origFetch = window.fetch;
        window.fetch = function () {
            return origFetch.apply(this, arguments).then(function (r) { if (r.ok) set(); return r; });
        };
    }

    // Failover readiness: one rule set shared by Managed Failover and the Command Center.
    // Each check: {id, label, level: ok|warn|bad|unknown, detail, href}. Verdict: NO-GO if any bad.
    async function readiness(opts) {
        opts = opts || {};
        var q = opts.refresh ? '?refresh=1' : '';
        var get = function (u, ms) {
            var ctl = new AbortController();
            var t = setTimeout(function () { ctl.abort(); }, ms || 90000);
            return fetchJSON(u, { signal: ctl.signal }).finally(function () { clearTimeout(t); });
        };
        var r = await Promise.allSettled([
            get('/api/active-pair-info'), get('/api/bcdr/data-freshness' + q), get('/api/bcdr/status'),
            get('/api/bcdr/model-readiness' + q), get('/api/bcdr/schedules' + q, 180000), get('/api/security/item-permissions' + q),
            get('/api/lineage/connections?max_inspect=20', 120000), get('/api/gateways'),
        ]);
        var v = function (i) { return r[i].status === 'fulfilled' ? r[i].value : null; };
        var why = function (i) { return r[i].status === 'rejected' ? (r[i].reason && r[i].reason.message) || 'unavailable' : ''; };
        var hrs = function (h) { return h == null ? '—' : h < 1 ? Math.round(h * 60) + ' min' : h < 48 ? h.toFixed(1) + ' h' : (h / 24).toFixed(1) + ' days'; };
        var checks = [];
        var add = function (id, label, level, detail, href) { checks.push({ id: id, label: label, level: level, detail: detail, href: href }); };

        var pair = v(0);
        if (pair) {
            var pr = pair.primary_region, sr = pair.secondary_region;
            add('region', 'Secondary in a different region', pr && sr ? (pr === sr ? 'bad' : 'ok') : 'unknown',
                pr && sr ? (pr === sr ? 'Both workspaces are in ' + pr + '; a regional outage takes out both.' : pr + ' → ' + sr) : 'Region not reported.', '/architecture');
            var cs = (pair.secondary_capacity_state || '').toLowerCase();
            add('capacity', 'Secondary capacity running', !cs ? 'unknown' : cs === 'active' ? 'ok' : 'warn',
                cs ? (pair.secondary_capacity || 'Secondary capacity') + ' is ' + pair.secondary_capacity_state + (cs === 'active' ? '' : '; resume it before failover.') : 'Capacity state not reported.', '/architecture');
        } else add('region', 'Secondary in a different region', 'unknown', why(0), '/architecture');

        var fr = v(1);
        if (fr) {
            var s = fr.summary || {}, h = s.measured_rpo_hours, st = s.states || {};
            var lvl = h == null ? 'unknown' : st.diverged ? 'bad' : h <= 1 ? 'ok' : h <= 24 ? 'warn' : 'bad';
            add('rpo', 'Data current in secondary (measured RPO)', lvl,
                (s.rpo_is_lower_bound ? '≥ ' : '') + hrs(h) + ' behind · ' + (st.behind || 0) + ' of ' + s.tables_compared + ' tables behind' +
                (st.diverged ? ' · ' + st.diverged + ' diverged' : '') + '. An unplanned failover loses up to this much data.', '/lakehouse');
        } else add('rpo', 'Data current in secondary (measured RPO)', 'unknown', why(1), '/lakehouse');

        var bs = v(2);
        if (bs) {
            var missing = [];
            (bs.type_cards || []).forEach(function (c) {
                if (!c.supported) return;
                var sn = {}; (c.secondary_items || []).forEach(function (i) { sn[i.name] = 1; });
                (c.primary_items || []).forEach(function (i) { if (!sn[i.name]) missing.push(i.name + ' (' + c.type + ')'); });
            });
            add('items', 'Every item exists in secondary', missing.length ? 'warn' : 'ok',
                missing.length ? missing.length + ' missing: ' + missing.slice(0, 4).join(', ') + (missing.length > 4 ? '…' : '') : bs.total_mirrored + '/' + bs.total_primary + ' items replicated.', '/inventory');
        } else add('items', 'Every item exists in secondary', 'unknown', why(2), '/inventory');

        var mr = v(3);
        if (mr) {
            var nr = (mr.models || []).filter(function (m) { return !m.ready; });
            add('models', 'Semantic models queryable in secondary', nr.length ? 'bad' : 'ok',
                nr.length ? nr.length + ' not refreshed/framed: ' + nr.map(function (m) { return m.name; }).slice(0, 3).join(', ') + '. Reports would show no data.' : (mr.models || []).length + ' model(s) ready.', '/architecture');
        } else add('models', 'Semantic models queryable in secondary', 'unknown', why(3), '/architecture');

        var sc = v(4);
        if (sc) {
            var c = sc.counts || {}, todo = (c.create_paused || 0) + (c.missing_item_in_secondary || 0), on = (sc.enabled_on_secondary || []).length;
            add('schedules', 'Job schedules replicated (paused) to secondary', sc.errors && sc.errors.length ? 'warn' : todo ? 'warn' : on ? 'warn' : 'ok',
                sc.primary_schedules === 0 ? 'Primary has no job schedules — nothing to activate on failover. Is the pipeline scheduled at all?'
                    : todo ? todo + ' of ' + sc.primary_schedules + ' schedule(s) not on secondary yet; run a sync.'
                    : on ? on + ' schedule(s) already enabled on the standby: ' + sc.enabled_on_secondary.slice(0, 3).join(', ')
                    : sc.primary_schedules + ' schedule(s) ready, ' + sc.primary_enabled + ' will be enabled on failover.', '/lakehouse');
        } else add('schedules', 'Job schedules replicated (paused) to secondary', 'unknown', why(4), '/lakehouse');

        var ip = v(5);
        if (ip) {
            var ic = ip.counts || {}, cov = ip.coverage || {};
            var open = (ic.grant_dataset_user || 0) + (ic.upgrade_dataset_user || 0) + (ic.manual_portal_share || 0);
            add('perms', 'Item permissions replicated', open ? 'warn' : cov.not_scanned ? 'warn' : 'ok',
                open ? open + ' share(s) still to replicate.' : cov.not_scanned ? cov.not_scanned + ' item(s) not scanned (needs Fabric admin or the opt-in API).' : 'All scanned shares in place.', '/security');
        } else add('perms', 'Item permissions replicated', 'unknown', why(5), '/security');

        var ln = v(6);
        if (ln) {
            var stale = (ln.summary || {}).stale || 0;
            add('refs', 'Secondary items point at secondary (no primary references)', stale ? 'bad' : 'ok',
                stale ? stale + ' reference(s) still point at the primary.' : ((ln.summary || {}).total_connections || 0) + ' references checked, all remapped.', '/topology');
        } else add('refs', 'Secondary items point at secondary (no primary references)', 'unknown', why(6), '/topology');

        var gw = v(7);
        if (gw) {
            var bad = (gw.gateways || []).filter(function (g) { return g.dr && g.dr.level !== 'ok'; });
            add('gateways', 'Gateways available after failover', bad.some(function (g) { return g.dr.level === 'bad'; }) ? 'bad' : bad.length ? 'warn' : 'ok',
                bad.length ? bad.map(function (g) { return g.name + ': ' + g.dr.text; }).slice(0, 2).join(' ') : (gw.gateways || []).length + ' gateway(s) OK.', '/gateways');
        } else add('gateways', 'Gateways available after failover', 'unknown', why(7), '/gateways');

        var n = function (l) { return checks.filter(function (c) { return c.level === l; }).length; };
        // Unknown on a critical check means we can't vouch for the failover, so never call it GO.
        var critical = ['region', 'rpo', 'items', 'models', 'refs'];
        var blind = checks.filter(function (c) { return c.level === 'unknown' && critical.indexOf(c.id) >= 0; }).length;
        var verdict = n('bad') ? 'NO-GO' : blind ? "CAN'T ASSESS" : n('warn') || n('unknown') ? 'GO WITH RISK' : 'GO';
        return { checks: checks, verdict: verdict, counts: { ok: n('ok'), warn: n('warn'), bad: n('bad'), unknown: n('unknown') },
                 expected_data_loss_hours: fr ? (fr.summary || {}).measured_rpo_hours : null, measured_at: new Date().toISOString() };
    }

    function renderReadiness(el, res, opts) {
        opts = opts || {};
        var ic = { ok: 'fa-circle-check', warn: 'fa-triangle-exclamation', bad: 'fa-circle-xmark', unknown: 'fa-circle-question' };
        var list = opts.onlyIssues ? res.checks.filter(function (c) { return c.level !== 'ok'; }) : res.checks;
        el.innerHTML = list.map(function (c) {
            return '<li class="rd-check rd-' + c.level + '"><i class="fas ' + ic[c.level] + '" aria-hidden="true"></i>' +
                '<div><div class="rd-check-label">' + esc(c.label) + '</div><div class="rd-check-detail">' + esc(c.detail) + '</div></div>' +
                (c.href ? '<a class="rd-check-link" href="' + esc(c.href) + '">Open <i class="fas fa-arrow-right" aria-hidden="true"></i></a>' : '') + '</li>';
        }).join('') || '<li class="rd-check rd-ok"><i class="fas fa-circle-check" aria-hidden="true"></i><div><div class="rd-check-label">Nothing to fix</div></div></li>';
    }

    window.FabricUI = {
        icon: icon, iconUrl: iconUrl, productIcon: productIcon, esc: esc,
        toast: toast, fetchJSON: fetchJSON, friendlyError: friendlyError, reveal: revealWithin, ITEM_ICONS: ITEM_ICONS,
        readiness: readiness, renderReadiness: renderReadiness
    };
    window.escapeHtml = esc;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { initNav(); observe(); stampFreshness(); });
    } else { initNav(); observe(); stampFreshness(); }
})();
