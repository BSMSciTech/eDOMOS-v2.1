// WebSocket connection for real-time events
function renderRecentEvents(events) {
    const container = document.getElementById('recent-events');
    if (!container) return;
    container.innerHTML = '';
    events.forEach(event => {
        const badgeClass = event.event_type === 'alarm_triggered' ? 'text-danger fw-bold' : 
                          event.event_type === 'door_open' ? 'text-warning fw-bold' : 'text-success fw-bold';
        const row = document.createElement('tr');
        row.innerHTML = `
            <td><span class="small">${event.timestamp}</span></td>
            <td class="${badgeClass}">${event.event_type.replace('_', ' ').toUpperCase()}</td>
            <td>${event.description}</td>
        `;
        container.appendChild(row);
    });
}

// Global socket variable and state
let socket = null;
let isSocketReady = false;
let eventQueue = [];

document.addEventListener('DOMContentLoaded', function() {
    console.log('🚀 DOM loaded, initializing WebSocket...');
    // Initialize WebSocket connection for all pages
    if (typeof io !== 'undefined') {
        console.log('Initializing WebSocket connection...');
        socket = io('/events', {
            transports: ['websocket', 'polling'],
            reconnection: true,
            reconnectionDelay: 1000,
            reconnectionAttempts: 5,
            timeout: 20000
        });
        
        socket.on('connect', function() {
            console.log('✅ Connected to WebSocket server');
            console.log('Socket ID:', socket.id);
            console.log('Socket namespace:', socket.nsp);
            isSocketReady = true;
            
            // Process any queued events
            if (eventQueue.length > 0) {
                console.log('Processing', eventQueue.length, 'queued events');
                eventQueue.forEach(event => processEvent(event));
                eventQueue = [];
            }
            
            // Show connection status if element exists
            const statusElement = document.getElementById('connection-status');
            if (statusElement) {
                statusElement.innerHTML = '<span class="badge bg-success">Connected</span>';
            }
        });
        
        socket.on('new_event', function(data) {
            console.log('📡 Received new event:', data);
            console.log('Event timestamp:', new Date().toISOString());
            console.log('Socket connected:', socket.connected);
            
            if (isSocketReady) {
                processEvent(data);
            } else {
                console.log('⏳ Socket not ready, queuing event');
                eventQueue.push(data);
            }
        });
        
        socket.on('disconnect', function() {
            console.log('❌ Disconnected from WebSocket server');
            isSocketReady = false;
            const statusElement = document.getElementById('connection-status');
            if (statusElement) {
                statusElement.innerHTML = '<span class="badge bg-danger">Disconnected</span>';
            }
        });
        
        socket.on('connect_error', function(error) {
            console.error('❌ WebSocket connection error:', error);
            const statusElement = document.getElementById('connection-status');
            if (statusElement) {
                statusElement.innerHTML = '<span class="badge bg-warning">Connection Error</span>';
            }
        });
        
        socket.on('reconnect', function(attemptNumber) {
            console.log('🔄 Reconnected to WebSocket server after', attemptNumber, 'attempts');
        });
        
        socket.on('reconnect_error', function(error) {
            console.error('🔄 Reconnection failed:', error);
        });
    } else {
        console.error('❌ Socket.IO library not loaded');
    }
    
    // Initial load for recent events (if element exists)
    if (document.getElementById('recent-events')) {
        fetch('/api/events?per_page=5')
        .then(response => response.json())
        .then(data => {
            renderRecentEvents(data.events);
        })
        .catch(err => console.error('Error loading initial events:', err));
    }
    
    // Debug: Log which elements are found on page load
    console.log('Page elements found:', {
        recentEvents: !!document.getElementById('recent-events'),
        eventFeed: !!document.querySelector('.event-feed'),
        eventTableBody: !!document.getElementById('eventTableBody'),
        doorStatus: !!document.getElementById('door-status'),
        alarmStatus: !!document.getElementById('alarm-status'),
        timerSet: !!document.getElementById('timer-set'),
        totalEvents: !!document.getElementById('total-events'),
        doorOpenEvents: !!document.getElementById('door-open-events'),
        doorCloseEvents: !!document.getElementById('door-close-events'),
        alarmEvents: !!document.getElementById('alarm-events')
    });
});

