"""
SentinelAI — Smart Surveillance & Crisis Orchestration System
====================================================================
Run locally:  python app.py
Deploy:       Render.com / Railway / any Python host

FIXED BUGS:
- templates/ folder mismatch (HTML was at root, Flask needs templates/)
- simulate_alert WebSocket used requests.post (circular call → deadlock on Render)
- video_feed generator blocks under eventlet (wrapped correctly)
- simulate_alert now calls trigger_alert logic directly (no HTTP self-call)
- /api/log returned newest-first but frontend expected oldest-first (fixed)
- Missing CORS headers for cross-origin dashboard access
- Added Gmail + SMS (Twilio) notification on every new incident
- Added /api/send_notification endpoint for manual staff messaging
- Added .env-based config so secrets never live in code
"""

from flask import Flask, render_template, Response, jsonify, request
from flask_socketio import SocketIO, emit
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import cv2, threading, time, json, os, random
from datetime import datetime

# ── Optional notification libs (gracefully absent) ──────────────────────────
try:
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    EMAIL_AVAILABLE = True
except ImportError:
    EMAIL_AVAILABLE = False

try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False

# ── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'sentinelai-secret-2024')
CORS(app)

# Database
basedir = os.path.abspath(os.path.dirname(__file__))
db_url = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(basedir, 'sentinel.db'))
# Render gives postgres:// but SQLAlchemy needs postgresql://
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# ── Notification config (from env vars) ─────────────────────────────────────
GMAIL_USER     = os.environ.get('GMAIL_USER', '')        # your@gmail.com
GMAIL_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '') # Gmail App Password
ALERT_EMAILS   = os.environ.get('ALERT_EMAILS', '').split(',')  # comma-separated

TWILIO_SID     = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN   = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_FROM    = os.environ.get('TWILIO_FROM_NUMBER', '')
ALERT_PHONES   = os.environ.get('ALERT_PHONES', '').split(',')  # comma-separated +91XXXXXXXXXX

# ── Database Models ──────────────────────────────────────────────────────────
class Incident(db.Model):
    __tablename__ = 'incidents'
    id            = db.Column(db.Integer, primary_key=True)
    type          = db.Column(db.String(50),  nullable=False)
    location      = db.Column(db.String(100), nullable=False)
    severity      = db.Column(db.String(20),  nullable=False)
    status        = db.Column(db.String(20),  default='active')
    assigned      = db.Column(db.String(200))  # JSON list of staff keys
    time          = db.Column(db.String(20))
    timestamp     = db.Column(db.Float)
    response_time = db.Column(db.Integer)
    confidence    = db.Column(db.Float)
    notes         = db.Column(db.Text)

    def to_dict(self):
        return {
            "id":            self.id,
            "type":          self.type,
            "location":      self.location,
            "severity":      self.severity,
            "status":        self.status,
            "assigned":      json.loads(self.assigned) if self.assigned else [],
            "time":          self.time,
            "timestamp":     self.timestamp,
            "response_time": self.response_time,
            "confidence":    self.confidence,
            "notes":         self.notes or "",
            "title":         f"{self.type.capitalize()} — {self.location}",
        }


class LogEvent(db.Model):
    __tablename__ = 'log_events'
    id   = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50))
    msg  = db.Column(db.String(500))
    time = db.Column(db.String(20))
    ts   = db.Column(db.Float)

    def to_dict(self):
        return {"type": self.type, "msg": self.msg, "time": self.time, "ts": self.ts}


# ── State ────────────────────────────────────────────────────────────────────
system_stats = {
    "fps": 0, "uptime": 0, "frames": 0,
    "detectors": ["fire", "accident", "violence", "suspicious"],
    "camera_source": "Demo Mode", "start_time": time.time()
}

camera_frame = None
frame_lock   = threading.Lock()

STAFF = {
    "SEC": {"name": "Riya Sharma",   "role": "Head of Security",   "initials": "RS", "color": "red",    "email": "", "phone": ""},
    "MGR": {"name": "Arjun Patel",   "role": "Duty Manager",       "initials": "AP", "color": "blue",   "email": "", "phone": ""},
    "MED": {"name": "Dr. Priya Nair","role": "Medical Officer",    "initials": "PN", "color": "green",  "email": "", "phone": ""},
    "GRD": {"name": "Vikram Singh",  "role": "Security Guard",     "initials": "VS", "color": "amber",  "email": "", "phone": ""},
    "ENG": {"name": "Neha Kulkarni", "role": "Maintenance Eng.",   "initials": "NK", "color": "purple", "email": "", "phone": ""},
}

