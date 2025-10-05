import os
import time
import threading
import sqlite3
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file, flash, render_template_string
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_socketio import SocketIO, emit
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, IntegerField, SelectMultipleField
from wtforms.validators import DataRequired, Email, Length
import RPi.GPIO as GPIO
from models import db, User, Setting, EventLog, EmailConfig
from config import Config

# Initialize Flask app
app = Flask(__name__)
app.config.from_object(Config)
db_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'instance', 'alarm_system.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

# Initialize extensions
db.init_app(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*")

# WebSocket event handlers
@socketio.on('connect', namespace='/events')
def handle_connect():
    """Handle client connection to WebSocket"""
    print(f"[WEBSOCKET] Client connected: {request.sid}")
    emit('connection_status', {
        'status': 'connected',
        'message': 'Real-time monitoring active',
        'server_time': datetime.now().isoformat()
    })

@socketio.on('disconnect', namespace='/events')
def handle_disconnect():
    """Handle client disconnection from WebSocket"""
    print(f"[WEBSOCKET] Client disconnected: {request.sid}")

@socketio.on('ping', namespace='/events')
def handle_ping(data):
    """Handle ping from client for connection testing"""
    print(f"[WEBSOCKET] Ping received from {request.sid}")
    emit('pong', {'timestamp': datetime.now().isoformat()})

# Enhanced broadcast function for events
def broadcast_event(event_data, namespace='/events'):
    """Enhanced broadcast function with better error handling"""
    try:
        print(f"[WEBSOCKET] Broadcasting event to all clients: {event_data.get('event', {}).get('event_type', 'unknown')}")
        socketio.emit('new_event', event_data, namespace=namespace)
        print(f"[WEBSOCKET] Event broadcast successful")
    except Exception as e:
        print(f"[WEBSOCKET ERROR] Failed to broadcast event: {e}")

# GPIO setup - only if not in testing mode
if not os.environ.get('TESTING'):
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(11, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # Magnetic sensor
    GPIO.setup(22, GPIO.OUT)  # Green LED
    GPIO.setup(13, GPIO.OUT)  # Red LED
    GPIO.setup(16, GPIO.OUT)  # White LED
    GPIO.setup(18, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # Switch

# Global variables for door state
door_open = False
alarm_active = False
timer_thread = None
timer_active = False
timer_duration = 30  # Default 30 seconds

# Duplicate prevention variables
last_logged_door_state = None
last_logged_alarm_state = False
last_event_timestamps = {}
event_lock = threading.Lock()

# Initialize system
def init_system():
    os.makedirs('instance', exist_ok=True)
    with app.app_context():
        db.create_all()
        
        # Create default admin if not exists
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', is_admin=True)
            admin.set_password('admin')  # Change in production!
            db.session.add(admin)
            db.session.commit()
        
        # Set default timer duration
        if not Setting.query.filter_by(key='timer_duration').first():
            setting = Setting(key='timer_duration', value='30')
            db.session.add(setting)
            db.session.commit()
        
        # Initialize LEDs
        if not os.environ.get('TESTING'):
            GPIO.output(22, GPIO.HIGH)  # Green LED on
            GPIO.output(13, GPIO.LOW)   # Red LED off
            GPIO.output(16, GPIO.LOW)   # White LED off

# Door monitoring thread
def monitor_door():
    global door_open, alarm_active, timer_active, timer_duration, timer_thread
    while True:
        if os.environ.get('TESTING'):
            time.sleep(1)  # Don't monitor in testing mode
            continue
            
        # For NO sensor: HIGH means door open, LOW means door closed
        door_is_open = GPIO.input(11) == GPIO.HIGH  # NO mode

        if door_is_open and not door_open:
            # Door just opened
            door_open = True
            alarm_active = False
            timer_active = True
            if not os.environ.get('TESTING'):
                GPIO.output(16, GPIO.LOW)  # Ensure white LED is off
            with app.app_context():
                timer_setting = Setting.query.filter_by(key='timer_duration').first()
                timer_duration = int(timer_setting.value) if timer_setting else 30
            print(f"[DEBUG] Door opened. Timer set to {timer_duration} seconds.")
            log_event('door_open', 'Door opened')
            if timer_thread and timer_thread.is_alive():
                timer_thread.join()
            timer_thread = threading.Thread(target=alarm_timer, args=(timer_duration,))
            timer_thread.start()
        elif not door_is_open and door_open:
            # Door just closed
            door_open = False
            alarm_active = False
            timer_active = False
            # Reset alarm state for duplicate prevention
            global last_logged_alarm_state
            last_logged_alarm_state = False
            if not os.environ.get('TESTING'):
                GPIO.output(13, GPIO.LOW)
                GPIO.output(16, GPIO.LOW)
            print("[DEBUG] Door closed. Timer and alarm deactivated.")
            log_event('door_close', 'Door closed')
        time.sleep(0.1)

def alarm_timer(duration):
    global timer_active, alarm_active
    start_time = time.time()
    print(f"[DEBUG] Alarm timer started for {duration} seconds.")
    
    # Blink red LED while door is open and timer is running
    while timer_active and (time.time() - start_time) < duration:
        if not timer_active or not door_open:
            break
        if not os.environ.get('TESTING'):
            GPIO.output(13, GPIO.HIGH)
        time.sleep(0.5)
        if not timer_active or not door_open:
            break
        if not os.environ.get('TESTING'):
            GPIO.output(13, GPIO.LOW)
        time.sleep(0.5)
    
    # Check if we should trigger alarm
    if timer_active and door_open:
        # Timer elapsed and door is still open - trigger alarm
        timer_active = False
        alarm_active = True
        if not os.environ.get('TESTING'):
            GPIO.output(13, GPIO.LOW)  # Turn off red LED completely
            GPIO.output(16, GPIO.HIGH)  # Turn on white LED
        print(f"[DEBUG] Alarm triggered after {duration} seconds.")
        log_event('alarm_triggered', f'Alarm triggered after {duration} seconds')
        send_alarm_email(duration)
    else:
        # Door was closed before timer elapsed, do not trigger alarm
        timer_active = False
        alarm_active = False
        if not os.environ.get('TESTING'):
            GPIO.output(13, GPIO.LOW)
            GPIO.output(16, GPIO.LOW)
        print("[DEBUG] Alarm timer cancelled (door closed before timer elapsed).")

def log_event(event_type, description):
    from pytz import timezone
    global door_open, alarm_active, last_logged_door_state, last_logged_alarm_state, last_event_timestamps
    
    # Enhanced duplicate prevention logic
    with event_lock:
        current_time = time.time()
        event_key = f"{event_type}_{description}"
        
        print(f"[DEBUG] log_event called: {event_type} - {description}")
        
        # Time-based duplicate prevention (within 2 seconds for better detection)
        if event_key in last_event_timestamps:
            time_diff = current_time - last_event_timestamps[event_key]
            if time_diff < 2.0:  # Increased from 0.1 to 2 seconds
                print(f"[DEBUG] Duplicate event prevented: {event_type} (time_diff: {time_diff:.3f}s)")
                return
        
        # State-based duplicate prevention with better logic
        if event_type == 'door_open':
            if last_logged_door_state is True:
                print(f"[DEBUG] Duplicate door_open prevented (already logged as open)")
                return
            last_logged_door_state = True
            print(f"[DEBUG] Setting door state to OPEN")
        elif event_type == 'door_close':
            if last_logged_door_state is False:
                print(f"[DEBUG] Duplicate door_close prevented (already logged as closed)")
                return
            last_logged_door_state = False
            print(f"[DEBUG] Setting door state to CLOSED")
        elif event_type == 'alarm_triggered':
            if last_logged_alarm_state is True:
                print(f"[DEBUG] Duplicate alarm_triggered prevented (already triggered)")
                return
            last_logged_alarm_state = True
            print(f"[DEBUG] Setting alarm state to TRIGGERED")
        
        # Update timestamp for all event types
        last_event_timestamps[event_key] = current_time
    
    with app.app_context():
        # Convert timestamp to IST
        ist = timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        event = EventLog(event_type=event_type, description=description, timestamp=now_ist)
        db.session.add(event)
        db.session.commit()
        
        # Get updated statistics
        total_events = EventLog.query.count()
        door_open_events = EventLog.query.filter_by(event_type='door_open').count()
        door_close_events = EventLog.query.filter_by(event_type='door_close').count()
        alarm_events = EventLog.query.filter_by(event_type='alarm_triggered').count()
        
        # Get timer setting
        timer_setting = Setting.query.filter_by(key='timer_duration').first()
        timer_set = timer_setting.value if timer_setting else '30'
        
        # Prepare real-time status payload
        last_event = EventLog.query.order_by(EventLog.timestamp.desc()).first()
        payload = {
            'event': event.to_dict(),
            'door_status': 'Open' if door_open else 'Closed',
            'alarm_status': 'Active' if alarm_active else 'Inactive',
            'timer_set': timer_set,
            'last_event': last_event.to_dict() if last_event else None,
            'statistics': {
                'total_events': total_events,
                'door_open_events': door_open_events,
                'door_close_events': door_close_events,
                'alarm_events': alarm_events
            }
        }
        # Broadcast event to all connected clients using enhanced function
        print(f"[DEBUG] Broadcasting WebSocket event: {event_type}")
        broadcast_event(payload)

def send_alarm_email(duration):
    try:
        print(f"[DEBUG] Attempting to send alarm email for duration: {duration}s")
        
        with app.app_context():
            email_config = EmailConfig.query.first()
            if not email_config:
                print("[DEBUG] No email configuration found in database")
                return
                
            if not email_config.is_configured:
                print("[DEBUG] Email configuration is not marked as configured")
                return
                
            if not email_config.sender_email or not email_config.app_password or not email_config.recipient_emails:
                print(f"[DEBUG] Email configuration incomplete: sender={bool(email_config.sender_email)}, password={bool(email_config.app_password)}, recipients={bool(email_config.recipient_emails)}")
                return
            
            print(f"[DEBUG] Email config found - Sender: {email_config.sender_email}")
            print(f"[DEBUG] Recipients: {email_config.recipient_emails}")
            
            recipients = [email.strip() for email in email_config.recipient_emails.split(',')]
            
            msg = MIMEMultipart()
            msg['From'] = email_config.sender_email
            msg['To'] = ", ".join(recipients)
            msg['Subject'] = "🚨 DOOR ALARM TRIGGERED - BSM Security System"
            
            body = f"""
🚨 SECURITY ALERT: Door alarm has been triggered!

📅 Date & Time: {datetime.now().strftime('%A, %B %d, %Y at %I:%M:%S %p')}
⏱️  Duration: {duration} seconds
🚪 Location: Main Door Security System
🏢 Facility: BSM Science and Technology Solutions

⚠️  IMMEDIATE ACTION REQUIRED:
Please check the door and premises immediately for security.

This is an automated alert from the BSM Door Alarm System v2.0
System Status: OPERATIONAL
Alert Priority: HIGH

---
BSM Science and Technology Solutions
Advanced Door Security & Monitoring Systems
            """
            
            msg.attach(MIMEText(body, 'plain'))
            
            print("[DEBUG] Connecting to Gmail SMTP...")
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            
            print("[DEBUG] Attempting login...")
            server.login(email_config.sender_email, email_config.app_password)
            
            print(f"[DEBUG] Sending email to {len(recipients)} recipients...")
            text = msg.as_string()
            server.sendmail(email_config.sender_email, recipients, text)
            server.quit()
            
            print(f"[SUCCESS] Alarm email sent successfully to: {', '.join(recipients)}")
            
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"SMTP Authentication failed - check email credentials: {e}"
        print(f"[ERROR] {error_msg}")
    except smtplib.SMTPException as e:
        error_msg = f"SMTP error occurred: {e}"
        print(f"[ERROR] {error_msg}")
    except Exception as e:
        error_msg = f"Email sending failed with unexpected error: {e}"
        print(f"[ERROR] {error_msg}")

# Flask-Login user loader
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Forms
class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])

class AdminSettingsForm(FlaskForm):
    timer_duration = IntegerField('Alarm Timer (seconds)', validators=[DataRequired()])
    sender_email = StringField('Sender Email', validators=[DataRequired(), Email()])
    app_password = PasswordField('App Password', validators=[DataRequired()])
    recipient_emails = StringField('Recipient Emails (comma separated)', validators=[DataRequired()])

class CreateUserForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=4, max=20)])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    is_admin = BooleanField('Admin User')
    permissions = SelectMultipleField('Permissions', 
        choices=[
            ('dashboard', 'Dashboard'),
            ('controls', 'Controls'),
            ('event_log', 'Event Log'),
            ('report', 'Report'),
            ('analytics', 'Analytics'),
            ('admin', 'Admin')
        ],
        validators=[DataRequired()]
    )

