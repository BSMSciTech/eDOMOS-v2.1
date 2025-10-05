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

// Function to update WebSocket connection status
function updateWebSocketStatus(connected) {
    const statusElement = document.getElementById('live-status');
    if (statusElement) {
        if (connected) {
            statusElement.innerHTML = '<i class="fas fa-wifi"></i> Real-time';
            statusElement.className = 'badge bg-success';
            statusElement.title = 'Connected - Events update automatically';
        } else {
            statusElement.innerHTML = '<i class="fas fa-exclamation-triangle"></i> Reconnecting...';
            statusElement.className = 'badge bg-warning';
            statusElement.title = 'Attempting to reconnect...';
        }
    }
    
    // Update system health indicator
    const healthStatus = document.getElementById('systemHealth');
    if (healthStatus) {
        const healthDot = healthStatus.querySelector('.health-dot');
        const healthText = healthStatus.querySelector('.health-text');
        
        if (connected) {
            if (healthDot) healthDot.className = 'health-dot online';
            if (healthText) healthText.textContent = 'Live Monitoring Active';
        } else {
            if (healthDot) healthDot.className = 'health-dot warning';
            if (healthText) healthText.textContent = 'Reconnecting...';
        }
    }
}

// Global socket variable and state
let socket = null;
let isSocketReady = false;
let eventQueue = [];
let lastEventIds = new Set(); // Track processed event IDs to prevent duplicates
let lastEventTimes = new Map(); // Track event timestamps for time-based duplicate prevention

// Polling mechanism variables
let pollingInterval = null;
let isPollingActive = false;
let pollingRate = 3000; // 3 seconds default
let lastEventId = 0; // Track last processed event ID for polling
let lastStatisticsUpdate = 0;
let pollingFailureCount = 0;
let maxPollingFailures = 3;

// WebSocket connection testing functions (callable from browser console)
window.testWebSocketConnection = function() {
    console.log('🔍 WEBSOCKET CONNECTION TEST:');
    console.log('=====================================');
    
    if (!socket) {
        console.log('❌ Socket object: NULL - Not initialized');
        return false;
    }
    
    console.log('✅ Socket object: EXISTS');
    console.log('  ├─ Socket ID:', socket.id || 'NOT_ASSIGNED');
    console.log('  ├─ Connected:', socket.connected);
    console.log('  ├─ Disconnected:', socket.disconnected);
    console.log('  ├─ Namespace:', socket.nsp);
    console.log('  ├─ Ready State:', isSocketReady);
    console.log('  ├─ Transport:', socket.io?.engine?.transport?.name || 'UNKNOWN');
    console.log('  ├─ Engine State:', socket.io?.engine?.readyState || 'UNKNOWN');
    console.log('  └─ URL:', socket.io?.uri || 'UNKNOWN');
    
    if (socket.connected) {
        console.log('🏓 Sending test ping...');
        socket.emit('ping', {
            test: 'Manual connection test',
            timestamp: new Date().toISOString()
        });
        return true;
    } else {
        console.log('❌ Socket not connected - attempting reconnection...');
        socket.connect();
        return false;
    }
};

window.getWebSocketStats = function() {
    console.log('📊 REAL-TIME UPDATE STATISTICS:');
    console.log('==================================');
    console.log('WebSocket Ready:', isSocketReady);
    console.log('Polling Active:', isPollingActive);
    console.log('Event Queue Length:', eventQueue.length);
    console.log('Processed Event IDs:', lastEventIds.size);
    console.log('Event Time Cache:', lastEventTimes.size);
    console.log('Last Event ID (Polling):', lastEventId);
    console.log('Polling Failure Count:', pollingFailureCount);
    console.log('Polling Rate:', pollingRate + 'ms');
    return {
        socketReady: isSocketReady,
        pollingActive: isPollingActive,
        queueLength: eventQueue.length,
        processedEvents: lastEventIds.size,
        timeCache: lastEventTimes.size,
        lastEventId: lastEventId,
        pollingFailures: pollingFailureCount,
        pollingRate: pollingRate
    };
};

// Console functions for testing and control
window.forcePolling = function() {
    console.log('🔧 FORCE POLLING MODE:');
    if (socket && socket.connected) {
        socket.disconnect();
    }
    isSocketReady = false;
    if (!isPollingActive) {
        startPolling();
    }
    console.log('✅ Forced into polling mode');
};

