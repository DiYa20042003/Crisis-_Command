"""
SentinelAI — Smart Surveillance & Crisis Orchestration System
====================================================================
Run locally:  python app.py [--source 0|<url>|demo] [--port 5000]
Deploy:       Render.com / Railway / any Python host

Architecture:
  Camera thread  → smart_surveillance.SurveillanceSystem.process_frame()
                 → alert callback → create_incident()
                 → DB + WebSocket + Email/SMS

FIXED BUGS (original + new):
  - templates/ folder mismatch
  - simulate_alert deadlock under eventlet (calls create_incident directly)
  - video_feed generator blocks under eventlet (eventlet.sleep)
  - /api/log returned newest-first; frontend expects oldest-first
  - Missing CORS headers
  - /api/report had hardcoded zones and response times (now dynamic)
  - /api/alert had no input validation or rate-limiting (now added)
  - No health endpoint for uptime monitoring (now added)
  - Camera thread had no app context for DB access (now wrapped)
  - No evidence path storage on incidents (column added)
"""

from flask import Flask, render_template, Response, jsonify, request, session
from flask_socketio import SocketIO, emit
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import cv2
import threading
import time
import json
import os
import random
import logging
from collections import defaultdict
from datetime import datetime
from sqlalchemy import text as sa_text

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("SentinelAI")

# ── Optional notification libs ────────────────────────────────────────────────
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

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "sentinelai-secret-2024")
CORS(app)

basedir = os.path.abspath(os.path.dirname(__file__))
db_url  = os.environ.get(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(basedir, "sentinel.db"),
)
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"]        = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ── Notification config ───────────────────────────────────────────────────────
GMAIL_USER     = os.environ.get("GMAIL_USER",         "")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD",  "")
ALERT_EMAILS   = os.environ.get("ALERT_EMAILS",        "").split(",")
TWILIO_SID     = os.environ.get("TWILIO_ACCOUNT_SID",  "")
TWILIO_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN",   "")
TWILIO_FROM    = os.environ.get("TWILIO_FROM_NUMBER",  "")
ALERT_PHONES   = os.environ.get("ALERT_PHONES",        "").split(",")

# ── Database Models ───────────────────────────────────────────────────────────
class Incident(db.Model):
    __tablename__ = "incidents"
    id            = db.Column(db.Integer, primary_key=True)
    type          = db.Column(db.String(50),  nullable=False)
    location      = db.Column(db.String(100), nullable=False)
    severity      = db.Column(db.String(20),  nullable=False)
    status        = db.Column(db.String(20),  default="active")
    assigned      = db.Column(db.String(200))   # JSON list of staff keys
    time          = db.Column(db.String(20))
    timestamp     = db.Column(db.Float)
    response_time = db.Column(db.Integer)
    confidence    = db.Column(db.Float)
    notes         = db.Column(db.Text)
    evidence_path = db.Column(db.String(500))   # path to evidence frames folder

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
            "evidence_path": self.evidence_path or "",
            "title":         f"{self.type.capitalize()} — {self.location}",
        }


class LogEvent(db.Model):
    __tablename__ = "log_events"
    id   = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50))
    msg  = db.Column(db.String(500))
    time = db.Column(db.String(20))
    ts   = db.Column(db.Float)

    def to_dict(self):
        return {"type": self.type, "msg": self.msg, "time": self.time, "ts": self.ts}


# ── Global state ──────────────────────────────────────────────────────────────
system_stats = {
    "fps": 0, "uptime": 0, "frames": 0,
    "detectors":     ["fire", "accident", "violence", "suspicious"],
    "camera_source": "Demo Mode",
    "start_time":    time.time(),
}

camera_frame = None
frame_lock   = threading.Lock()

# Simple in-memory rate limiter: ip -> last alert timestamp
_alert_rate_limiter: dict[str, float] = {}
_ALERT_RATE_LIMIT = 2.0  # minimum seconds between /api/alert calls per IP