# Routes
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=True)
            session.permanent = True
            app.permanent_session_lifetime = timedelta(minutes=30)
            
            # Check if admin needs onboarding
            if user.is_admin:
                email_config = EmailConfig.query.first()
                if not email_config or not email_config.is_configured:
                    return redirect(url_for('admin_onboarding'))
                    
            return redirect(url_for('dashboard'))
        return render_template('login.html', form=form, error='Invalid username or password')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    # Get user permissions
    permissions = current_user.permissions.split(',') if current_user.permissions else ['dashboard']
    
    # Get system status
    door_status = "Open" if door_open else "Closed"
    alarm_status = "Active" if alarm_active else "Inactive"
    timer_set = Setting.query.filter_by(key='timer_duration').first().value
    
    # Get event counts
    total_events = EventLog.query.count()
    door_open_events = EventLog.query.filter_by(event_type='door_open').count()
    door_close_events = EventLog.query.filter_by(event_type='door_close').count()
    alarm_events = EventLog.query.filter_by(event_type='alarm_triggered').count()
    
    # Get last event
    last_event = EventLog.query.order_by(EventLog.timestamp.desc()).first()
    last_event_str = last_event.to_dict() if last_event else None
    
    return render_template('dashboard.html', 
        permissions=permissions,
        door_status=door_status,
        alarm_status=alarm_status,
        timer_set=timer_set,
        total_events=total_events,
        door_open_events=door_open_events,
        door_close_events=door_close_events,
        alarm_events=alarm_events,
        last_event=last_event_str
    )