// Centralized event processing function
function processEvent(data) {
    console.log('🔄 Processing event:', data.event?.event_type);
    
    try {
        // Update event feed if present (dashboard)  
        if (document.querySelector('.event-feed')) {
            console.log('Updating dashboard event feed...');
            if (typeof addEventToFeed === 'function') {
                addEventToFeed(data.event);
            } else {
                console.warn('addEventToFeed function not available');
            }
        }
        
        // Update recent events table if present (other pages)
        if (document.getElementById('recent-events')) {
            console.log('Updating recent events table...');
            fetch('/api/events?per_page=5')
            .then(response => response.json())
            .then(data2 => {
                renderRecentEvents(data2.events);
            })
            .catch(err => console.error('Error fetching events:', err));
        }
        
        // Update event log table if present (event log page)
        if (document.getElementById('eventTableBody')) {
            console.log('Updating event log table...');
            updateEventLogTable(data.event);
        }
        
        // Update dashboard stats if present
        if (data.statistics || data.door_status || data.alarm_status) {
            console.log('Updating dashboard statistics...');
            updateDashboardStats(data);
        }
        
        // Show notification for new events
        if (data.event) {
            showEventNotification(data.event);
        }
        
        console.log('✅ Event processed successfully');
        
    } catch (error) {
        console.error('❌ Error processing event:', error);
    }
}

// Function to update event log table in real-time
function updateEventLogTable(event) {
    const tbody = document.getElementById('eventTableBody');
    if (!tbody) return;
    
    // Create badge based on event type
    let badge = '';
    if (event.event_type === 'door_open') {
        badge = '<span class="badge bg-info">Door Open</span>';
    } else if (event.event_type === 'door_close') {
        badge = '<span class="badge bg-success">Door Close</span>';
    } else if (event.event_type === 'alarm_triggered') {
        badge = '<span class="badge bg-danger">Alarm Triggered</span>';
    } else {
        badge = `<span class="badge bg-secondary">${event.event_type}</span>`;
    }
    
    // Create new row
    const newRow = document.createElement('tr');
    newRow.className = 'table-success';
    newRow.innerHTML = `
        <td>${event.id}</td>
        <td>${badge}</td>
        <td>${event.description}</td>
        <td>${event.timestamp}</td>
    `;
    
    // Prepend to table
    tbody.insertAdjacentElement('afterbegin', newRow);
    
    // Remove highlight after 3 seconds
    setTimeout(() => {
        newRow.classList.remove('table-success');
    }, 3000);
    
    // Remove excess rows (keep only first 50)
    const rows = tbody.querySelectorAll('tr');
    if (rows.length > 50) {
        rows[rows.length - 1].remove();
    }
}