# ── Staff & protocols ─────────────────────────────────────────────────────────
STAFF = {
    "SEC": {"name": "Riya Sharma",    "role": "Head of Security",  "initials": "RS", "color": "red",    "email": "", "phone": ""},
    "MGR": {"name": "Arjun Patel",    "role": "Duty Manager",      "initials": "AP", "color": "blue",   "email": "", "phone": ""},
    "MED": {"name": "Dr. Priya Nair", "role": "Medical Officer",   "initials": "PN", "color": "green",  "email": "", "phone": ""},
    "GRD": {"name": "Vikram Singh",   "role": "Security Guard",    "initials": "VS", "color": "amber",  "email": "", "phone": ""},
    "ENG": {"name": "Neha Kulkarni",  "role": "Maintenance Eng.",  "initials": "NK", "color": "purple", "email": "", "phone": ""},
}

PROTOCOLS = {
    "fire":       {"severity": "CRITICAL", "color": "red",   "staff": ["SEC", "MGR", "MED", "ENG"], "response_time": 90},
    "violence":   {"severity": "HIGH",     "color": "red",   "staff": ["SEC", "GRD", "MGR"],        "response_time": 120},
    "suspicious": {"severity": "MEDIUM",   "color": "amber", "staff": ["SEC", "GRD"],               "response_time": 180},
    "accident":   {"severity": "HIGH",     "color": "amber", "staff": ["MED", "SEC", "MGR"],        "response_time": 120},
}

VALID_ALERT_TYPES = set(PROTOCOLS.keys())

# ── Notification helpers ──────────────────────────────────────────────────────
def send_email_alert(incident_dict):
    if not EMAIL_AVAILABLE or not GMAIL_USER or not GMAIL_PASSWORD:
        return
    emails = [e.strip() for e in ALERT_EMAILS if e.strip()]
    if not emails:
        return
    try:
        subject = (
            f"CRISIS ALERT: {incident_dict['type'].upper()} "
            f"at {incident_dict['location']}"
        )
        staff_lines = "\n".join(
            f"  • {STAFF[k]['name']} ({STAFF[k]['role']})"
            for k in incident_dict["assigned"]
            if k in STAFF
        )
        body = (
            f"SENTINELAI CRISIS ALERT\n"
            f"=======================\n"
            f"Type      : {incident_dict['type'].upper()}\n"
            f"Severity  : {incident_dict['severity']}\n"
            f"Location  : {incident_dict['location']}\n"
            f"Time      : {incident_dict['time']}\n"
            f"Confidence: {incident_dict['confidence'] * 100:.0f}%\n"
            f"Status    : {incident_dict['status'].upper()}\n\n"
            f"Assigned Personnel:\n{staff_lines}\n\n"
            f"Please log into the Crisis Command Dashboard immediately."
        )
        msg = MIMEMultipart()
        msg["From"]    = GMAIL_USER
        msg["To"]      = ", ".join(emails)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, emails, msg.as_string())
        logger.info(f"Email sent to {emails}")
    except Exception as exc:
        logger.warning(f"Email failed: {exc}")


def send_sms_alert(incident_dict):
    if not TWILIO_AVAILABLE or not TWILIO_SID or not TWILIO_TOKEN:
        return
    phones = [p.strip() for p in ALERT_PHONES if p.strip()]
    if not phones:
        return
    try:
        client   = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        staff_ns = ", ".join(
            STAFF[k]["name"] for k in incident_dict["assigned"] if k in STAFF
        )
        msg = (
            f"CRISIS ALERT\n"
            f"{incident_dict['type'].upper()} – {incident_dict['severity']}\n"
            f"Location: {incident_dict['location']}\n"
            f"Time: {incident_dict['time']}\n"
            f"Staff: {staff_ns}\n"
            f"Login to SentinelAI dashboard immediately."
        )
        for phone in phones:
            client.messages.create(body=msg, from_=TWILIO_FROM, to=phone)
        logger.info(f"SMS sent to {phones}")
    except Exception as exc:
        logger.warning(f"SMS failed: {exc}")


def send_notifications_async(incident_dict):
    t = threading.Thread(
        target=lambda: (send_email_alert(incident_dict), send_sms_alert(incident_dict)),
        daemon=True,
    )
    t.start()