@app.route('/event-log')
@login_required
def event_log():
    """Display full event log page"""
    if 'event_log' not in current_user.permissions.split(','):
        return redirect(url_for('dashboard'))
    
    # Get all events with pagination
    page = request.args.get('page', 1, type=int)
    per_page = 50
    events = EventLog.query.order_by(EventLog.timestamp.desc()).paginate(
        page=page, per_page=per_page, error_out=False)
    
    return render_template('event_log.html', 
        events=events.items,
        pagination=events,
        permissions=current_user.permissions.split(',')
    )

@app.route('/admin/onboarding', methods=['GET', 'POST'])
@login_required
def admin_onboarding():
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    
    # Check if already configured
    email_config = EmailConfig.query.first()
    if email_config and email_config.is_configured:
        return redirect(url_for('admin_panel'))
        
    form = AdminSettingsForm()
    if form.validate_on_submit():
        # Save email configuration
        email_config = EmailConfig.query.first()
        if not email_config:
            email_config = EmailConfig()
            
        email_config.sender_email = form.sender_email.data
        email_config.app_password = form.app_password.data
        email_config.recipient_emails = form.recipient_emails.data
        email_config.is_configured = True
        
        db.session.add(email_config)
        
        # Save timer setting
        timer_setting = Setting.query.filter_by(key='timer_duration').first()
        if timer_setting:
            timer_setting.value = str(form.timer_duration.data)
        else:
            timer_setting = Setting(key='timer_duration', value=str(form.timer_duration.data))
            db.session.add(timer_setting)
            
        db.session.commit()
        return redirect(url_for('admin_panel'))
        
    return render_template('admin_onboarding.html', form=form)

@app.route('/admin')
@login_required
def admin_panel():
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    
    users = User.query.all()
    create_form = CreateUserForm()
    settings_form = AdminSettingsForm()
    
    # Pre-fill settings form
    timer_setting = Setting.query.filter_by(key='timer_duration').first()
    email_config = EmailConfig.query.first()
    
    if timer_setting:
        settings_form.timer_duration.data = int(timer_setting.value)
    if email_config:
        settings_form.sender_email.data = email_config.sender_email
        settings_form.app_password.data = email_config.app_password
        settings_form.recipient_emails.data = email_config.recipient_emails
    
    return render_template('admin.html', 
        users=users, 
        create_form=create_form, 
        settings_form=settings_form,
        permissions=current_user.permissions.split(',')
    )

@app.route('/admin/create-user', methods=['POST'])
@login_required
def create_user():
    if not current_user.is_admin:
        flash('Permission denied', 'error')
        return redirect(url_for('admin_panel'))
    
    form = CreateUserForm()
    
    # Debug form data
    print(f"Form data received: {request.form}")
    print(f"Form keys: {list(request.form.keys())}")
    print(f"Permissions received: {request.form.getlist('permissions')}")
    
    # Try different ways to get permissions
    permissions_wtf = form.permissions.data if form.permissions.data else []
    permissions_direct = request.form.getlist('permissions')
    
    print(f"WTF Permissions: {permissions_wtf}")
    print(f"Direct permissions: {permissions_direct}")
    
    # Manual validation approach to handle permission selection better
    username = form.username.data
    password = form.password.data
    is_admin = form.is_admin.data
    
    # Use WTF form data first, fallback to direct request
    permissions = permissions_wtf if permissions_wtf else permissions_direct
    
    # Basic validation
    validation_errors = []
    if not username or len(username) < 4:
        validation_errors.append('Username must be at least 4 characters long')
    if not password or len(password) < 6:
        validation_errors.append('Password must be at least 6 characters long')
    if not permissions:
        validation_errors.append('Please select at least one permission for the user')
    
    if not validation_errors:
        # Check if user already exists
        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash(f'User {username} already exists', 'error')
            return redirect(url_for('admin_panel'))
        
        try:
            # Create new user
            new_user = User(
                username=username,
                is_admin=is_admin,
                permissions=','.join(permissions) if permissions else 'dashboard'
            )
            new_user.set_password(password)
            
            db.session.add(new_user)
            db.session.commit()
            
            log_event('user_created', f'User {username} created by admin')
            flash(f'User {username} created successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating user: {str(e)}', 'error')
    else:
        # Validation failed
        for error in validation_errors:
            flash(error, 'error')
    
    return redirect(url_for('admin_panel'))

@app.route('/admin/settings', methods=['POST'])
@login_required
def admin_settings():
    if not current_user.is_admin:
        return jsonify({'error': 'Permission denied'}), 403
    
    form = AdminSettingsForm()
    if form.validate_on_submit():
        # Save email configuration
        email_config = EmailConfig.query.first()
        if not email_config:
            email_config = EmailConfig()
            
        email_config.sender_email = form.sender_email.data
        email_config.app_password = form.app_password.data
        email_config.recipient_emails = form.recipient_emails.data
        email_config.is_configured = True
        
        db.session.add(email_config)
        
        # Save timer setting
        timer_setting = Setting.query.filter_by(key='timer_duration').first()
        if timer_setting:
            timer_setting.value = str(form.timer_duration.data)
        else:
            timer_setting = Setting(key='timer_duration', value=str(form.timer_duration.data))
            db.session.add(timer_setting)
            
        db.session.commit()
        log_event('settings_changed', 'Admin updated system settings')
    
    return redirect(url_for('admin_panel'))

@app.route('/api/users/<int:user_id>', methods=['PUT', 'DELETE'])
@login_required
def manage_user(user_id):
    if not current_user.is_admin:
        return jsonify({'error': 'Permission denied'}), 403
    
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    if request.method == 'PUT':
        data = request.get_json()
        user.is_admin = data.get('is_admin', False)
        user.permissions = data.get('permissions', '')
        db.session.commit()
        log_event('user_updated', f'User {user.username} permissions updated by admin')
        return jsonify({'success': True})
    
    elif request.method == 'DELETE':
        if user.username == 'admin':
            return jsonify({'error': 'Cannot delete admin user'}), 403
        username = user.username
        db.session.delete(user)
        db.session.commit()
        log_event('user_deleted', f'User {username} deleted by admin')
        return jsonify({'success': True})

