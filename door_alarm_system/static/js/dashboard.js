// static/js/dashboard.js
document.addEventListener('DOMContentLoaded', function() {
    // Tab navigation
    const permissions = JSON.parse(document.getElementById('permissions-data').textContent);
    
    if (permissions.includes('controls')) {
        document.getElementById('controls-tab')?.addEventListener('click', function(e) {
            e.preventDefault();
            new bootstrap.Modal(document.getElementById('controlsModal')).show();
        });
    }
    
    if (permissions.includes('event_log')) {
        document.getElementById('event-log-tab')?.addEventListener('click', function(e) {
            e.preventDefault();
            window.location.href = '/event-log';
        });
    }
    
    if (permissions.includes('report')) {
        document.getElementById('report-tab')?.addEventListener('click', function(e) {
            e.preventDefault();
            new bootstrap.Modal(document.getElementById('reportModal')).show();
        });
    }
    
    if (permissions.includes('analytics')) {
        document.getElementById('analytics-tab')?.addEventListener('click', function(e) {
            e.preventDefault();
            window.location.href = '/analytics';
        });
    }
    
    document.getElementById('view-all-logs')?.addEventListener('click', function() {
        window.location.href = '/event-log';
    });
    
    // Save settings
    document.getElementById('save-settings')?.addEventListener('click', function() {
        const duration = document.getElementById('timer-duration').value;
        fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ timer_duration: parseInt(duration) })
        })
        .then(response => response.json())
        .then(data => {
            if(data.success) {
                alert('Settings saved successfully!');
                location.reload();
            } else {
                alert('Error saving settings: ' + data.error);
            }
        });
    });
    
    // Backup database
    document.getElementById('backup-db')?.addEventListener('click', function() {
        window.location.href = '/api/backup';
    });
    
    // Load recent events
    loadRecentEvents();
});

function loadRecentEvents() {
    fetch('/api/events?per_page=5')
    .then(response => response.json())
    .then(data => {
        const container = document.getElementById('recent-events');
        if (!container) return;
        container.innerHTML = '';
        data.events.forEach(event => {
            const badgeClass = event.event_type === 'alarm_triggered' ? 'bg-danger' : 
                              event.event_type === 'door_open' ? 'bg-warning' : 'bg-success';
            container.innerHTML += `
                <div class="list-group-item d-flex justify-content-between align-items-start">
                    <div class="ms-2 me-auto">
                        <div class="fw-bold">${event.event_type.replace('_', ' ').toUpperCase()}</div>
                        ${event.description}
                    </div>
                    <span class="badge ${badgeClass} rounded-pill">${event.timestamp}</span>
                </div>
            `;
        });
    });
}