# ── Seed data ─────────────────────────────────────────────────────────────────
def seed_incidents():
    if Incident.query.first():
        return
    seeds = [
        {"type": "suspicious", "location": "Parking Level B3",   "status": "resolved"},
        {"type": "fire",       "location": "Kitchen Floor 2",    "status": "resolved"},
        {"type": "violence",   "location": "Corridor B Floor 1", "status": "resolved"},
        {"type": "accident",   "location": "Lobby Entrance",     "status": "resolved"},
    ]
    for s in seeds:
        proto = PROTOCOLS[s["type"]]
        inc   = Incident(
            type=s["type"], location=s["location"], severity=proto["severity"],
            status=s["status"], assigned=json.dumps(proto["staff"]),
            time=datetime.now().strftime("%H:%M"),
            timestamp=time.time() - random.randint(600, 3600),
            response_time=proto["response_time"],
            notes="", confidence=0.9,
        )
        db.session.add(inc)
    db.session.commit()


# ── Core incident creation ────────────────────────────────────────────────────
def create_incident(alert_type, location, confidence, notes="", evidence_path=""):
    proto   = PROTOCOLS.get(alert_type, PROTOCOLS["suspicious"])
    inc     = Incident(
        type=alert_type, location=location, severity=proto["severity"],
        status="active", assigned=json.dumps(proto["staff"]),
        time=datetime.now().strftime("%H:%M"),
        timestamp=time.time(), response_time=proto["response_time"],
        confidence=confidence, notes=notes,
        evidence_path=evidence_path,
    )
    db.session.add(inc)
    db.session.commit()
    inc_dict = inc.to_dict()

    log_event("alert", f"{alert_type.upper()} at {location} ({confidence * 100:.0f}% conf)")
    socketio.emit("new_incident", inc_dict)
    socketio.emit("stats_update", get_stats_dict())

    for staff_id in proto["staff"]:
        if staff_id in STAFF:
            socketio.emit("staff_notification", {
                "staff": STAFF[staff_id], "incident": inc_dict,
            })

    send_notifications_async(inc_dict)
    return inc_dict


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")

@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")

@app.route("/terms")
def terms_page():
    return render_template("terms.html")


# ── Health endpoint ───────────────────────────────────────────────────────────
@app.route("/health")
def health_check():
    """Uptime monitor / container health probe endpoint."""
    uptime    = int(time.time() - system_stats["start_time"])
    db_ok     = True
    db_status = "ok"
    try:
        db.session.execute(sa_text("SELECT 1"))
    except Exception as exc:
        db_ok     = False
        db_status = str(exc)

    payload = {
        "status":        "ok" if db_ok else "degraded",
        "uptime_seconds": uptime,
        "fps":            round(system_stats["fps"], 1),
        "camera":         system_stats["camera_source"],
        "db":             db_status,
        "detectors":      system_stats["detectors"],
        "timestamp":      datetime.utcnow().isoformat() + "Z",
    }
    return jsonify(payload), 200 if db_ok else 503


# ── REST API ──────────────────────────────────────────────────────────────────
@app.route("/api/incidents")
def get_incidents():
    incs = Incident.query.order_by(Incident.id.desc()).all()
    return jsonify([i.to_dict() for i in incs])


@app.route("/api/incidents/<int:iid>/resolve", methods=["POST"])
def resolve_incident(iid):
    inc = db.session.get(Incident, iid)
    if not inc:
        return jsonify({"ok": False, "error": "Not found"}), 404
    inc.status = "resolved"
    db.session.commit()
    log_event("ok", f"Incident #{iid} resolved: {inc.location}")
    socketio.emit("incident_update", inc.to_dict())
    socketio.emit("stats_update", get_stats_dict())
    return jsonify({"ok": True, "incident": inc.to_dict()})


