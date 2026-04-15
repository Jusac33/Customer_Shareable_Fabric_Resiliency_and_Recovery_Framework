// Fabric Resiliency & Recovery Dashboard - Global JavaScript Functions

// API Base URL
const API_BASE = '/api';

// Chart.js Chart instances (global storage)
const charts = {};

// Initialize dashboard on page load
document.addEventListener('DOMContentLoaded', function() {
    initializePageElements();
    initializeCharts();
    startAutoRefresh();
    startClockUpdate();
    setActiveNavItem();
});

/**
 * Initialize common page elements and event listeners
 */
function initializePageElements() {
    // Set up navigation active states
    const currentPath = window.location.pathname;
    document.querySelectorAll('.nav-link').forEach(link => {
        link.classList.remove('active');
        if (link.getAttribute('href') === currentPath) {
            link.classList.add('active');
        }
    });

    // Set up button click handlers
    document.querySelectorAll('[data-action]').forEach(element => {
        element.addEventListener('click', function() {
            const action = this.getAttribute('data-action');
            handleAction(action);
        });
    });

    // Set up form submissions
    document.querySelectorAll('form[data-api-endpoint]').forEach(form => {
        form.addEventListener('submit', function(e) {
            e.preventDefault();
            handleFormSubmit(this);
        });
    });
}

/**
 * Set the active navigation item based on current URL
 */
function setActiveNavItem() {
    const currentPath = window.location.pathname;
    document.querySelectorAll('.nav-link').forEach(link => {
        link.classList.remove('active');
    });
    
    const activeLink = document.querySelector(`a[href="${currentPath}"]`);
    if (activeLink) {
        activeLink.classList.add('active');
    }
}

/**
 * Initialize Chart.js charts on the page
 */
function initializeCharts() {
    // Find all chart containers
    document.querySelectorAll('[data-chart-type]').forEach(element => {
        const chartType = element.getAttribute('data-chart-type');
        const chartId = element.id;
        
        if (!chartId) return;
        
        const ctx = element.getContext('2d');
        const chartConfig = buildChartConfig(chartType, element);
        
        if (chartConfig) {
            charts[chartId] = new Chart(ctx, chartConfig);
        }
    });
}

/**
 * Build Chart.js configuration based on chart type
 */
function buildChartConfig(type, element) {
    const dataset = element.getAttribute('data-dataset');
    
    const baseConfig = {
        options: {
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
                legend: {
                    position: 'top',
                    labels: {
                        font: { size: 12 },
                        padding: 15,
                        usePointStyle: true
                    }
                }
            },
            scales: {
                y: {
                    beginAtZero: true,
                    ticks: { font: { size: 11 } },
                    grid: { color: 'rgba(0,0,0,0.05)' }
                },
                x: {
                    ticks: { font: { size: 11 } },
                    grid: { display: false }
                }
            }
        }
    };

    if (type === 'line') {
        return {
            type: 'line',
            data: {
                labels: ['12:00 AM', '4:00 AM', '8:00 AM', '12:00 PM', '4:00 PM', '8:00 PM'],
                datasets: [{
                    label: 'Capacity (%)',
                    data: [25, 28, 32, 38, 45, 52],
                    borderColor: '#2196F3',
                    backgroundColor: 'rgba(33, 150, 243, 0.1)',
                    borderWidth: 2,
                    tension: 0.4,
                    fill: true
                }]
            },
            ...baseConfig
        };
    } else if (type === 'bar') {
        return {
            type: 'bar',
            data: {
                labels: ['Primary', 'Secondary'],
                datasets: [{
                    label: 'Utilization (%)',
                    data: [65, 42],
                    backgroundColor: ['#2196F3', '#4CAF50'],
                    borderRadius: 6
                }]
            },
            ...baseConfig
        };
    } else if (type === 'doughnut') {
        return {
            type: 'doughnut',
            data: {
                labels: ['In-Sync', 'Missing', 'Mismatched'],
                datasets: [{
                    data: [18, 0, 0],
                    backgroundColor: ['#4CAF50', '#ff9800', '#f44336'],
                    borderColor: 'white',
                    borderWidth: 2
                }]
            },
            options: {
                ...baseConfig.options,
                plugins: {
                    ...baseConfig.options.plugins,
                    legend: {
                        ...baseConfig.options.plugins.legend,
                        position: 'bottom'
                    }
                }
            }
        };
    }
    
    return null;
}

/**
 * Update chart data
 */