@app.route('/analytics')
@login_required
def analytics():
    if 'analytics' not in current_user.permissions.split(','):
        return redirect(url_for('dashboard'))
    
    # Get analytics data
    from sqlalchemy import func
    
    # Events by type
    events_by_type = db.session.query(
        EventLog.event_type, 
        func.count(EventLog.id)
    ).group_by(EventLog.event_type).all()
    
    # Events by day (last 7 days)
    today = date.today()
    week_ago = today - timedelta(days=7)
    
    events_by_day = db.session.query(
        func.date(EventLog.timestamp),
        func.count(EventLog.id)
    ).filter(EventLog.timestamp >= week_ago).group_by(
        func.date(EventLog.timestamp)
    ).all()
    
    return render_template('analytics.html',
        events_by_type=events_by_type,
        events_by_day=events_by_day,
        permissions=current_user.permissions.split(',')
    )

@app.route('/reports')
@login_required
def reports():
    if 'report' not in current_user.permissions.split(','):
        return redirect(url_for('dashboard'))
    
    return render_template('reports.html',
        permissions=current_user.permissions.split(',')
    )

@app.route('/api/events')
@login_required
def get_events():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    events = EventLog.query.order_by(EventLog.timestamp.desc()).paginate(
        page=page, per_page=per_page, error_out=False)
    return jsonify({
        'events': [event.to_dict() for event in events.items],
        'total': events.total,
        'pages': events.pages,
        'current_page': page
    })

@app.route('/api/statistics')
@login_required
def get_statistics():
    """Get real-time event statistics"""
    total_events = EventLog.query.count()
    door_open_events = EventLog.query.filter_by(event_type='door_open').count()
    door_close_events = EventLog.query.filter_by(event_type='door_close').count()
    alarm_events = EventLog.query.filter_by(event_type='alarm_triggered').count()
    
    return jsonify({
        'total_events': total_events,
        'door_open_events': door_open_events,
        'door_close_events': door_close_events,
        'alarm_events': alarm_events
    })

@app.route('/api/settings', methods=['POST'])
@login_required
def update_settings():
    if 'controls' not in current_user.permissions.split(','):
        return jsonify({'error': 'Permission denied'}), 403
        
    data = request.get_json()
    if 'timer_duration' in data:
        setting = Setting.query.filter_by(key='timer_duration').first()
        if setting:
            setting.value = str(data['timer_duration'])
            db.session.commit()
            log_event('setting_changed', f'Timer duration changed to {data["timer_duration"]} seconds')
            return jsonify({'success': True})
    return jsonify({'error': 'Invalid setting'}), 400

@app.route('/api/backup')
@login_required
def backup_database():
    if 'controls' not in current_user.permissions.split(','):
        return jsonify({'error': 'Permission denied'}), 403
        
    return send_file('instance/alarm_system.db', as_attachment=True, download_name='alarm_system_backup.db')