@app.route("/api/alert", methods=["POST"])
def trigger_alert():
    """
    Called by bridge.py or external detectors when a detection fires.
    Validates input, rate-limits per IP, and creates an incident.
    """
    ip  = request.remote_addr or "unknown"
    now = time.time()

    # Per-IP rate limit
    if now - _alert_rate_limiter.get(ip, 0.0) < _ALERT_RATE_LIMIT:
        return jsonify({"ok": False, "error": "Rate limited"}), 429
    _alert_rate_limiter[ip] = now

    data       = request.get_json(silent=True) or {}
    alert_type = str(data.get("type", "suspicious")).strip().lower()

    if alert_type not in VALID_ALERT_TYPES:
        return jsonify({
            "ok": False,
            "error": f"Invalid type. Must be one of: {', '.join(sorted(VALID_ALERT_TYPES))}",
        }), 400

    # Clamp confidence to [0.0, 1.0]
    try:
        confidence = float(data.get("confidence", 0.85))
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = 0.85

    location      = str(data.get("location",      "Unknown"))[:100]
    notes         = str(data.get("notes",          ""))[:500]
    evidence_path = str(data.get("evidence_path", ""))[:500]

    inc_dict = create_incident(alert_type, location, confidence, notes, evidence_path)
    return jsonify({"ok": True, "incident_id": inc_dict["id"]})


@app.route("/api/send_notification", methods=["POST"])
def send_notification_endpoint():
    data = request.get_json(silent=True) or {}
    iid  = data.get("incident_id")
    inc  = db.session.get(Incident, iid) if iid else None
    if not inc:
        return jsonify({"ok": False, "error": "Incident not found"}), 404
    send_notifications_async(inc.to_dict())
    return jsonify({"ok": True, "message": "Notification dispatched"})


@app.route("/api/stats")
def get_stats():
    return jsonify(get_stats_dict())


@app.route("/api/log")
def get_log():
    logs = LogEvent.query.order_by(LogEvent.id.asc()).limit(100).all()
    return jsonify([l.to_dict() for l in logs])


@app.route("/api/evidence/<int:iid>")
def get_evidence(iid):
    """Return list of JPEG filenames for an incident's evidence folder."""
    inc = db.session.get(Incident, iid)
    if not inc or not inc.evidence_path:
        return jsonify({"ok": False, "frames": [], "error": "No evidence"}), 404

    path = inc.evidence_path
    if not os.path.isdir(path):
        return jsonify({"ok": False, "frames": [], "error": "Evidence dir not found"}), 404

    frames = sorted(
        f for f in os.listdir(path) if f.endswith(".jpg")
    )
    meta_path = os.path.join(path, "meta.json")
    meta      = {}
    if os.path.isfile(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)

    return jsonify({"ok": True, "frames": frames, "path": path, "meta": meta})


@app.route("/api/report")
def get_report():
    """Fully dynamic incident report — no hardcoded numbers."""
    incs     = Incident.query.all()
    resolved = [i for i in incs if i.status == "resolved"]
    active   = [i for i in incs if i.status == "active"]

    # ── Type breakdown ────────────────────────────────────────────────
    by_type: dict[str, int] = defaultdict(int)
    for inc in incs:
        by_type[inc.type] += 1

    # ── Average response time per type (from actual data) ─────────────
    resp_by_type: dict[str, list] = defaultdict(list)
    for inc in resolved:
        resp_by_type[inc.type].append(inc.response_time or 0)

    avg_resp_by_type = {
        t: round(sum(times) / len(times))
        for t, times in resp_by_type.items()
        if times
    }

    overall_avg = (
        round(sum(i.response_time or 0 for i in resolved) / len(resolved))
        if resolved else 0
    )

    # ── Top zones by incident count ───────────────────────────────────
    zone_counts: dict[str, int] = defaultdict(int)
    for inc in incs:
        zone_counts[inc.location] += 1

    top_zones = [
        loc for loc, _ in sorted(zone_counts.items(), key=lambda x: -x[1])
    ][:5]

    # ── Dynamic recommendations ───────────────────────────────────────
    recs = _generate_recommendations(incs, by_type, top_zones)

    # ── Average confidence ────────────────────────────────────────────
    confidences = [i.confidence for i in incs if i.confidence is not None]
    avg_conf    = round(sum(confidences) / len(confidences), 2) if confidences else 0.0

    return jsonify({
        "total":             len(incs),
        "resolved":          len(resolved),
        "active":            len(active),
        "resolution_rate":   round(len(resolved) / max(len(incs), 1) * 100),
        "avg_response_sec":  overall_avg,
        "avg_response_by_type": avg_resp_by_type,
        "avg_confidence":    avg_conf,
        "by_type":           dict(by_type),
        "top_zones":         top_zones,
        "recommendations":   recs,
        "staff":             STAFF,
        "generated_at":      datetime.now().isoformat(),
    })