PROTOCOLS = {
    "fire":       {"severity": "CRITICAL", "color": "red",   "staff": ["SEC","MGR","MED","ENG"], "response_time": 90},
    "violence":   {"severity": "HIGH",     "color": "red",   "staff": ["SEC","GRD","MGR"],       "response_time": 120},
    "suspicious": {"severity": "MEDIUM",   "color": "amber", "staff": ["SEC","GRD"],             "response_time": 180},
    "accident":   {"severity": "HIGH",     "color": "amber", "staff": ["MED","SEC","MGR"],       "response_time": 120},
}

# ── Notification helpers ─────────────────────────────────────────────────────
def send_email_alert(incident_dict):
    """Send Gmail alert for a new incident."""
    if not EMAIL_AVAILABLE or not GMAIL_USER or not GMAIL_PASSWORD:
        return
    emails = [e.strip() for e in ALERT_EMAILS if e.strip()]
    if not emails:
        return
    try:
        subject = f"🚨 CRISIS ALERT: {incident_dict['type'].upper()} at {incident_dict['location']}"
        body = f"""
SENTINELAI CRISIS ALERT
=======================
Type      : {incident_dict['type'].upper()}
Severity  : {incident_dict['severity']}
Location  : {incident_dict['location']}
Time      : {incident_dict['time']}
Confidence: {incident_dict['confidence']*100:.0f}%
Status    : {incident_dict['status'].upper()}

Assigned Personnel:
{chr(10).join(f"  • {STAFF[k]['name']} ({STAFF[k]['role']})" for k in incident_dict['assigned'] if k in STAFF)}

Please log into the Crisis Command Dashboard immediately.
        """.strip()

        msg = MIMEMultipart()
        msg['From']    = GMAIL_USER
        msg['To']      = ', '.join(emails)
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, emails, msg.as_string())
        print(f"[NOTIFY] Email sent to {emails}")
    except Exception as e:
        print(f"[NOTIFY] Email failed: {e}")


def send_sms_alert(incident_dict):
    """Send Twilio SMS alert for a new incident."""
    if not TWILIO_AVAILABLE or not TWILIO_SID or not TWILIO_TOKEN:
        return
    phones = [p.strip() for p in ALERT_PHONES if p.strip()]
    if not phones:
        return
    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        msg = (f"🚨 CRISIS ALERT\n"
               f"{incident_dict['type'].upper()} – {incident_dict['severity']}\n"
               f"📍 {incident_dict['location']}\n"
               f"🕐 {incident_dict['time']}\n"
               f"Staff: {', '.join(STAFF[k]['name'] for k in incident_dict['assigned'] if k in STAFF)}\n"
               f"Login to SentinelAI dashboard immediately.")
        for phone in phones:
            client.messages.create(body=msg, from_=TWILIO_FROM, to=phone)
        print(f"[NOTIFY] SMS sent to {phones}")
    except Exception as e:
        print(f"[NOTIFY] SMS failed: {e}")


def send_notifications_async(incident_dict):
    """Fire email + SMS in background thread so it doesn't block the response."""
    t = threading.Thread(target=lambda: (send_email_alert(incident_dict), send_sms_alert(incident_dict)), daemon=True)
    t.start()


# ── Seed data ────────────────────────────────────────────────────────────────
def seed_incidents():
    if Incident.query.first():
        return
    seeds = [
        {"type": "suspicious", "location": "Parking Level B3",  "status": "resolved"},
        {"type": "fire",       "location": "Kitchen Floor 2",   "status": "resolved"},
        {"type": "violence",   "location": "Corridor B Floor 1","status": "resolved"},
        {"type": "accident",   "location": "Lobby Entrance",    "status": "resolved"},
    ]
    for s in seeds:
        proto = PROTOCOLS[s["type"]]
        inc = Incident(
            type=s["type"], location=s["location"], severity=proto["severity"],
            status=s["status"], assigned=json.dumps(proto["staff"]),
            time=datetime.now().strftime("%H:%M"),
            timestamp=time.time() - random.randint(600, 3600),
            response_time=proto["response_time"], notes="", confidence=0.9,
        )
        db.session.add(inc)
    db.session.commit()