@app.route('/api/report', methods=['POST'])
@login_required
def generate_report():
    if 'report' not in current_user.permissions.split(','):
        return jsonify({'error': 'Permission denied'}), 403
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
            
        start_date = datetime.strptime(data['start_date'], '%Y-%m-%d')
        end_date = datetime.strptime(data['end_date'], '%Y-%m-%d') + timedelta(days=1)
        event_types = data.get('event_types', [])
        
        query = EventLog.query.filter(EventLog.timestamp.between(start_date, end_date))
        if event_types:
            query = query.filter(EventLog.event_type.in_(event_types))
            
        events = query.all()
        
        # For CSV
        if data.get('format') == 'csv':
            import csv
            from io import StringIO
            
            output = StringIO()
            writer = csv.writer(output)
            writer.writerow(['ID', 'Event Type', 'Description', 'Timestamp'])
            for event in events:
                writer.writerow([event.id, event.event_type, event.description, event.timestamp])
                
            output.seek(0)
            return jsonify({'csv_data': output.getvalue()})
        
        # For PDF - Professional Modern Design
        elif data.get('format') == 'pdf':
            try:
                from reportlab.lib.pagesizes import letter, A4
                from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
                from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
                from reportlab.lib.units import inch, cm
                from reportlab.lib import colors
                from reportlab.platypus.flowables import HRFlowable
                from io import BytesIO
                import base64
            except ImportError as e:
                return jsonify({'error': f'PDF generation libraries not available: {str(e)}'}), 500
            
            buffer = BytesIO()
            
            # Custom page template with margins
            doc = SimpleDocTemplate(
            buffer, 
            pagesize=A4,
            rightMargin=2*cm,
            leftMargin=2*cm,
            topMargin=2.5*cm,
            bottomMargin=2*cm
        )
        
        # Create custom colors
        primary_blue = colors.Color(0.23, 0.51, 0.96)  # #3b82f6
        dark_blue = colors.Color(0.06, 0.09, 0.16)     # #0f172a  
        light_gray = colors.Color(0.97, 0.98, 0.99)    # #f8fafc
        medium_gray = colors.Color(0.55, 0.65, 0.75)   # #8b9aab
        
        # Custom styles
        styles = getSampleStyleSheet()
        
        # Company Header Style
        company_style = ParagraphStyle(
            'CompanyHeader',
            parent=styles['Title'],
            fontSize=24,
            fontName='Helvetica-Bold',
            textColor=dark_blue,
            spaceAfter=5,
            alignment=1,  # Center
            letterSpacing=1
        )
        
        # Subtitle Style
        subtitle_style = ParagraphStyle(
            'Subtitle',
            parent=styles['Normal'],
            fontSize=12,
            fontName='Helvetica',
            textColor=medium_gray,
            spaceAfter=30,
            alignment=1,
            letterSpacing=0.5
        )
        
        # Main Title Style
        title_style = ParagraphStyle(
            'MainTitle',
            parent=styles['Title'],
            fontSize=20,
            fontName='Helvetica-Bold',
            textColor=primary_blue,
            spaceAfter=20,
            spaceBefore=10,
            alignment=0,  # Left
            borderWidth=0,
            borderPadding=10
        )
        
        # Report Info Style
        info_header_style = ParagraphStyle(
            'InfoHeader',
            parent=styles['Normal'],
            fontSize=11,
            fontName='Helvetica-Bold',
            textColor=dark_blue,
            spaceAfter=15,
            spaceBefore=20,
            leftIndent=0
        )
        
        info_content_style = ParagraphStyle(
            'InfoContent',
            parent=styles['Normal'],
            fontSize=10,
            fontName='Helvetica',
            textColor=colors.black,
            spaceAfter=20,
            leftIndent=20
        )
        
        # Story elements
        story = []
        
        # Header Section
        story.append(Paragraph("BSM SCIENCE & TECHNOLOGY SOLUTIONS", company_style))
        story.append(Paragraph("Advanced Door Security & Monitoring Systems", subtitle_style))
        
        # Horizontal line
        story.append(HRFlowable(width="100%", thickness=2, color=primary_blue))
        story.append(Spacer(1, 20))
        
        # Main Title
        story.append(Paragraph("🔒 DOOR ALARM SYSTEM REPORT", title_style))
        story.append(Spacer(1, 10))
        
        # Report Information Section
        story.append(Paragraph("REPORT DETAILS", info_header_style))
        
        # Calculate statistics
        door_open_count = sum(1 for e in events if e.event_type == 'door_open')
        door_close_count = sum(1 for e in events if e.event_type == 'door_close') 
        alarm_count = sum(1 for e in events if e.event_type == 'alarm_triggered')
        
        report_details = f"""
        <table>
            <tr><td width="140"><b>Report Period:</b></td><td>{data['start_date']} to {data['end_date']}</td></tr>
            <tr><td><b>Event Types:</b></td><td>{', '.join([t.replace('_', ' ').title() for t in event_types]) if event_types else 'All Event Types'}</td></tr>
            <tr><td><b>Total Events:</b></td><td><font color="#3b82f6"><b>{len(events)}</b></font></td></tr>
            <tr><td><b>Door Openings:</b></td><td><font color="#10b981">{door_open_count}</font></td></tr>
            <tr><td><b>Door Closings:</b></td><td><font color="#059669">{door_close_count}</font></td></tr>
            <tr><td><b>Alarm Events:</b></td><td><font color="#ef4444">{alarm_count}</font></td></tr>
            <tr><td><b>Generated On:</b></td><td>{datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')}</td></tr>
            <tr><td><b>System Status:</b></td><td><font color="#10b981"><b>OPERATIONAL</b></font></td></tr>
        </table>
        """
        
        story.append(Paragraph(report_details, info_content_style))
        story.append(Spacer(1, 25))
        
        # Events Table Section
        if events:
            story.append(Paragraph("EVENT LOG DETAILS", info_header_style))
            
            # Table headers
            table_data = [
                ['Event ID', 'Type', 'Description', 'Date', 'Time']
            ]
            
            # Table data with enhanced formatting
            for event in events:
                event_type_display = event.event_type.replace('_', ' ').title()
                date_part = event.timestamp.strftime('%Y-%m-%d')
                time_part = event.timestamp.strftime('%H:%M:%S')
                
                table_data.append([
                    f"#{event.id}",
                    event_type_display,
                    event.description,
                    date_part,
                    time_part
                ])
            
            # Create enhanced table
            table = Table(
                table_data, 
                colWidths=[1.2*cm, 2.8*cm, 6*cm, 2.5*cm, 2*cm],
                repeatRows=1
            )
            
            # Professional table styling
            table.setStyle(TableStyle([
                # Header row styling
                ('BACKGROUND', (0, 0), (-1, 0), primary_blue),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
                ('VALIGN', (0, 0), (-1, 0), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, 0), 12),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                
                # Data rows styling
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 1), (-1, -1), 9),
                ('ALIGN', (0, 1), (0, -1), 'CENTER'),  # ID column center
                ('ALIGN', (1, 1), (1, -1), 'CENTER'),  # Type column center  
                ('ALIGN', (2, 1), (2, -1), 'LEFT'),    # Description left
                ('ALIGN', (3, 1), (3, -1), 'CENTER'),  # Date center
                ('ALIGN', (4, 1), (4, -1), 'CENTER'),  # Time center
                ('VALIGN', (0, 1), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 1), (-1, -1), 8),
                ('BOTTOMPADDING', (0, 1), (-1, -1), 8),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                
                # Alternating row colors
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, light_gray]),
                
                # Grid lines
                ('GRID', (0, 0), (-1, -1), 0.5, medium_gray),
                ('LINEBELOW', (0, 0), (-1, 0), 2, primary_blue),
                
                # Special coloring for event types
                ('TEXTCOLOR', (1, 1), (1, -1), dark_blue),
            ]))
            
            story.append(table)
        else:
            story.append(Paragraph("No events found for the specified criteria.", info_content_style))
        
        # Footer
        story.append(Spacer(1, 30))
        story.append(HRFlowable(width="100%", thickness=1, color=medium_gray))
        
        footer_style = ParagraphStyle(
            'Footer',
            parent=styles['Normal'],
            fontSize=8,
            fontName='Helvetica',
            textColor=medium_gray,
            alignment=1,
            spaceAfter=0
        )
        
        story.append(Spacer(1, 10))
        story.append(Paragraph(
            f"Generated by BSM Door Alarm System v2.0 | Report ID: RPT-{datetime.now().strftime('%Y%m%d%H%M%S')} | Confidential", 
            footer_style
        ))
        
        try:
            # Build PDF
            doc.build(story)
            
            buffer.seek(0)
            pdf_data = base64.b64encode(buffer.getvalue()).decode()
            return jsonify({'pdf_data': pdf_data})
        
        except Exception as e:
                print(f"PDF Generation Error: {str(e)}")
                return jsonify({'error': f'PDF generation failed: {str(e)}'}), 500
        
        # For JSON (default)
        return jsonify({'events': [event.to_dict() for event in events]})
        
    except Exception as e:
        print(f"Report Generation Error: {str(e)}")
        return jsonify({'error': f'Report generation failed: {str(e)}'}), 500

# All test routes removed for production deployment

# WebSocket events
@socketio.on('connect', namespace='/events')
def handle_connect():
    print(f'✅ WebSocket client connected: {request.sid}')
    print(f'📡 Total clients connected: {len(socketio.server.manager.rooms.get("/events", {}))}')

@socketio.on('disconnect', namespace='/events')
def handle_disconnect():
    print(f'❌ WebSocket client disconnected: {request.sid}')

@socketio.on('ping', namespace='/events')
def handle_ping(data):
    """Handle ping from client for connection testing"""
    print(f"[WEBSOCKET] Ping received from {request.sid}")
    emit('pong', {'timestamp': datetime.now().isoformat()})

# Start door monitoring in a separate thread
monitor_thread_started = False
monitor_thread_lock = threading.Lock()

def start_monitoring():
    global monitor_thread_started
    with monitor_thread_lock:
        if not monitor_thread_started:
            monitor_thread = threading.Thread(target=monitor_door, name="DoorMonitor")
            monitor_thread.daemon = True
            monitor_thread.start()
            monitor_thread_started = True
            print("🔍 Door monitoring thread started")
        else:
            print("⚠️ Door monitoring thread already running")