def _generate_recommendations(incs, by_type, top_zones):
    """Build context-specific recommendations from actual incident data."""
    recs = []

    if by_type.get("fire", 0) > 0:
        zone = top_zones[0] if top_zones else "high-activity zones"
        recs.append(
            f"Install additional smoke/heat detectors in {zone} — "
            f"{by_type['fire']} fire incident(s) recorded."
        )

    if by_type.get("violence", 0) > 0:
        recs.append(
            f"Schedule monthly violence-response drills for security staff — "
            f"{by_type['violence']} incident(s) on record."
        )

    if by_type.get("suspicious", 0) > 0:
        recs.append(
            f"Review CCTV blind spots and increase patrol frequency — "
            f"{by_type['suspicious']} suspicious-activity event(s) detected."
        )

    if by_type.get("accident", 0) > 0:
        recs.append(
            f"Conduct safety audit of floor surfaces and lighting — "
            f"{by_type['accident']} accident(s) reported."
        )

    if len(top_zones) >= 2:
        recs.append(
            f"Prioritise camera upgrades in top incident zones: "
            f"{', '.join(top_zones[:3])}."
        )

    if not recs:
        recs.append("No incidents on record — system is operating normally.")

    return recs


@app.route("/video_feed")
def video_feed():
    """MJPEG stream — eventlet-safe generator."""
    def generate():
        while True:
            with frame_lock:
                frame = camera_frame
            if frame is not None:
                ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ret:
                    yield (
                        b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                        + buf.tobytes()
                        + b"\r\n"
                    )
            import eventlet
            eventlet.sleep(0.033)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_stats_dict():
    uptime      = int(time.time() - system_stats["start_time"])
    active_incs = Incident.query.filter_by(status="active").all()
    resolved_ct = Incident.query.filter_by(status="resolved").count()
    deployed    = set()
    for i in active_incs:
        if i.assigned:
            deployed.update(json.loads(i.assigned))
    return {
        "fps":              round(system_stats["fps"], 1),
        "uptime":           uptime,
        "frames":           system_stats["frames"],
        "active_incidents": len(active_incs),
        "resolved_today":   resolved_ct,
        "teams_deployed":   len(deployed),
        "detectors":        system_stats["detectors"],
        "camera":           system_stats["camera_source"],
    }


def log_event(etype, msg):
    lg = LogEvent(
        type=etype, msg=msg,
        time=datetime.now().strftime("%H:%M:%S"),
        ts=time.time(),
    )
    db.session.add(lg)
    db.session.commit()
    socketio.emit("log_event", lg.to_dict())


# ── Camera / demo-frame generator ────────────────────────────────────────────
def run_camera(source=None):
    """
    Background thread: captures frames and feeds them to SurveillanceSystem.
    Falls back to a synthetic demo feed if no camera or module is available.

    The entire function runs inside app.app_context() so that create_incident()
    can use db.session from the alert callback.
    """
    global camera_frame
    import numpy as np

    with app.app_context():
        if source is not None:
            _run_real_camera(source)
        else:
            _run_demo_feed()


def _run_real_camera(source):
    """Open a real camera, run SurveillanceSystem, stream frames to dashboard."""
    global camera_frame
    import numpy as np

    try:
        from smart_surveillance import SurveillanceSystem
    except ImportError:
        logger.warning("smart_surveillance not found — falling back to demo mode.")
        _run_demo_feed()
        return

    def on_alert(alert_type, confidence, location, evidence_path=""):
        try:
            create_incident(
                alert_type, location, confidence,
                notes=f"AI-detected via SurveillanceSystem",
                evidence_path=evidence_path,
            )
        except Exception as exc:
            logger.error(f"create_incident failed: {exc}")

    surveillance = SurveillanceSystem(alert_manager=on_alert)
    cap          = cv2.VideoCapture(source)

    if not cap.isOpened():
        logger.error(f"Cannot open camera source: {source}. Falling back to demo.")
        _run_demo_feed()
        return

    system_stats["camera_source"] = str(source)
    fps_t, fps_n = time.time(), 0
    logger.info(f"Camera opened on source: {source}")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                logger.warning("Frame read failed — retrying in 100 ms.")
                time.sleep(0.1)
                continue

            frame = cv2.resize(frame, (960, 540))

            # AI detection — all detectors run on every frame
            surveillance.process_frame(frame)

            # Sync FPS stats
            fps_n += 1
            elapsed = time.time() - fps_t
            if elapsed >= 1.0:
                system_stats["fps"]    = fps_n / elapsed
                fps_n                  = 0
                fps_t                  = time.time()
            system_stats["frames"] += 1

            with frame_lock:
                camera_frame = frame

    finally:
        cap.release()