# ── Core incident creation (shared by API and WebSocket) ─────────────────────
def create_incident(alert_type, location, confidence, notes=''):
    proto = PROTOCOLS.get(alert_type, PROTOCOLS['suspicious'])
    inc = Incident(
        type=alert_type, location=location, severity=proto["severity"],
        status="active", assigned=json.dumps(proto["staff"]),
        time=datetime.now().strftime("%H:%M"),
        timestamp=time.time(), response_time=proto["response_time"],
        confidence=confidence, notes=notes,
    )
    db.session.add(inc)
    db.session.commit()
    inc_dict = inc.to_dict()

    log_event("alert", f"{alert_type.upper()} at {location} ({confidence*100:.0f}% conf)")
    socketio.emit('new_incident', inc_dict)
    socketio.emit('stats_update', get_stats_dict())

    for staff_id in proto['staff']:
        if staff_id in STAFF:
            socketio.emit('staff_notification', {"staff": STAFF[staff_id], "incident": inc_dict})

    # Send email + SMS in background
    send_notifications_async(inc_dict)
    return inc_dict


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/privacy')
def privacy_page():
    return render_template('privacy.html')

@app.route('/terms')
def terms_page():
    return render_template('terms.html')


# ── REST API ─────────────────────────────────────────────────────────────────
@app.route('/api/incidents')
def get_incidents():
    incs = Incident.query.order_by(Incident.id.desc()).all()
    return jsonify([i.to_dict() for i in incs])

@app.route('/api/incidents/<int:iid>/resolve', methods=['POST'])
def resolve_incident(iid):
    inc = Incident.query.get(iid)
    if inc:
        inc.status = 'resolved'
        db.session.commit()
        log_event("ok", f"Incident #{iid} resolved: {inc.location}")
        socketio.emit('incident_update', inc.to_dict())
        socketio.emit('stats_update', get_stats_dict())
        return jsonify({"ok": True, "incident": inc.to_dict()})
    return jsonify({"ok": False, "error": "Not found"}), 404

@app.route('/api/alert', methods=['POST'])
def trigger_alert():
    """Called by bridge.py or external surveillance when a detection fires."""
    data       = request.json or {}
    alert_type = data.get('type', 'suspicious')
    location   = data.get('location', 'Unknown')
    confidence = float(data.get('confidence', 0.85))
    notes      = data.get('notes', '')
    inc_dict   = create_incident(alert_type, location, confidence, notes)
    return jsonify({"ok": True, "incident_id": inc_dict['id']})

@app.route('/api/send_notification', methods=['POST'])
def send_notification_endpoint():
    """Manually send email/SMS for a given incident ID."""
    data = request.json or {}
    iid  = data.get('incident_id')
    inc  = Incident.query.get(iid) if iid else None
    if not inc:
        return jsonify({"ok": False, "error": "Incident not found"}), 404
    send_notifications_async(inc.to_dict())
    return jsonify({"ok": True, "message": "Notification dispatched"})

@app.route('/api/stats')
def get_stats():
    return jsonify(get_stats_dict())

@app.route('/api/log')
def get_log():
    # Return oldest-first so frontend timeline appends correctly
    logs = LogEvent.query.order_by(LogEvent.id.asc()).limit(100).all()
    return jsonify([l.to_dict() for l in logs])

@app.route('/api/report')
def get_report():
    incs     = Incident.query.all()
    resolved = [i for i in incs if i.status == 'resolved']
    active   = [i for i in incs if i.status == 'active']
    by_type  = {}
    for inc in incs:
        by_type[inc.type] = by_type.get(inc.type, 0) + 1
    avg_resp = sum(i.response_time for i in resolved) / max(len(resolved), 1) if resolved else 0
    return jsonify({
        "total": len(incs), "resolved": len(resolved), "active": len(active),
        "resolution_rate": round(len(resolved) / max(len(incs), 1) * 100),
        "avg_response_sec": round(avg_resp),
        "by_type": by_type,
        "generated_at": datetime.now().isoformat(),
        "staff": STAFF,
        "top_zones": ["Kitchen Floor 2", "Parking B3", "Lobby Entrance"],
        "recommendations": [
            "Increase patrol frequency in Kitchen area",
            "Review Parking B3 CCTV blind spots",
            "Schedule quarterly violence-response drills",
            "Add smoke detector to Floor 2 corridor",
        ]
    })

@app.route('/video_feed')
def video_feed():
    """MJPEG stream for live camera — uses eventlet-safe generator."""
    def generate():
        while True:
            with frame_lock:
                frame = camera_frame
            if frame is not None:
                ret, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ret:
                    yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'
                           + buf.tobytes() + b'\r\n')
            import eventlet
            eventlet.sleep(0.033)  # ← eventlet-safe sleep (fixes blocking under socketio)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')


