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
    async function fetchJSON(url, options) {
        var resp = await fetch(url, options);
        var data = null;
        try { data = await resp.json(); } catch (_) { data = null; }
        if (!resp.ok) {
            var msg = (data && (data.error || data.message)) || (resp.status + ' ' + resp.statusText);
            var err = new Error(msg);
            err.status = resp.status;
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

    window.FabricUI = {
        icon: icon, iconUrl: iconUrl, productIcon: productIcon, esc: esc,
        toast: toast, fetchJSON: fetchJSON, reveal: revealWithin, ITEM_ICONS: ITEM_ICONS
    };
    window.escapeHtml = esc;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { initNav(); observe(); stampFreshness(); });
    } else { initNav(); observe(); stampFreshness(); }
})();