def _run_demo_feed():
    """Generate synthetic frames when no real camera is available."""
    global camera_frame
    import numpy as np

    system_stats["camera_source"] = "Demo Mode"
    fps_t, fps_n = time.time(), 0
    logger.info("Running in demo (synthetic frame) mode.")

    while True:
        h, w  = 540, 960
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:] = (18, 18, 24)

        t = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        cv2.putText(frame, "SENTINELAI  —  DEMO MODE",
                    (w // 2 - 200, h // 2 - 20),
                    cv2.FONT_HERSHEY_DUPLEX, 0.9, (0, 200, 100), 2)
        cv2.putText(frame, t,
                    (w // 2 - 150, h // 2 + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120, 120, 120), 1)
        cv2.putText(frame, "Connect camera:  python app.py --source 0",
                    (w // 2 - 240, h // 2 + 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 80, 80), 1)

        fps_n += 1
        elapsed = time.time() - fps_t
        if elapsed >= 1.0:
            system_stats["fps"] = fps_n / elapsed
            fps_n = 0
            fps_t = time.time()
        system_stats["frames"] += 1

        with frame_lock:
            camera_frame = frame
        time.sleep(0.033)


# ── SocketIO events ───────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    emit("stats_update", get_stats_dict())
    incs = Incident.query.order_by(Incident.id.desc()).all()
    emit("incidents_init", [i.to_dict() for i in incs])
    logs = LogEvent.query.order_by(LogEvent.id.asc()).limit(100).all()
    emit("log_init", [l.to_dict() for l in logs])


@socketio.on("simulate_alert")
def on_simulate(data):
    """
    Simulate a detection from the dashboard test buttons.
    Calls create_incident directly — no self-HTTP-call (that deadlocks eventlet).
    """
    alert_type = data.get("type", "suspicious")
    if alert_type not in VALID_ALERT_TYPES:
        return
    location   = str(data.get("location",   "Simulated Zone"))[:100]
    confidence = float(data.get("confidence", 0.9))
    confidence = max(0.0, min(1.0, confidence))

    with app.app_context():
        create_incident(alert_type, location, confidence, notes="[SIMULATED]")


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        seed_incidents()

    import argparse
    parser = argparse.ArgumentParser(description="SentinelAI Crisis Command Center")
    parser.add_argument("--source", default="demo",
                        help="Camera source: 0 (webcam), URL, or demo")
    parser.add_argument("--port",   type=int, default=5000)
    args = parser.parse_args()

    source = args.source
    if str(source).isdigit():
        source = int(source)
    elif source == "demo":
        source = None

    print("=" * 62)
    print("  SENTINELAI — Crisis Command Center")
    print("=" * 62)
    print(f"  Dashboard  : http://localhost:{args.port}/dashboard")
    print(f"  Landing    : http://localhost:{args.port}/")
    print(f"  Health     : http://localhost:{args.port}/health")
    print(f"  Camera     : {source or 'Demo mode'}")
    print(f"  Detectors  : Fire / Violence / Accident / Suspicious")
    print(f"  Email      : {'configured' if GMAIL_USER else 'not set  (GMAIL_USER)'}")
    print(f"  SMS        : {'configured' if TWILIO_SID else 'not set  (TWILIO_ACCOUNT_SID)'}")
    print("=" * 62)

    os.makedirs("alerts",   exist_ok=True)
    os.makedirs("evidence", exist_ok=True)

    cam_thread = threading.Thread(
        target=run_camera, args=(source,), daemon=True
    )
    cam_thread.start()

    socketio.run(
        app, host="0.0.0.0", port=args.port,
        debug=False, allow_unsafe_werkzeug=True,
    )