function updateDashboardStats(data) {
    console.log('🔄 updateDashboardStats called with:', data);
    
    // Debug: Check which elements exist
    const elementCheck = {
        'door-status': !!document.getElementById('door-status'),
        'alarm-status': !!document.getElementById('alarm-status'), 
        'timer-set': !!document.getElementById('timer-set'),
        'total-events': !!document.getElementById('total-events'),
        'door-open-events': !!document.getElementById('door-open-events'),
        'door-close-events': !!document.getElementById('door-close-events'),
        'alarm-events': !!document.getElementById('alarm-events'),
        'status-indicator': !!document.querySelector('.status-indicator')
    };
    console.log('📊 Available elements:', elementCheck);
    
    // Direct element updates using specific IDs from dashboard.html
    // Door Status
    if (data.door_status) {
        const doorStatusEl = document.getElementById('door-status');
        if (doorStatusEl) {
            doorStatusEl.textContent = data.door_status;
            console.log('✅ Updated door status:', data.door_status);
        } else {
            console.warn('❌ door-status element not found');
        }
    }
    
    // Alarm Status  
    if (data.alarm_status) {
        const alarmStatusEl = document.getElementById('alarm-status');
        if (alarmStatusEl) {
            alarmStatusEl.textContent = data.alarm_status;
            console.log('✅ Updated alarm status:', data.alarm_status);
        } else {
            console.warn('❌ alarm-status element not found');
        }
    }
    
    // Timer Set
    if (data.timer_set) {
        const timerSetEl = document.getElementById('timer-set');
        if (timerSetEl) {
            timerSetEl.textContent = data.timer_set + 's';
            console.log('✅ Updated timer set:', data.timer_set + 's');
        } else {
            console.warn('❌ timer-set element not found');
        }
    }
    
    // Update status indicator
    const statusIndicator = document.querySelector('.status-indicator');
    if (statusIndicator && data.door_status) {
        if (data.door_status === 'Closed') {
            statusIndicator.className = 'status-indicator secure';
            statusIndicator.innerHTML = '<i class="fas fa-lock"></i> DOOR SECURE';
        } else {
            statusIndicator.className = 'status-indicator alert';
            statusIndicator.innerHTML = '<i class="fas fa-unlock"></i> DOOR OPEN';
        }
        console.log('✅ Updated status indicator for:', data.door_status);
    } else if (!statusIndicator) {
        console.warn('❌ status-indicator element not found');
    }
    
    // Update event statistics from WebSocket payload
    if (data.statistics) {
        // Dashboard statistics - using correct IDs from dashboard.html
        const totalEvents = document.getElementById('total-events');
        const doorOpenEvents = document.getElementById('door-open-events');
        const doorCloseEvents = document.getElementById('door-close-events');
        const alarmEvents = document.getElementById('alarm-events');
        
        // Update statistics with animation
        if (totalEvents) {
            animateCounterUpdate(totalEvents, data.statistics.total_events);
        }
        if (doorOpenEvents) {
            animateCounterUpdate(doorOpenEvents, data.statistics.door_open_events);
        }
        if (doorCloseEvents) {
            animateCounterUpdate(doorCloseEvents, data.statistics.door_close_events);
        }
        if (alarmEvents) {
            animateCounterUpdate(alarmEvents, data.statistics.alarm_events);
        }
        
        console.log('Statistics updated:', data.statistics);
    }
}

// Function to animate counter updates
function animateCounterUpdate(element, newValue) {
    const currentValue = parseInt(element.textContent) || 0;
    if (currentValue !== newValue) {
        element.style.transition = 'all 0.3s ease';
        element.style.color = '#3b82f6';
        element.style.transform = 'scale(1.1)';
        
        setTimeout(() => {
            element.textContent = newValue;
            setTimeout(() => {
                element.style.color = '';
                element.style.transform = 'scale(1)';
            }, 150);
        }, 150);
    }
}

// Function to show event notifications
function showEventNotification(event) {
    // Create a toast notification for new events
    if (typeof bootstrap !== 'undefined' && bootstrap.Toast) {
        const toastContainer = document.getElementById('toast-container') || createToastContainer();
        
        const eventColor = event.event_type === 'alarm_triggered' ? 'bg-danger' :
                          event.event_type === 'door_open' ? 'bg-warning' : 'bg-success';
        
        const toastHtml = `
            <div class="toast align-items-center text-white ${eventColor} border-0" role="alert" aria-live="assertive" aria-atomic="true">
                <div class="d-flex">
                    <div class="toast-body">
                        <strong>${event.event_type.replace('_', ' ').toUpperCase()}</strong><br>
                        ${event.description}
                    </div>
                    <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
                </div>
            </div>
        `;
        
        toastContainer.insertAdjacentHTML('beforeend', toastHtml);
        const toastElement = toastContainer.lastElementChild;
        const toast = new bootstrap.Toast(toastElement, { delay: 5000 });
        toast.show();
        
        // Remove toast after it's hidden
        toastElement.addEventListener('hidden.bs.toast', () => {
            toastElement.remove();
        });
    } else {
        // Fallback notification
        console.log('📢 New Event:', event.event_type, '-', event.description);
    }
}

// Function to create toast container if it doesn't exist
function createToastContainer() {
    const container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container position-fixed top-0 end-0 p-3';
    container.style.zIndex = '9999';
    document.body.appendChild(container);
    return container;
}

// Function to test WebSocket connection
function testWebSocketConnection() {
    if (socket && socket.connected) {
        console.log('✅ WebSocket is connected');
        return true;
    } else {
        console.log('❌ WebSocket is not connected');
        return false;
    }
}

// Make functions available globally for debugging
window.testWebSocketConnection = testWebSocketConnection;
window.showEventNotification = showEventNotification;