function updateChart(chartId, newData) {
    if (!charts[chartId]) return;
    
    const chart = charts[chartId];
    if (newData.labels) chart.data.labels = newData.labels;
    if (newData.datasets) chart.data.datasets = newData.datasets;
    
    chart.update();
}

/**
 * Start auto-refresh of dashboard data every 30 seconds
 */
function startAutoRefresh() {
    setInterval(function() {
        refreshDashboardData();
    }, 30000); // 30 seconds
}

/**
 * Refresh all dashboard data from API
 */
function refreshDashboardData() {
    // Fetch topology data
    fetch(`${API_BASE}/topology`)
        .then(response => response.json())
        .then(data => {
            updateTopologyDisplay(data);
        })
        .catch(error => console.error('Error fetching topology:', error));

    // Fetch inventory data
    fetch(`${API_BASE}/inventory`)
        .then(response => response.json())
        .then(data => {
            updateInventoryDisplay(data);
        })
        .catch(error => console.error('Error fetching inventory:', error));

    // Fetch logs
    fetch(`${API_BASE}/logs`)
        .then(response => response.json())
        .then(data => {
            updateEventFeed(data);
        })
        .catch(error => console.error('Error fetching logs:', error));

    // Fetch sync plan
    fetch(`${API_BASE}/sync-plan`)
        .then(response => response.json())
        .then(data => {
            updateSyncProgress(data);
        })
        .catch(error => console.error('Error fetching sync plan:', error));
}

/**
 * Update topology display with fresh data
 */
function updateTopologyDisplay(data) {
    const topologyElement = document.getElementById('topology-data');
    if (!topologyElement) return;

    data.regions.forEach(region => {
        const regionEl = document.querySelector(`[data-region="${region.id}"]`);
        if (regionEl) {
            regionEl.querySelector('[data-health]').textContent = region.health + '%';
            regionEl.querySelector('[data-capacity]').textContent = region.capacity + '%';
            regionEl.querySelector('[data-heartbeat]').textContent = formatTimeAgo(region.lastHeartbeat);
        }
    });
}

/**
 * Update inventory display
 */
function updateInventoryDisplay(data) {
    const inventoryElement = document.getElementById('inventory-data');
    if (!inventoryElement) return;

    let html = '';
    Object.entries(data).forEach(([workspace, items]) => {
        html += `<div class="inventory-workspace">
            <h4>${workspace}</h4>
            <div class="items">`;
        
        items.forEach(item => {
            html += `<div class="item-badge">${item.type}: ${item.name} <span class="live">LIVE</span></div>`;
        });
        
        html += `</div></div>`;
    });

    inventoryElement.innerHTML = html;
}

/**
 * Update event feed with latest logs
 */
function updateEventFeed(logs) {
    const feedElement = document.getElementById('event-feed');
    if (!feedElement) return;

    let html = '';
    logs.slice(0, 10).forEach(log => {
        const severityClass = log.severity.toLowerCase();
        html += `<div class="event-item event-${severityClass}">
            <div class="event-time">${formatTime(log.timestamp)}</div>
            <div class="event-badge ${severityClass}">${log.severity}</div>
            <div class="event-content">
                <div class="event-title">${log.title}</div>
                <div class="event-message">${log.message}</div>
            </div>
        </div>`;
    });

    feedElement.innerHTML = html;
}

/**
 * Update sync progress display
 */
function updateSyncProgress(syncPlan) {
    const progressElement = document.getElementById('sync-progress');
    if (!progressElement) return;

    const total = syncPlan.inSync + syncPlan.missing + syncPlan.mismatched;
    const percentage = total > 0 ? (syncPlan.inSync / total) * 100 : 0;

    progressElement.style.width = percentage + '%';
    progressElement.textContent = Math.round(percentage) + '%';

    // Update stats
    const statsElement = document.getElementById('sync-stats');
    if (statsElement) {
        statsElement.innerHTML = `
            In-Sync: ${syncPlan.inSync} | 
            Missing: ${syncPlan.missing} | 
            Mismatched: ${syncPlan.mismatched}
        `;
    }
}

/**
 * Start updating the clock every second
 */
function startClockUpdate() {
    function updateClock() {
        const timeElement = document.querySelector('.current-time');
        if (timeElement) {
            const now = new Date();
            const hours = String(now.getHours()).padStart(2, '0');
            const minutes = String(now.getMinutes()).padStart(2, '0');
            const seconds = String(now.getSeconds()).padStart(2, '0');
            const ampm = now.getHours() >= 12 ? 'PM' : 'AM';
            
            timeElement.textContent = `${hours}:${minutes}:${seconds} ${ampm}`;
        }
    }

    updateClock();
    setInterval(updateClock, 1000);
}

