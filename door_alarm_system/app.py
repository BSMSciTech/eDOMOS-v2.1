import os
import time
import threading
import sqlite3
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, date
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_file, flash
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
    global door_open, alarm_active
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
        # Emit event to WebSocket clients
        print(f"[DEBUG] Emitting WebSocket event: {event_type}")
        socketio.emit('new_event', payload, namespace='/events')

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
    return db.session.get(User, int(user_id))

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

# WebSocket events
@socketio.on('connect', namespace='/events')
def handle_connect():
    print('Client connected to WebSocket')

@socketio.on('disconnect', namespace='/events')
def handle_disconnect():
    print('Client disconnected from WebSocket')

# Start door monitoring in a separate thread
def start_monitoring():
    monitor_thread = threading.Thread(target=monitor_door)
    monitor_thread.daemon = True
    monitor_thread.start()

if __name__ == '__main__':
    init_system()
    start_monitoring()
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)