window.forceWebSocket = function() {
    console.log('🔧 FORCE WEBSOCKET MODE:');
    if (isPollingActive) {
        stopPolling();
    }
    if (!socket || !socket.connected) {
        // Reinitialize WebSocket
        if (typeof io !== 'undefined') {
            socket = io('/events', {
                transports: ['websocket', 'polling'],
                reconnection: true,
                forceNew: true
            });
        }
    }
    console.log('✅ Attempting to force WebSocket mode');
};

window.setPollingRate = function(milliseconds) {
    if (milliseconds < 1000) {
        console.log('⚠️ Minimum polling rate is 1000ms');
        return;
    }
    
    pollingRate = milliseconds;
    console.log('🔧 Set polling rate to:', pollingRate + 'ms');
    
    if (isPollingActive) {
        console.log('🔄 Restarting polling with new rate');
        stopPolling();
        setTimeout(startPolling, 500);
    }
};

window.testPolling = function() {
    console.log('🧪 TESTING POLLING MECHANISM:');
    console.log('==============================');
    pollForEvents();
    pollForStatistics();
};

window.simulateEvent = function() {
    console.log('🎭 SIMULATING WEBSOCKET EVENT FOR TESTING:');
    console.log('==========================================');
    
    const testEvent = {
        event: {
            id: 9999,
            event_type: 'test_event',
            description: 'Manual test event from browser console',
            timestamp: new Date().toISOString()
        },
        door_status: 'Test Status',
        alarm_status: 'Test Alarm',
        statistics: {
            total_events: 100,
            door_open_events: 30,
            door_close_events: 30,
            alarm_events: 5
        },
        source: 'manual_test'
    };
    
    console.log('📤 Simulating event:', testEvent);
    processEvent(testEvent);
    
    // Also test the new_event handler directly
    if (socket && socket.connected) {
        socket.emit('test_event', testEvent);
    }
    
    return testEvent;
};

window.debugPageElements = function() {
    console.log('🔍 DEBUGGING PAGE ELEMENTS:');
    console.log('============================');
    
    const elements = {
        'door-status': document.getElementById('door-status'),
        'alarm-status': document.getElementById('alarm-status'),
        'timer-set': document.getElementById('timer-set'),
        'total-events': document.getElementById('total-events'),
        'door-open-events': document.getElementById('door-open-events'),
        'door-close-events': document.getElementById('door-close-events'),
        'alarm-events': document.getElementById('alarm-events'),
        'recent-events': document.getElementById('recent-events'),
        'eventTableBody': document.getElementById('eventTableBody'),
        'event-feed': document.querySelector('.event-feed'),
        'live-status': document.getElementById('live-status')
    };
    
    Object.entries(elements).forEach(([name, element]) => {
        console.log(`${name}:`, element ? '✅ FOUND' : '❌ NOT FOUND', element);
    });
    
    return elements;
};

// Polling functions for real-time updates
function startPolling() {
    if (isPollingActive) {
        console.log('⚠️ Polling already active');
        return;
    }
    
    console.log('🔄 STARTING POLLING MECHANISM:');
    console.log('  ├─ Rate:', pollingRate + 'ms');
    console.log('  ├─ Last Event ID:', lastEventId);
    console.log('  └─ Fallback for WebSocket');
    
    isPollingActive = true;
    pollingFailureCount = 0;
    
    pollingInterval = setInterval(() => {
        pollForEvents();
        pollForStatistics();
    }, pollingRate);
    
    updateConnectionStatus();
}

function stopPolling() {
    if (!isPollingActive) {
        console.log('⚠️ Polling not active');
        return;
    }
    
    console.log('🛑 STOPPING POLLING MECHANISM');
    
    if (pollingInterval) {
        clearInterval(pollingInterval);
        pollingInterval = null;
    }
    
    isPollingActive = false;
    pollingFailureCount = 0;
    
    updateConnectionStatus();
}