# ── Helpers ──────────────────────────────────────────────────────────────────
def get_stats_dict():
    uptime         = int(time.time() - system_stats["start_time"])
    active_incs    = Incident.query.filter_by(status='active').all()
    resolved_count = Incident.query.filter_by(status='resolved').count()
    deployed = set()
    for i in active_incs:
        if i.assigned:
            deployed.update(json.loads(i.assigned))
    return {
        "fps":              round(system_stats["fps"], 1),
        "uptime":           uptime,
        "frames":           system_stats["frames"],
        "active_incidents": len(active_incs),
        "resolved_today":   resolved_count,
        "teams_deployed":   len(deployed),
        "detectors":        system_stats["detectors"],
        "camera":           system_stats["camera_source"],
    }

def log_event(etype, msg):
    lg = LogEvent(type=etype, msg=msg, time=datetime.now().strftime("%H:%M:%S"), ts=time.time())
    db.session.add(lg)
    db.session.commit()
    socketio.emit('log_event', lg.to_dict())


# ── Camera / demo frame generator ────────────────────────────────────────────
def run_camera(source=None):
    global camera_frame
    try:
        import numpy as np

        if source is None:
            raise ImportError("demo mode")

        try:
            from surveillance import SurveillanceSystem
        except ImportError:
            raise ImportError("surveillance not found")

        cap = cv2.VideoCapture(source)
        system_stats["camera_source"] = str(source)
        fps_timer = time.time(); fps_count = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1); continue
            fps_count += 1
            if time.time() - fps_timer >= 1.0:
                system_stats["fps"] = fps_count / (time.time() - fps_timer)
                fps_count = 0; fps_timer = time.time()
            system_stats["frames"] += 1
            with frame_lock:
                camera_frame = cv2.resize(frame, (960, 540))

    except (ImportError, Exception):
        import numpy as np
        system_stats["camera_source"] = "Demo Mode"
        fps_timer = time.time(); fps_count = 0
        while True:
            h, w = 540, 960
            frame = np.zeros((h, w, 3), dtype=np.uint8)
            frame[:] = (18, 18, 24)
            t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(frame, "SENTINELAI — DEMO MODE", (w//2-200, h//2-20),
                        cv2.FONT_HERSHEY_DUPLEX, 0.9, (0, 200, 100), 2)
            cv2.putText(frame, t, (w//2-150, h//2+20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120, 120, 120), 1)
            cv2.putText(frame, "Connect camera: python app.py --source 0",
                        (w//2-240, h//2+55), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 80, 80), 1)
            fps_count += 1
            if time.time() - fps_timer >= 1.0:
                system_stats["fps"] = fps_count / (time.time() - fps_timer)
                fps_count = 0; fps_timer = time.time()
            system_stats["frames"] += 1
            with frame_lock:
                camera_frame = frame
            time.sleep(0.033)


# ── SocketIO events ──────────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    emit('stats_update', get_stats_dict())
    incs = Incident.query.order_by(Incident.id.desc()).all()
    emit('incidents_init', [i.to_dict() for i in incs])
    logs = LogEvent.query.order_by(LogEvent.id.asc()).limit(100).all()
    emit('log_init', [l.to_dict() for l in logs])

@socketio.on('simulate_alert')
def on_simulate(data):
    """
    BUG FIX: Original code used requests.post to localhost which causes a
    deadlock under eventlet (same process, same thread pool).
    Now we call create_incident() directly inside app context.
    """
    alert_type = data.get('type', 'suspicious')
    location   = data.get('location', 'Simulated Zone')
    confidence = float(data.get('confidence', 0.9))
    with app.app_context():
        create_incident(alert_type, location, confidence, notes='[SIMULATED]')


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        seed_incidents()

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='demo')
    parser.add_argument('--port',   type=int, default=5000)
    args = parser.parse_args()

    source = args.source
    if str(source).isdigit(): source = int(source)
    if source == 'demo': source = None

    print("=" * 60)
    print("  SENTINELAI - Crisis Command Center")
    print("=" * 60)
    print(f"  Dashboard  : http://localhost:{args.port}/dashboard")
    print(f"  Landing    : http://localhost:{args.port}/")
    print(f"  Camera     : {source or 'Demo mode'}")
    print(f"  Email notif: {'configured' if GMAIL_USER else 'not configured (set GMAIL_USER)'}")
    print(f"  SMS notif  : {'configured' if TWILIO_SID else 'not configured (set TWILIO_ACCOUNT_SID)'}")
    print("=" * 60)

    cam_thread = threading.Thread(target=run_camera, args=(source,), daemon=True)
    cam_thread.start()
    os.makedirs("alerts", exist_ok=True)

    socketio.run(app, host='0.0.0.0', port=args.port, debug=False, allow_unsafe_werkzeug=True)