/**
 * Handle generic action buttons
 */
function handleAction(action) {
    switch(action) {
        case 'pause-primary':
            pauseWorkspace('primary');
            break;
        case 'pause-secondary':
            pauseWorkspace('secondary');
            break;
        case 'refresh-data':
            refreshDashboardData();
            showNotification('Data refreshed', 'success');
            break;
        case 'run-failover':
            confirmAction('Run Failover?', () => runFailover());
            break;
        default:
            console.warn('Unknown action:', action);
    }
}

/**
 * Pause a workspace
 */
function pauseWorkspace(workspace) {
    const endpoint = workspace === 'primary' ? 'primary_ws' : 'secondary_ws';
    
    fetch(`${API_BASE}/workspace/${endpoint}/pause`, {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        showNotification(`${workspace} workspace paused`, 'success');
        refreshDashboardData();
    })
    .catch(error => {
        showNotification('Error pausing workspace', 'error');
        console.error(error);
    });
}

/**
 * Run failover operation
 */
function runFailover() {
    fetch(`${API_BASE}/failover`, {
        method: 'POST'
    })
    .then(response => response.json())
    .then(data => {
        showNotification('Failover initiated', 'success');
        refreshDashboardData();
    })
    .catch(error => {
        showNotification('Error initiating failover', 'error');
        console.error(error);
    });
}

/**
 * Handle form submissions
 */
function handleFormSubmit(form) {
    const endpoint = form.getAttribute('data-api-endpoint');
    const formData = new FormData(form);
    const data = Object.fromEntries(formData);

    fetch(`${API_BASE}${endpoint}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            showNotification(data.message || 'Operation completed', 'success');
            form.reset();
            refreshDashboardData();
        } else {
            showNotification(data.message || 'Operation failed', 'error');
        }
    })
    .catch(error => {
        showNotification('Error submitting form', 'error');
        console.error(error);
    });
}

/**
 * Show notification message
 */
function showNotification(message, type = 'info') {
    const notification = document.createElement('div');
    notification.className = `alert alert-${type}`;
    notification.innerHTML = `
        <i class="fas fa-${getIconForType(type)}"></i>
        ${message}
    `;

    const container = document.querySelector('main') || document.body;
    container.insertBefore(notification, container.firstChild);

    setTimeout(() => {
        notification.remove();
    }, 5000);
}

/**
 * Get icon for notification type
 */
function getIconForType(type) {
    const icons = {
        success: 'check-circle',
        error: 'exclamation-circle',
        warning: 'exclamation-triangle',
        info: 'info-circle'
    };
    return icons[type] || 'info-circle';
}

/**
 * Show confirmation dialog
 */
function confirmAction(message, callback) {
    if (confirm(message)) {
        callback();
    }
}

/**
 * Format timestamp to readable time
 */
function formatTime(timestamp) {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    return date.toLocaleTimeString('en-US', { 
        hour: '2-digit', 
        minute: '2-digit',
        hour12: true 
    });
}

/**
 * Format timestamp to relative time (e.g., "5 minutes ago")
 */
function formatTimeAgo(timestamp) {
    if (!timestamp) return 'N/A';
    
    const now = new Date();
    const date = new Date(timestamp);
    const seconds = Math.floor((now - date) / 1000);

    if (seconds < 60) return 'just now';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ago';
    return Math.floor(seconds / 86400) + 'd ago';
}

/**
 * Format bytes to human-readable format
 */
function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
}

/**
 * Format percentage with color indicator
 */
function formatPercentage(value, thresholds = { warning: 75, critical: 90 }) {
    let color = '#4CAF50';
    if (value >= thresholds.critical) color = '#f44336';
    else if (value >= thresholds.warning) color = '#ff9800';
    
    return `<span style="color: ${color}; font-weight: 600;">${value}%</span>`;
}

/**
 * Debounce function for reducing API calls
 */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Log message to console with timestamp
 */
function logDebug(message, data = null) {
    const timestamp = new Date().toLocaleTimeString();
    console.log(`[${timestamp}] ${message}`, data || '');
}

// Export functions for external use
window.dashboardUtils = {
    updateChart,
    refreshDashboardData,
    showNotification,
    confirmAction,
    runFailover,
    pauseWorkspace,
    formatBytes,
    formatPercentage,
    formatTime,
    formatTimeAgo
};