function pollForEvents() {
    fetch(`/api/events?since=${lastEventId}&per_page=10`)
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        return response.json();
    })
    .then(data => {
        pollingFailureCount = 0; // Reset failure count on success
        
        if (data.events && data.events.length > 0) {
            console.log(`📊 POLLING: Received ${data.events.length} new events`);
            
            data.events.forEach(event => {
                // Update last event ID
                if (event.id > lastEventId) {
                    lastEventId = event.id;
                }
                
                // Process each new event
                const eventData = {
                    event: event,
                    source: 'polling'
                };
                
                processEvent(eventData);
            });
            
            // Show polling success indicator
            updateConnectionStatus();
        }
    })
    .catch(error => {
        pollingFailureCount++;
        console.error(`❌ POLLING ERROR (${pollingFailureCount}/${maxPollingFailures}):`, error);
        
        if (pollingFailureCount >= maxPollingFailures) {
            console.error('🚨 Max polling failures reached, stopping polling');
            stopPolling();
            updateConnectionStatus();
        }
    });
}

function pollForStatistics() {
    // Only poll statistics every 10 seconds to reduce server load
    const now = Date.now();
    if (now - lastStatisticsUpdate < 10000) {
        return;
    }
    
    fetch('/api/statistics')
    .then(response => response.json())
    .then(data => {
        lastStatisticsUpdate = now;
        updateDashboardStats({
            statistics: data,
            source: 'polling'
        });
    })
    .catch(error => {
        console.error('❌ Statistics polling error:', error);
    });
}

function updateConnectionStatus() {
    const statusElement = document.getElementById('live-status');
    const healthStatus = document.getElementById('systemHealth');
    
    if (isSocketReady) {
        // WebSocket is primary
        if (statusElement) {
            statusElement.innerHTML = '<i class="fas fa-wifi"></i> WebSocket';
            statusElement.className = 'badge bg-success';
            statusElement.title = 'WebSocket connected - Real-time updates active';
        }
        
        if (healthStatus) {
            const healthDot = healthStatus.querySelector('.health-dot');
            const healthText = healthStatus.querySelector('.health-text');
            if (healthDot) healthDot.className = 'health-dot online';
            if (healthText) healthText.textContent = 'WebSocket Active';
        }
    } else if (isPollingActive) {
        // Polling is fallback
        if (statusElement) {
            statusElement.innerHTML = '<i class="fas fa-sync-alt"></i> Polling';
            statusElement.className = 'badge bg-info';
            statusElement.title = 'Polling active - Events update every ' + (pollingRate/1000) + 's';
        }
        
        if (healthStatus) {
            const healthDot = healthStatus.querySelector('.health-dot');
            const healthText = healthStatus.querySelector('.health-text');
            if (healthDot) healthDot.className = 'health-dot warning';
            if (healthText) healthText.textContent = 'Polling Active';
        }
    } else {
        // Neither WebSocket nor polling
        if (statusElement) {
            statusElement.innerHTML = '<i class="fas fa-exclamation-triangle"></i> Offline';
            statusElement.className = 'badge bg-danger';
            statusElement.title = 'No real-time connection - Manual refresh required';
        }
        
        if (healthStatus) {
            const healthDot = healthStatus.querySelector('.health-dot');
            const healthText = healthStatus.querySelector('.health-text');
            if (healthDot) healthDot.className = 'health-dot offline';
            if (healthText) healthText.textContent = 'Connection Lost';
        }
    }
}

