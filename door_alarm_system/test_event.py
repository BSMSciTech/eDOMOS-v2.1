import sys
import os
sys.path.append(os.path.dirname(__file__))

from app import app, log_event
import threading

# Test the event logging system
with app.app_context():
    print("🧪 Testing event logging...")
    log_event('test_door_open', 'Manual test: Door opened from test script')
    print("✅ Test event logged successfully!")