if __name__ == '__main__':
    print("🚀 Starting eDOMOS-v2 Door Alarm System...")
    print("📡 WebSocket support enabled")
    print("🔄 Event-driven real-time updates active")
    init_system()
    start_monitoring()
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)
<html>
<head>
    <title>Test Dashboard - eDOMOS v2</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #1a1a1a; color: #fff; }
        .status-panel { background: #333; padding: 20px; margin: 10px 0; border-radius: 8px; display: inline-block; min-width: 200px; }
        .status-value { font-size: 2rem; font-weight: bold; color: #3b82f6; }
        .event-feed { background: #333; padding: 20px; margin: 20px 0; border-radius: 8px; }
        .event-item { background: #444; padding: 10px; margin: 5px 0; border-radius: 5px; }
        .stats { display: flex; gap: 20px; flex-wrap: wrap; }
        button { padding: 10px 20px; margin: 5px; background: #3b82f6; color: white; border: none; border-radius: 5px; cursor: pointer; }
    </style>
</head>
<body>
    <h1>🔒 Test Dashboard - eDOMOS v2 Real-Time Testing</h1>
    
    <div class="stats">
        <div class="status-panel">
            <h3>Door Status</h3>
            <div class="status-value" id="door-status">{{ door_status }}</div>
        </div>
        <div class="status-panel">
            <h3>Alarm Status</h3>
            <div class="status-value" id="alarm-status">{{ alarm_status }}</div>
        </div>
        <div class="status-panel">
            <h3>Timer Setting</h3>
            <div class="status-value" id="timer-set">{{ timer_set }}s</div>
        </div>
        <div class="status-panel">
            <h3>Total Events</h3>
            <div class="status-value" id="total-events">{{ total_events }}</div>
        </div>
    </div>
    
    <div class="stats">
        <div class="status-panel">
            <h3>Door Openings</h3>
            <div class="status-value" id="door-open-events">{{ door_open_events }}</div>
        </div>
        <div class="status-panel">
            <h3>Door Closings</h3>
            <div class="status-value" id="door-close-events">{{ door_close_events }}</div>
        </div>
        <div class="status-panel">
            <h3>Alarm Events</h3>
            <div class="status-value" id="alarm-events">{{ alarm_events }}</div>
        </div>
    </div>
    
    <h3>🎮 Test Controls:</h3>
    <button onclick="triggerEvent('door_open')">🚪 Trigger Door Open</button>
    <button onclick="triggerEvent('door_close')">🔒 Trigger Door Close</button>
    <button onclick="triggerEvent('alarm')">🚨 Trigger Alarm</button>
    
    <div class="event-feed">
        <h3>📡 Live Event Feed</h3>
        <div id="event-list">
            <div style="opacity: 0.6;">Waiting for events...</div>
        </div>
    </div>
    
    <div style="background: #222; padding: 15px; border-radius: 8px; margin-top: 20px;">
        <h4>WebSocket Status: <span id="connection-status">Connecting...</span></h4>
        <p>Events received: <span id="events-received">0</span></p>
    </div>
    
    <script>
        let socket = null;
        let eventCount = 0;
        let eventsReceived = 0;
        
        // Initialize WebSocket
        socket = io('/events', {
            transports: ['websocket', 'polling'],
            reconnection: true
        });
        
        socket.on('connect', () => {
            console.log('✅ WebSocket connected');
            document.getElementById('connection-status').textContent = 'Connected ✅';
            document.getElementById('connection-status').style.color = '#10b981';
        });
        
        socket.on('disconnect', () => {
            console.log('❌ WebSocket disconnected');
            document.getElementById('connection-status').textContent = 'Disconnected ❌';
            document.getElementById('connection-status').style.color = '#ef4444';
        });
        
        socket.on('new_event', (data) => {
            console.log('📡 Received real-time event:', data);
            eventsReceived++;
            document.getElementById('events-received').textContent = eventsReceived;
            
            // Update statistics in real-time
            if (data.statistics) {
                document.getElementById('total-events').textContent = data.statistics.total_events || 0;
                document.getElementById('door-open-events').textContent = data.statistics.door_open_events || 0;
                document.getElementById('door-close-events').textContent = data.statistics.door_close_events || 0;
                document.getElementById('alarm-events').textContent = data.statistics.alarm_events || 0;
            }
            
            // Update status
            if (data.door_status) document.getElementById('door-status').textContent = data.door_status;
            if (data.alarm_status) document.getElementById('alarm-status').textContent = data.alarm_status;
            if (data.timer_set) document.getElementById('timer-set').textContent = data.timer_set + 's';
            
            // Add to event feed
            if (data.event) {
                addEventToFeed(data.event);
            }
        });
        
        function addEventToFeed(event) {
            const eventList = document.getElementById('event-list');
            
            // Clear waiting message
            if (eventList.innerHTML.includes('Waiting for events')) {
                eventList.innerHTML = '';
            }
            
            const eventDiv = document.createElement('div');
            eventDiv.className = 'event-item';
            eventDiv.style.opacity = '0';
            eventDiv.style.transform = 'translateX(-20px)';
            eventDiv.style.transition = 'all 0.3s ease';
            
            let icon = '📝';
            if (event.event_type === 'door_open') icon = '🚪';
            else if (event.event_type === 'door_close') icon = '🔒';
            else if (event.event_type === 'alarm_triggered') icon = '🚨';
            
            eventDiv.innerHTML = `
                <strong>${icon} ${event.event_type.replace('_', ' ').toUpperCase()}</strong><br>
                <small>${event.description} | #${event.id} | ${new Date().toLocaleTimeString()}</small>
            `;
            
            eventList.insertBefore(eventDiv, eventList.firstChild);
            
            // Animate in
            setTimeout(() => {
                eventDiv.style.opacity = '1';
                eventDiv.style.transform = 'translateX(0)';
            }, 100);
            
            // Keep only last 10 events
            while (eventList.children.length > 10) {
                eventList.removeChild(eventList.lastChild);
            }
        }
        
        function triggerEvent(type) {
            const urls = {
                'door_open': '/trigger-door-open',
                'door_close': '/trigger-door-close', 
                'alarm': '/trigger-alarm'
            };
            
            fetch(urls[type])
                .then(response => response.text())
                .then(data => console.log('✅ Event triggered:', data))
                .catch(error => console.error('❌ Error:', error));
        }
    </script>
</body>
</html>
        ''', 
# Production code only

# Test routes removed for production
<!DOCTYPE html>
<html>
<head>
    <title>Enhanced Event Log - eDOMOS v2</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #1a1a1a; color: #fff; }
        .event-entry { background: #2a2a2a; padding: 10px; margin: 5px 0; border-radius: 5px; border-left: 4px solid #3b82f6; }
        .event-entry.door-open { border-left-color: #f59e0b; }
        .event-entry.door-close { border-left-color: #10b981; }
        .event-entry.alarm { border-left-color: #ef4444; }
        button { padding: 10px 20px; margin: 5px; background: #3b82f6; color: white; border: none; border-radius: 5px; cursor: pointer; }
        .status { padding: 10px; border-radius: 5px; margin: 10px 0; }
        .connected { background: #10b981; }
        .disconnected { background: #ef4444; }
    </style>
</head>
<body>
    <h1>🔗 Enhanced WebSocket Event Log - eDOMOS v2</h1>
    <div id="status" class="status disconnected">Connecting...</div>
    
    <h3>🎮 Test Controls:</h3>
    <button onclick="triggerEvent('/trigger_event')">🎲 Random Event</button>
    <button onclick="triggerEvent('/trigger-door-open')">🚪 Door Open</button>
    <button onclick="triggerEvent('/trigger-door-close')">🔒 Door Close</button>
    <button onclick="triggerEvent('/trigger-alarm')">🚨 Alarm</button>
    
    <h3>📊 Statistics:</h3>
    <div style="display: flex; gap: 20px;">
        <div>Events Received: <span id="events-received">0</span></div>
        <div>Total Events: <span id="total-events">0</span></div>
        <div>Door Events: <span id="door-events">0</span></div>
        <div>Alarm Events: <span id="alarm-events">0</span></div>
    </div>
    
    <h3>📡 Live Event Stream:</h3>
    <div id="event-log" style="max-height: 400px; overflow-y: auto; border: 1px solid #333; padding: 10px; background: #111;">
        <div style="text-align: center; opacity: 0.6;">Waiting for events...</div>
    </div>

    <script>
        // Apply your example logic with enhancements
        const socket = io('/events');
        let eventsReceived = 0;
        
        socket.on('connect', () => {
            console.log('✅ Connected to WebSocket server');
            document.getElementById('status').innerHTML = '✅ Connected! Socket ID: ' + socket.id;
            document.getElementById('status').className = 'status connected';
        });
        
        socket.on('disconnect', (reason) => {
            console.log('❌ Disconnected:', reason);
            document.getElementById('status').innerHTML = '❌ Disconnected: ' + reason;
            document.getElementById('status').className = 'status disconnected';
        });
        
        // Enhanced event handler applying your example logic
        socket.on('new_event', (data) => {
            console.log('📡 Received event:', data);
            eventsReceived++;
            document.getElementById('events-received').textContent = eventsReceived;
            
            // Update statistics from WebSocket data
            if (data.statistics) {
                document.getElementById('total-events').textContent = data.statistics.total_events || 0;
                const doorEvents = (data.statistics.door_open_events || 0) + (data.statistics.door_close_events || 0);
                document.getElementById('door-events').textContent = doorEvents;
                document.getElementById('alarm-events').textContent = data.statistics.alarm_events || 0;
            }
            
            // Add event to log (enhanced from your example)
            addEventToLog(data);
        });
        
        function addEventToLog(data) {
            const log = document.getElementById('event-log');
            
            // Clear waiting message
            if (log.innerHTML.includes('Waiting for events')) {
                log.innerHTML = '';
            }
            
            const entry = document.createElement('div');
            entry.className = 'event-entry';
            
            if (data.event) {
                // Add specific styling based on event type
                if (data.event.event_type === 'door_open') {
                    entry.classList.add('door-open');
                } else if (data.event.event_type === 'door_close') {
                    entry.classList.add('door-close');
                } else if (data.event.event_type === 'alarm_triggered') {
                    entry.classList.add('alarm');
                }
                
                // Enhanced display format (applying your example)
                const timestamp = new Date().toLocaleTimeString();
                const eventType = data.event.event_type.replace('_', ' ').toUpperCase();
                
                entry.innerHTML = `
                    <strong>${timestamp} - ${getEventIcon(data.event.event_type)} ${eventType}</strong><br>
                    <small>${data.event.description} | ID: #${data.event.id}</small>
                `;
            } else {
                // Fallback for basic events
                entry.innerHTML = '<strong>' + new Date().toLocaleTimeString() + '</strong> - ' + JSON.stringify(data, null, 2);
            }
            
            // Insert at top (latest first)
            log.insertBefore(entry, log.firstChild);
            
            // Limit to 20 entries
            while (log.children.length > 20) {
                log.removeChild(log.lastChild);
            }
        }
        
        function getEventIcon(eventType) {
            switch (eventType) {
                case 'door_open': return '🚪';
                case 'door_close': return '🔒';
                case 'alarm_triggered': return '🚨';
                default: return '📋';
            }
        }
        
        function triggerEvent(url) {
            console.log('🎯 Triggering:', url);
            fetch(url)
                .then(response => response.text())
                .then(data => console.log('✅ Response:', data))
                .catch(error => console.error('❌ Error:', error));
        }
    </script>
</body>
</html>
    ''')

@app.route('/test-websocket')
def test_websocket():
    """Original WebSocket test page"""
    return render_template_string('''
<!DOCTYPE html>
<html>
<head>
    <title>WebSocket Real-Time Test - eDOMOS v2</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #1a1a1a; color: #fff; }
        .container { max-width: 1200px; margin: 0 auto; }
        .status { padding: 15px; margin: 10px 0; border-radius: 8px; border: 2px solid; }
        .connected { background: #d4edda; color: #155724; border-color: #c3e6cb; }
        .disconnected { background: #f8d7da; color: #721c24; border-color: #f5c6cb; }
        .event { background: #e3f2fd; color: #0d47a1; padding: 12px; margin: 8px 0; border-radius: 6px; border-left: 4px solid #2196f3; animation: slideIn 0.3s ease; }
        .event.new { background: #e8f5e8; border-left-color: #4caf50; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin: 20px 0; }
        .stat-card { background: #333; padding: 15px; border-radius: 8px; text-align: center; }
        .stat-number { font-size: 2rem; font-weight: bold; color: #2196f3; }
        .stat-label { font-size: 0.9rem; opacity: 0.8; }
        button { padding: 12px 24px; margin: 8px; cursor: pointer; background: #2196f3; color: white; border: none; border-radius: 6px; font-weight: bold; transition: background 0.3s; }
        button:hover { background: #1976d2; }
        .controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin: 20px 0; }
        @keyframes slideIn { from { opacity: 0; transform: translateX(-20px); } to { opacity: 1; transform: translateX(0); } }
        .debug-info { background: #2a2a2a; padding: 15px; border-radius: 8px; margin: 20px 0; font-size: 0.9rem; }
        .debug-info code { background: #1a1a1a; padding: 2px 6px; border-radius: 3px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔗 WebSocket Real-Time Test - eDOMOS v2</h1>
        <div id="status" class="status disconnected">🔄 Connecting to WebSocket server...</div>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-number" id="totalEvents">0</div>
                <div class="stat-label">Total Events</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="doorEvents">0</div>
                <div class="stat-label">Door Events</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="alarmEvents">0</div>
                <div class="stat-label">Alarm Events</div>
            </div>
            <div class="stat-card">
                <div class="stat-number" id="eventsReceived">0</div>
                <div class="stat-label">Events Received</div>
            </div>
        </div>
        
        <h3>🎮 Test Controls:</h3>
        <div class="controls">
            <button onclick="triggerEvent('/trigger-door-open', 'door_open')">🚪 Trigger Door Open</button>
            <button onclick="triggerEvent('/trigger-door-close', 'door_close')">🔒 Trigger Door Close</button>
            <button onclick="triggerEvent('/trigger-alarm', 'alarm')">🚨 Trigger Alarm</button>
            <button onclick="clearEvents()">🗑️ Clear Events</button>
        </div>
        
        <div class="debug-info">
            <strong>🔧 Debug Info:</strong> 
            Socket ID: <code id="socketId">Not connected</code> | 
            Transport: <code id="transport">Unknown</code> | 
            Connected: <code id="connectedTime">Never</code>
        </div>
        
        <h3>📡 Live Events Stream:</h3>
        <div id="events">
            <div style="text-align: center; opacity: 0.6; padding: 20px;">
                ⏳ Waiting for events...
            </div>
        </div>
    </div>
    
    <script>
        let socket = null;
        let eventsReceived = 0;
        let totalEvents = 0;
        let doorEvents = 0;
        let alarmEvents = 0;
        let connectedTime = null;
        
        console.log('🚀 Initializing enhanced WebSocket test...');
        
        socket = io('/events', {
            transports: ['websocket', 'polling'],
            reconnection: true,
            reconnectionDelay: 1000,
            reconnectionAttempts: 10,
            timeout: 20000
        });
        
        socket.on('connect', () => {
            console.log('✅ Connected to WebSocket server');
            connectedTime = new Date();
            updateStatus(true);
            updateDebugInfo();
        });
        
        socket.on('disconnect', (reason) => {
            console.log('❌ Disconnected:', reason);
            updateStatus(false, reason);
            updateDebugInfo();
        });
        
        socket.on('new_event', (data) => {
            console.log('📡 Received WebSocket event:', data);
            eventsReceived++;
            processEvent(data);
            updateStats();
            updateDebugInfo();
        });
        
        socket.on('connect_error', (error) => {
            console.error('❌ Connection error:', error);
            updateStatus(false, 'Connection Error: ' + error);
        });
        
        function updateStatus(connected, reason = '') {
            const statusEl = document.getElementById('status');
            if (connected) {
                statusEl.innerHTML = '✅ Connected to WebSocket server at ' + new Date().toLocaleTimeString();
                statusEl.className = 'status connected';
            } else {
                statusEl.innerHTML = '❌ Disconnected from WebSocket server' + (reason ? ': ' + reason : '');
                statusEl.className = 'status disconnected';
            }
        }
        
        function updateDebugInfo() {
            document.getElementById('socketId').textContent = socket?.id || 'Not connected';
            document.getElementById('transport').textContent = socket?.io?.engine?.transport?.name || 'Unknown';
            document.getElementById('connectedTime').textContent = connectedTime ? connectedTime.toLocaleTimeString() : 'Never';
        }
        
        function processEvent(data) {
            totalEvents++;
            
            // Update statistics from WebSocket data
            if (data.statistics) {
                document.getElementById('totalEvents').textContent = data.statistics.total_events || totalEvents;
                doorEvents = (data.statistics.door_open_events || 0) + (data.statistics.door_close_events || 0);
                document.getElementById('doorEvents').textContent = doorEvents;
                document.getElementById('alarmEvents').textContent = data.statistics.alarm_events || 0;
            }
            
            // Add event to display
            if (data.event) {
                addEventToDisplay(data.event);
            }
        }
        
        function addEventToDisplay(event) {
            const eventsDiv = document.getElementById('events');
            
            // Clear waiting message
            if (eventsDiv.innerHTML.includes('Waiting for events')) {
                eventsDiv.innerHTML = '';
            }
            
            const eventDiv = document.createElement('div');
            eventDiv.className = 'event new';
            
            let icon = '📝';
            if (event.event_type === 'door_open') icon = '🚪';
            else if (event.event_type === 'door_close') icon = '🔒';
            else if (event.event_type === 'alarm_triggered') icon = '🚨';
            
            eventDiv.innerHTML = `
                <div style="display: flex; justify-content: between; align-items: center;">
                    <div>
                        <strong>${icon} ${event.event_type.replace('_', ' ').toUpperCase()}</strong><br>
                        <small>${event.description}</small>
                    </div>
                    <div style="text-align: right; margin-left: auto;">
                        <small>#${event.id} | ${new Date().toLocaleTimeString()}</small>
                    </div>
                </div>
            `;
            
            eventsDiv.insertBefore(eventDiv, eventsDiv.firstChild);
            
            // Remove highlight after 3 seconds
            setTimeout(() => {
                eventDiv.classList.remove('new');
            }, 3000);
            
            // Keep only last 15 events
            while (eventsDiv.children.length > 15) {
                eventsDiv.removeChild(eventsDiv.lastChild);
            }
        }
        
        function updateStats() {
            document.getElementById('eventsReceived').textContent = eventsReceived;
        }
        
        function triggerEvent(url, type) {
            console.log('🎯 Triggering test event:', type);
            fetch(url)
                .then(response => response.text())
                .then(data => {
                    console.log('✅ Test event response:', data);
                })
                .catch(error => {
                    console.error('❌ Error triggering event:', error);
                });
        }
        
        function clearEvents() {
            document.getElementById('events').innerHTML = '<div style="text-align: center; opacity: 0.6; padding: 20px;">⏳ Waiting for events...</div>';
        }
    </script>
</body>
</html>
    ''')

# Test route for triggering events (for debugging)
@app.route('/api/test-event', methods=['POST'])
@login_required
def test_event():
    if current_user.is_admin:
        data = request.get_json()
        event_type = data.get('event_type', 'door_open')
        description = data.get('description', 'Test event')
        log_event(event_type, description)
        return jsonify({'success': True, 'message': 'Test event triggered'})
    return jsonify({'error': 'Permission denied'}), 403

@app.route('/websocket_test.html')
def websocket_test():
    """Serve WebSocket test page"""
    from flask import send_from_directory
    return send_from_directory('.', 'websocket_test.html')

# WebSocket events
@socketio.on('connect', namespace='/events')
def handle_connect():
    print(f'✅ WebSocket client connected: {request.sid}')
    print(f'📡 Total clients connected: {len(socketio.server.manager.rooms.get("/events", {}))}')

@socketio.on('disconnect', namespace='/events')
def handle_disconnect():
    print(f'❌ WebSocket client disconnected: {request.sid}')

# Start door monitoring in a separate thread
monitor_thread_started = False
monitor_thread_lock = threading.Lock()

def start_monitoring():
    global monitor_thread_started
    with monitor_thread_lock:
        if not monitor_thread_started:
            monitor_thread = threading.Thread(target=monitor_door, name="DoorMonitor")
            monitor_thread.daemon = True
            monitor_thread.start()
            monitor_thread_started = True
            print("🔍 Door monitoring thread started")
        else:
            print("⚠️ Door monitoring thread already running")

if __name__ == '__main__':
    print("🚀 Starting eDOMOS-v2 Door Alarm System...")
    print("📡 WebSocket support enabled")
    print("🔄 Event-driven real-time updates active")
    init_system()
    start_monitoring()
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)