document.addEventListener('DOMContentLoaded', function() {
    console.log('🚀 DOM loaded, initializing WebSocket...');
    
    // Prevent multiple connections - singleton pattern
    if (socket && socket.connected) {
        console.log('✅ WebSocket already connected, reusing existing connection');
        return;
    }
    
    // Disconnect any existing socket first
    if (socket) {
        console.log('🔌 Disconnecting existing socket before creating new one');
        socket.disconnect();
    }
    
    // Initialize WebSocket connection for all pages
    if (typeof io !== 'undefined') {
        console.log('Initializing WebSocket connection...');
        socket = io('/events', {
            transports: ['websocket', 'polling'],
            reconnection: true,
            reconnectionDelay: 1000,
            reconnectionAttempts: 10,
            timeout: 20000,
            forceNew: true // Force new connection to prevent duplicates
        });
        
        socket.on('connect', function() {
            console.log('🔌 WEBSOCKET CONNECTION ESTABLISHED:');
            console.log('  ├─ Status: CONNECTED');
            console.log('  ├─ Socket ID:', socket.id);
            console.log('  ├─ Namespace:', socket.nsp);
            console.log('  ├─ Connected:', socket.connected);
            console.log('  ├─ Transport:', socket.io.engine.transport.name);
            console.log('  ├─ Timestamp:', new Date().toISOString());
            console.log('  └─ Ready State: Setting to TRUE');
            
            isSocketReady = true;
            
            // Stop polling since WebSocket is now active
            if (isPollingActive) {
                console.log('🛑 Stopping polling - WebSocket connection established');
                stopPolling();
            }
            
            // Update connection status
            updateConnectionStatus();
            
            // Send client ready signal to server
            console.log('📡 Sending client ready signal to server...');
            socket.emit('client_ready', {
                timestamp: new Date().toISOString(),
                page: window.location.pathname,
                userAgent: navigator.userAgent.substring(0, 50)
            });
            
            // Send test ping to verify bidirectional communication
            console.log('🏓 Sending test ping to server...');
            socket.emit('ping', {
                timestamp: new Date().toISOString(),
                test: 'Client connection verification'
            });
            
            // Show connection notification
            showNotification('WebSocket connected - Real-time monitoring active', 'success', 3000);
            
            // Process any queued events
            if (eventQueue.length > 0) {
                console.log('⏳ Processing', eventQueue.length, 'queued events');
                eventQueue.forEach(event => processEvent(event));
                eventQueue = [];
            }
            
            // Show connection status if element exists
            const statusElement = document.getElementById('connection-status');
            if (statusElement) {
                statusElement.innerHTML = '<span class="badge bg-success">WebSocket Connected</span>';
            }
        });
        
        // Enhanced WebSocket event handlers with detailed logging
        socket.on('connection_status', function(data) {
            console.log('📋 CONNECTION STATUS FROM SERVER:');
            console.log('  ├─ Status:', data.status);
            console.log('  ├─ Message:', data.message);
            console.log('  ├─ Server Time:', data.server_time);
            console.log('  ├─ Session ID:', data.session_id);
            console.log('  ├─ Total Clients:', data.total_clients);
            console.log('  └─ Connection Established:', data.connection_established);
            
            if (data.connection_established) {
                console.log('✅ SERVER CONFIRMS: WebSocket handshake successful!');
            }
        });
        
        socket.on('server_ping', function(data) {
            console.log('🏓 SERVER PING RECEIVED:', data);
            console.log('📡 Responding with client pong...');
            socket.emit('pong', {
                timestamp: new Date().toISOString(),
                response_to: data.timestamp
            });
        });
        
        socket.on('pong', function(data) {
            console.log('🏓 PONG FROM SERVER:');
            console.log('  ├─ Timestamp:', data.timestamp);
            console.log('  ├─ Client ID:', data.client_id);
            console.log('  └─ Response:', data.server_response);
            console.log('✅ BIDIRECTIONAL COMMUNICATION VERIFIED!');
        });
        
        socket.on('server_ack', function(data) {
            console.log('📨 SERVER ACKNOWLEDGMENT:');
            console.log('  ├─ Status:', data.status);
            console.log('  ├─ Message:', data.message);
            console.log('  └─ Timestamp:', data.timestamp);
        });
        
        socket.on('new_event', function(data) {
            console.log('🎯 WEBSOCKET EVENT RECEIVED - DETAILED ANALYSIS:');
            console.log('================================================');
            console.log('  ├─ Event Type:', data.event?.event_type || 'UNDEFINED');
            console.log('  ├─ Event ID:', data.event?.id || 'UNDEFINED'); 
            console.log('  ├─ Event Description:', data.event?.description || 'UNDEFINED');
            console.log('  ├─ Full Event Object:', data.event);
            console.log('  ├─ Door Status:', data.door_status || 'UNDEFINED');
            console.log('  ├─ Alarm Status:', data.alarm_status || 'UNDEFINED');
            console.log('  ├─ Statistics:', data.statistics);
            console.log('  ├─ Full Data Keys:', Object.keys(data));
            console.log('  ├─ Socket Ready State:', isSocketReady);
            console.log('  ├─ Socket Connected:', socket.connected);
            console.log('  ├─ Current Page:', window.location.pathname);
            console.log('  └─ Timestamp:', new Date().toISOString());
            
            // Mark data source for processing
            data.source = 'websocket';
            
            // FORCE IMMEDIATE UI UPDATE - TESTING
            console.log('🔥 FORCING IMMEDIATE UI UPDATES FOR TESTING...');
            
            // Test 1: Update any visible status elements immediately
            const doorStatusEl = document.getElementById('door-status');
            const alarmStatusEl = document.getElementById('alarm-status');
            
            if (doorStatusEl && data.door_status) {
                console.log('🔄 DIRECT UPDATE: Door status from', doorStatusEl.textContent, 'to', data.door_status);
                doorStatusEl.textContent = data.door_status;
                doorStatusEl.style.backgroundColor = data.door_status === 'Open' ? '#ffeaa7' : '#74b9ff';
                doorStatusEl.style.color = '#2d3436';
                doorStatusEl.style.fontWeight = 'bold';
            }
            
            if (alarmStatusEl && data.alarm_status) {
                console.log('🔄 DIRECT UPDATE: Alarm status from', alarmStatusEl.textContent, 'to', data.alarm_status);
                alarmStatusEl.textContent = data.alarm_status;
                alarmStatusEl.style.backgroundColor = data.alarm_status === 'Active' ? '#ff7675' : '#00b894';
                alarmStatusEl.style.color = 'white';
                alarmStatusEl.style.fontWeight = 'bold';
            }
            
            // Test 2: Add event to any visible event feed
            const eventFeed = document.querySelector('.event-feed') || document.getElementById('recent-events') || document.getElementById('eventTableBody');
            if (eventFeed && data.event) {
                console.log('🔄 ADDING EVENT TO FEED:', data.event.event_type);
                
                const eventItem = document.createElement('div');
                eventItem.className = 'alert alert-info mb-2';
                eventItem.style.border = '2px solid #0984e3';
                eventItem.innerHTML = `
                    <strong>🚀 LIVE EVENT (WebSocket):</strong><br>
                    <strong>Type:</strong> ${data.event.event_type}<br>
                    <strong>Description:</strong> ${data.event.description}<br>
                    <strong>Time:</strong> ${new Date().toLocaleTimeString()}<br>
                    <small class="text-success">Received via WebSocket in real-time!</small>
                `;
                
                if (eventFeed.tagName === 'TBODY') {
                    // It's a table body
                    const row = document.createElement('tr');
                    row.className = 'table-success';
                    row.innerHTML = `
                        <td>${data.event.id || 'NEW'}</td>
                        <td><span class="badge bg-primary">🚀 LIVE: ${data.event.event_type}</span></td>
                        <td>${data.event.description}</td>
                        <td>${new Date().toLocaleString()}</td>
                    `;
                    eventFeed.insertBefore(row, eventFeed.firstChild);
                } else {
                    // It's a div container
                    eventFeed.insertBefore(eventItem, eventFeed.firstChild);
                }
                
                // Remove old entries
                const children = eventFeed.children;
                while (children.length > 10) {
                    children[children.length - 1].remove();
                }
            } else {
                console.log('❌ No event feed found to update');
            }
            
            // Test 3: Show browser notification
            if (data.event) {
                const notificationTitle = `🚀 ${data.event.event_type.toUpperCase()}`;
                const notificationBody = `${data.event.description} (Real-time via WebSocket)`;
                
                console.log('🔔 SHOWING NOTIFICATION:', notificationTitle);
                
                // Try browser notification
                if ('Notification' in window && Notification.permission === 'granted') {
                    new Notification(notificationTitle, {
                        body: notificationBody,
                        icon: '/favicon.ico'
                    });
                } else if ('Notification' in window && Notification.permission !== 'denied') {
                    Notification.requestPermission().then(permission => {
                        if (permission === 'granted') {
                            new Notification(notificationTitle, {
                                body: notificationBody,
                                icon: '/favicon.ico'
                            });
                        }
                    });
                }
                
                // Show toast notification
                showNotification(notificationBody, 'success', 5000);
            }
            
            // Enhanced event handling with the centralized function
            if (isSocketReady) {
                console.log('✅ Processing event through centralized function...');
                processEvent(data);
            } else {
                console.log('⏳ Socket not ready, queuing WebSocket event');
                eventQueue.push(data);
            }
            
            console.log('🎯 WEBSOCKET EVENT PROCESSING COMPLETE');
            console.log('================================================');
        });
        
        socket.on('disconnect', function(reason) {
            console.log('❌ WEBSOCKET DISCONNECTED:');
            console.log('  ├─ Reason:', reason);
            console.log('  ├─ Socket ID:', socket.id);
            console.log('  ├─ Timestamp:', new Date().toISOString());
            console.log('  └─ Setting ready state to FALSE');
            
            isSocketReady = false;
            
            // Start polling as fallback when WebSocket disconnects
            console.log('🔄 WebSocket disconnected - Starting polling fallback');
            setTimeout(() => {
                if (!isSocketReady) { // Only start polling if WebSocket hasn't reconnected
                    startPolling();
                    showNotification('Switched to polling mode - Events will update every 3 seconds', 'info', 4000);
                }
            }, 2000); // Wait 2 seconds before starting polling to allow reconnection attempts
            
            updateConnectionStatus();
            
            const statusElement = document.getElementById('connection-status');
            if (statusElement) {
                statusElement.innerHTML = '<span class="badge bg-warning">Reconnecting...</span>';
            }
        });
        
        socket.on('connect_error', function(error) {
            console.error('❌ WEBSOCKET CONNECTION ERROR:');
            console.error('  ├─ Error:', error);
            console.error('  ├─ Type:', error.type);
            console.error('  ├─ Description:', error.description);
            console.error('  └─ Timestamp:', new Date().toISOString());
            
            isSocketReady = false;
            
            // Start polling if WebSocket fails to connect
            if (!isPollingActive) {
                console.log('🔄 WebSocket failed - Starting polling fallback');
                setTimeout(startPolling, 1000);
                showNotification('WebSocket failed - Switching to polling mode', 'warning', 4000);
            }
            
            updateConnectionStatus();
            const statusElement = document.getElementById('connection-status');
            if (statusElement) {
                statusElement.innerHTML = '<span class="badge bg-warning">Using Polling</span>';
            }
        });
        
        socket.on('reconnect', function(attemptNumber) {
            console.log('🔄 Reconnected to WebSocket server after', attemptNumber, 'attempts');
            isSocketReady = true;
            
            // Stop polling since WebSocket is back
            if (isPollingActive) {
                console.log('🛑 Stopping polling - WebSocket reconnected');
                stopPolling();
            }
            
            updateConnectionStatus();
            
            // Show reconnection notification
            showNotification('WebSocket reconnected - Real-time monitoring restored!', 'success', 3000);
        });
        
        socket.on('reconnect_error', function(error) {
            console.error('❌ Reconnection failed:', error);
            updateWebSocketStatus(false);
        });
    } else {
        console.error('❌ Socket.IO library not loaded - Starting polling mode');
        // If Socket.IO is not available, start polling immediately
        setTimeout(startPolling, 1000);
        showNotification('WebSocket unavailable - Using polling mode for real-time updates', 'info', 4000);
    }
    
    // Initialize lastEventId from the latest event
    fetch('/api/events?per_page=1')
    .then(response => response.json())
    .then(data => {
        if (data.events && data.events.length > 0) {
            lastEventId = data.events[0].id;
            console.log('📊 Initialized lastEventId:', lastEventId);
        }
    })
    .catch(err => console.error('Error getting initial event ID:', err));
    
    // Initial load for recent events (if element exists)
    if (document.getElementById('recent-events')) {
        fetch('/api/events?per_page=5')
        .then(response => response.json())
        .then(data => {
            renderRecentEvents(data.events);
        })
        .catch(err => console.error('Error loading initial events:', err));
    }
    
    // Start polling as fallback if WebSocket doesn't connect within 5 seconds
    setTimeout(() => {
        if (!isSocketReady && !isPollingActive) {
            console.log('⏰ WebSocket timeout - Starting polling fallback');
            startPolling();
            showNotification('WebSocket timeout - Switched to polling mode', 'warning', 4000);
        }
    }, 5000);
    
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
    const source = data.source || 'unknown';
    const eventType = data.event?.event_type || 'unknown';
    const eventId = data.event?.id || 0;
    
    console.log(`🔄 PROCESSING EVENT (${source.toUpperCase()}):`, eventType);
    console.log(`  ├─ Event ID: ${eventId}`);
    console.log(`  ├─ Source: ${source}`);
    console.log(`  └─ Description: ${data.event?.description || 'N/A'}`);
    
    // Client-side duplicate prevention
    if (data.event && data.event.id) {
        // Check if we've already processed this event ID
        if (lastEventIds.has(data.event.id)) {
            console.log(`🚫 DUPLICATE PREVENTED (${source}): Event ID ${data.event.id} already processed`);
            return;
        }
        
        // Check time-based duplicates
        const eventKey = `${data.event.event_type}_${data.event.description}`;
        const now = Date.now();
        if (lastEventTimes.has(eventKey)) {
            const timeDiff = now - lastEventTimes.get(eventKey);
            if (timeDiff < 3000) { // 3 seconds
                console.log(`🚫 TIME-BASED DUPLICATE PREVENTED (${source}):`, eventKey, 'within', timeDiff, 'ms');
                return;
            }
        }
        
        // Record this event to prevent future duplicates
        lastEventIds.add(data.event.id);
        lastEventTimes.set(eventKey, now);
        
        // Update lastEventId for polling
        if (data.event.id > lastEventId) {
            lastEventId = data.event.id;
            console.log(`📊 Updated lastEventId to ${lastEventId} (${source})`);
        }
        
        // Clean up old entries to prevent memory buildup
        if (lastEventIds.size > 100) {
            const oldestIds = Array.from(lastEventIds).slice(0, 50);
            oldestIds.forEach(id => lastEventIds.delete(id));
        }
        
        console.log(`✅ Event passed duplicate checks: ${data.event.id} (${source})`);
    }
    
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
        
        // Show enhanced notification for new events with source indicator
        if (data.event) {
            showEventNotification(data.event, source);
            console.log('📢 Event notification shown:', data.event.event_type, '(via ' + source + ')');
        }
        
        // Add visual indicator for polling events
        if (source === 'polling' && document.getElementById('event-log')) {
            const pollingIndicator = document.createElement('div');
            pollingIndicator.className = 'polling-event-indicator';
            pollingIndicator.innerHTML = `<small class="text-info">📊 ${new Date().toLocaleTimeString()} - Event received via polling</small>`;
            
            const eventLog = document.getElementById('event-log');
            if (eventLog && eventLog.children.length > 0) {
                eventLog.appendChild(pollingIndicator);
                
                // Remove old indicators
                const indicators = eventLog.querySelectorAll('.polling-event-indicator');
                if (indicators.length > 5) {
                    indicators[0].remove();
                }
            }
        }
        
        console.log('✅ Event processed successfully - Page updated in real-time');
        
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

// Enhanced notification function for events with source indicator
function showEventNotification(event, source = 'unknown') {
    if (!event) return;
    
    let message = '';
    let type = 'info';
    
    // Add source indicator to message
    const sourceIcon = source === 'websocket' ? '📡' : source === 'polling' ? '📊' : '📋';
    const sourceText = source === 'websocket' ? 'WebSocket' : source === 'polling' ? 'Polling' : 'Unknown';
    
    switch (event.event_type) {
        case 'door_open':
            message = `🚪 Door Opened - Monitoring timer started (via ${sourceText} ${sourceIcon})`;
            type = 'warning';
            break;
        case 'door_close':
            message = `🔒 Door Closed - System secured (via ${sourceText} ${sourceIcon})`;
            type = 'success';
            break;
        case 'alarm_triggered':
            message = `🚨 ALARM TRIGGERED - Immediate attention required! (via ${sourceText} ${sourceIcon})`;
            type = 'danger';
            break;
        default:
            message = `📋 ${event.event_type.replace('_', ' ').toUpperCase()} - ${event.description} (via ${sourceText} ${sourceIcon})`;
    }
    
    showNotification(message, type, 5000);
}

// Function to show event notifications
function showNotification(message, type, duration) {
    // Create a toast notification for new events
    if (typeof bootstrap !== 'undefined' && bootstrap.Toast) {
        const toastContainer = document.getElementById('toast-container') || createToastContainer();
        
        const toastHtml = `
            <div class="toast align-items-center text-white bg-${type} border-0" role="alert" aria-live="assertive" aria-atomic="true">
                <div class="d-flex">
                    <div class="toast-body">
                        ${message}
                    </div>
                    <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
                </div>
            </div>
        `;
        
        toastContainer.insertAdjacentHTML('beforeend', toastHtml);
        const toastElement = toastContainer.lastElementChild;
        const toast = new bootstrap.Toast(toastElement, { delay: duration });
        toast.show();
        
        // Remove toast after it's hidden
        toastElement.addEventListener('hidden.bs.toast', () => {
            toastElement.remove();
        });
    } else {
        // Fallback notification
        console.log('📢 Notification:', message);
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