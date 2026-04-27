(function() {
    'use strict';

    const container = document.getElementById('timeline-container');
    const statusBadge = document.getElementById('timeline-status');
    const placeholder = document.getElementById('timeline-placeholder');

    if (!container || !statusBadge) {
        console.warn('Timeline elements not found');
        return;
    }

    const wsProtocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${wsProtocol}://${window.location.host}/ws/timeline`;
    let ws = null;
    let reconnectTimeout = null;

    function connect() {
        if (ws && ws.readyState === WebSocket.OPEN) return;

        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            statusBadge.textContent = 'Connected';
            statusBadge.className = 'badge bg-success';
            if (placeholder) placeholder.style.display = 'none';
            if (reconnectTimeout) {
                clearTimeout(reconnectTimeout);
                reconnectTimeout = null;
            }
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                addTimelineItem(data);
            } catch (e) {
                console.error('Failed to parse timeline message:', e);
            }
        };

        ws.onclose = () => {
            statusBadge.textContent = 'Disconnected';
            statusBadge.className = 'badge bg-danger';
            if (placeholder) placeholder.style.display = '';
            // Reconnect after 3 seconds
            reconnectTimeout = setTimeout(connect, 3000);
        };

        ws.onerror = (err) => {
            console.error('WebSocket error:', err);
            ws.close();
        };
    }

    function addTimelineItem(item) {
        if (!item.task_id || !item.state) return;

        // Construct timeline entry
        const entry = document.createElement('div');
        entry.className = 'timeline-item mb-2';
        entry.innerHTML = `
            <div class="d-flex align-items-center">
                <span class="badge bg-secondary me-2">${item.task_id}</span>
                <span class="badge bg-${stateColor(item.state)} me-2">${item.state}</span>
                <small class="text-muted">${new Date(item.timestamp).toLocaleTimeString()}</small>
            </div>
        `;
        container.prepend(entry);

        // Limit displayed items to 50
        while (container.children.length > 50) {
            container.removeChild(container.lastChild);
        }

        // Remove placeholder if present
        if (placeholder) placeholder.style.display = 'none';
    }

    function stateColor(state) {
        const colors = {
            'created': 'info',
            'enriched': 'primary',
            'dispatched': 'warning',
            'in_progress': 'secondary',
            'review': 'dark',
            'done': 'success',
            'failed': 'danger'
        };
        return colors[state] || 'light';
    }

    // Initial connect
    connect();
})();