"""
SentinelAI — Smart Surveillance & Crisis Orchestration System
====================================================================
Run:  python app.py
Open: http://localhost:5000
"""

from flask import Flask, render_template, Response, jsonify, request
from flask_socketio import SocketIO, emit
from flask_sqlalchemy import SQLAlchemy
import cv2, threading, time, json, os, random
from datetime import datetime

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sentinelai-secret-2024'

# --- Database Config ---
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'sentinel.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

socketio = SocketIO(app, cors_allowed_origins="*")

# ─── Database Models ──────────────────────────────────────────────────────────
class Incident(db.Model):
    __tablename__ = 'incidents'
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False)
    location = db.Column(db.String(100), nullable=False)
    severity = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), default='active')
    assigned = db.Column(db.String(200)) # Stored as JSON string
    time = db.Column(db.String(20))
    timestamp = db.Column(db.Float)
    response_time = db.Column(db.Integer)
    confidence = db.Column(db.Float)
    notes = db.Column(db.Text)

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "location": self.location,
            "severity": self.severity,
            "status": self.status,
            "assigned": json.loads(self.assigned) if self.assigned else [],
            "time": self.time,
            "timestamp": self.timestamp,
            "response_time": self.response_time,
            "confidence": self.confidence,
            "notes": self.notes or ""
        }

class LogEvent(db.Model):
    __tablename__ = 'log_events'
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50))
    msg = db.Column(db.String(200))
    time = db.Column(db.String(20))
    ts = db.Column(db.Float)

    def to_dict(self):
        return {
            "type": self.type,
            "msg": self.msg,
            "time": self.time,
            "ts": self.ts
        }


# ─── State ────────────────────────────────────────────────────────────────────
system_stats = {
    "fps": 0, "uptime": 0, "frames": 0,
    "detectors": ["fire","accident","violence","suspicious"],
    "camera_source": "Not connected", "start_time": time.time()
}
camera_frame = None
frame_lock   = threading.Lock()
surveillance_thread = None

STAFF = {
    "SEC": {"name": "Riya Sharma",   "role": "Head of Security",  "initials": "RS", "color": "red"},
    "MGR": {"name": "Arjun Patel",   "role": "Duty Manager",      "initials": "AP", "color": "blue"},
    "MED": {"name": "Dr. Priya Nair","role": "Medical Officer",   "initials": "PN", "color": "green"},
    "GRD": {"name": "Vikram Singh",  "role": "Security Guard",    "initials": "VS", "color": "amber"},
    "ENG": {"name": "Neha Kulkarni", "role": "Maintenance Eng.",  "initials": "NK", "color": "purple"},
}

PROTOCOLS = {
    "fire":       {"severity":"CRITICAL","color":"red",   "staff":["SEC","MGR","MED","ENG"],"response_time":90},
    "violence":   {"severity":"HIGH",    "color":"red",   "staff":["SEC","GRD","MGR"],      "response_time":120},
    "suspicious": {"severity":"MEDIUM",  "color":"amber", "staff":["SEC","GRD"],            "response_time":180},
    "accident":   {"severity":"HIGH",    "color":"amber", "staff":["MED","SEC","MGR"],      "response_time":120},
}

# ─── Demo incident seeds ───────────────────────────────────────────────────────
def seed_incidents():
    if Incident.query.first():
        return # DB already has data

    seeds = [
        {"type":"suspicious","location":"Parking Level B3","status":"resolved"},
        {"type":"fire","location":"Kitchen Floor 2","status":"resolved"},
        {"type":"violence","location":"Corridor B Floor 1","status":"resolved"},
        {"type":"accident","location":"Lobby Entrance","status":"resolved"},
    ]
    for s in seeds:
        proto = PROTOCOLS[s["type"]]
        inc = Incident(
            type=s["type"],
            location=s["location"],
            severity=proto["severity"],
            status=s["status"],
            assigned=json.dumps(proto["staff"]),
            time=datetime.now().strftime("%H:%M"),
            timestamp=time.time() - random.randint(600,3600),
            response_time=proto["response_time"],
            notes="",
            confidence=0.9
        )
        db.session.add(inc)
    db.session.commit()

# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

# ─── API ──────────────────────────────────────────────────────────────────────
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
    return jsonify({"ok": False}), 404

@app.route('/api/alert', methods=['POST'])
def trigger_alert():
    """Called by the Python surveillance system when a detection fires."""
    data = request.json or {}
    alert_type = data.get('type', 'suspicious')
    location   = data.get('location', 'Unknown')
    confidence = data.get('confidence', 0.85)

    proto = PROTOCOLS.get(alert_type, PROTOCOLS['suspicious'])
    
    inc = Incident(
        type=alert_type,
        location=location,
        severity=proto["severity"],
        status="active",
        assigned=json.dumps(proto["staff"]),
        time=datetime.now().strftime("%H:%M"),
        timestamp=time.time(),
        response_time=proto["response_time"],
        confidence=confidence,
        notes=data.get('notes', '')
    )
    db.session.add(inc)
    db.session.commit()

    inc_dict = inc.to_dict()
    log_event("alert", f"{alert_type.upper()} at {location} ({confidence:.0%} conf)")
    socketio.emit('new_incident', inc_dict)
    socketio.emit('stats_update', get_stats_dict())
    
    # Auto-notify assigned staff
    for staff_id in proto['staff']:
        s = STAFF[staff_id]
        socketio.emit('staff_notification', {
            "staff": s, "incident": inc_dict
        })
    return jsonify({"ok": True, "incident_id": inc.id})

@app.route('/api/stats')
def get_stats():
    return jsonify(get_stats_dict())

@app.route('/api/log')
def get_log():
    logs = LogEvent.query.order_by(LogEvent.id.desc()).limit(100).all()
    # Reverse so it's oldest first if needed, or youngest. In frontend it usually expects list. 
    # Current deque is appended to. Let's return as ordered by ID desc
    return jsonify([l.to_dict() for l in logs])

@app.route('/api/report')
def get_report():
    incs = Incident.query.all()
    resolved = [i for i in incs if i.status == 'resolved']
    active   = [i for i in incs if i.status == 'active']
    by_type  = {}
    for inc in incs:
        by_type[inc.type] = by_type.get(inc.type, 0) + 1
    avg_resp = sum(i.response_time for i in resolved) / max(len(resolved), 1) if resolved else 0
    return jsonify({
        "total": len(incs),
        "resolved": len(resolved),
        "active": len(active),
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
    """MJPEG stream for live camera view."""
    def generate():
        while True:
            with frame_lock:
                frame = camera_frame
            if frame is not None:
                ret, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ret:
                    yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'
            time.sleep(0.033)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

# ─── Helpers ──────────────────────────────────────────────────────────────────
def get_stats_dict():
    uptime = int(time.time() - system_stats["start_time"])
    active_count = Incident.query.filter_by(status='active').count()
    resolved_count = Incident.query.filter_by(status='resolved').count()
    
    # Teams deployed based on active incidents
    active_incs = Incident.query.filter_by(status='active').all()
    deployed = set()
    for i in active_incs:
        if i.assigned:
            deployed.update(json.loads(i.assigned))
            
    return {
        "fps":       round(system_stats["fps"], 1),
        "uptime":    uptime,
        "frames":    system_stats["frames"],
        "active_incidents": active_count,
        "resolved_today":   resolved_count,
        "teams_deployed":   len(deployed),
        "detectors": system_stats["detectors"],
        "camera":    system_stats["camera_source"],
    }

def log_event(etype, msg):
    lg = LogEvent(
        type=etype,
        msg=msg,
        time=datetime.now().strftime("%H:%M:%S"),
        ts=time.time()
    )
    db.session.add(lg)
    db.session.commit()
    socketio.emit('log_event', lg.to_dict())

# ─── Camera thread ────────────────────────────────────────────────────────────
def run_camera(source=0):
    """Runs surveillance in background. Replace with full detector pipeline."""
    global camera_frame
    try:
        from surveillance import SurveillanceSystem
        sys_obj = SurveillanceSystem(
            source=source,
            alert_dir="alerts",
            confidence=0.5,
        )
        # Hook alerts into Flask API
        original_handle = sys_obj.alert_manager.handle
        def hooked_handle(alert, frame):
            original_handle(alert, frame)
            import requests
            try:
                requests.post('http://localhost:5000/api/alert', json={
                    "type": alert["type"],
                    "location": "CCTV Camera 1",
                    "confidence": alert.get("confidence", 0.8),
                }, timeout=1)
            except: pass
        sys_obj.alert_manager.handle = hooked_handle

        # Stream frames
        cap = cv2.VideoCapture(source)
        fps_timer = time.time()
        fps_count = 0
        system_stats["camera_source"] = str(source)
        while True:
            ret, frame = cap.read()
            if not ret: time.sleep(0.1); continue
            fps_count += 1
            if time.time() - fps_timer >= 1.0:
                system_stats["fps"] = fps_count / (time.time() - fps_timer)
                fps_count = 0; fps_timer = time.time()
            system_stats["frames"] += 1
            with frame_lock:
                camera_frame = cv2.resize(frame, (960, 540))
        cap.release()
    except ImportError:
        # Demo mode: generate placeholder frames
        system_stats["camera_source"] = "Demo Mode"
        fps_timer = time.time(); fps_count = 0
        while True:
            h, w = 540, 960
            frame = __import__('numpy').zeros((h, w, 3), dtype=__import__('numpy').uint8)
            frame[:] = (18, 18, 24)
            t = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
            cv2.putText(frame, "SENTINELAI — DEMO MODE", (w//2-200, h//2-20),
                        cv2.FONT_HERSHEY_DUPLEX, 0.9, (0,200,100), 2)
            cv2.putText(frame, t, (w//2-150, h//2+20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (120,120,120), 1)
            cv2.putText(frame, "Connect camera: python app.py --source 0", (w//2-230, h//2+50),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80,80,80), 1)
            fps_count += 1
            if time.time() - fps_timer >= 1.0:
                system_stats["fps"] = fps_count / (time.time() - fps_timer)
                fps_count = 0; fps_timer = time.time()
            system_stats["frames"] += 1
            with frame_lock:
                camera_frame = frame
            time.sleep(0.033)

# ─── SocketIO events ──────────────────────────────────────────────────────────
@socketio.on('connect')
def on_connect():
    emit('stats_update', get_stats_dict())
    
    incs = Incident.query.order_by(Incident.id.desc()).all()
    emit('incidents_init', [i.to_dict() for i in incs])
    
    logs = LogEvent.query.order_by(LogEvent.id.asc()).limit(100).all()
    emit('log_init', [l.to_dict() for l in logs])

@socketio.on('simulate_alert')
def on_simulate(data):
    """Browser can trigger simulated alert via websocket."""
    import requests
    try:
        requests.post('http://localhost:5000/api/alert', json=data, timeout=2)
    except: pass

# ─── Main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    # Initialize Database
    with app.app_context():
        db.create_all()
        seed_incidents()

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='demo', help='Camera source (0, URL, or demo)')
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()

    source = args.source
    if source.isdigit(): source = int(source)
    if source == 'demo': source = None

    print("="*60)
    print("  SENTINELAI — Smart Hotel Surveillance System")
    print("="*60)
    print(f"  Dashboard : http://localhost:{args.port}/dashboard")
    print(f"  Camera    : {source or 'Demo mode'}")
    print("="*60)

    cam_thread = threading.Thread(target=run_camera, args=(source if source else 0,), daemon=True)
    cam_thread.start()

    os.makedirs("alerts", exist_ok=True)
    socketio.run(app, host='0.0.0.0', port=args.port, debug=False, allow_unsafe_werkzeug=True)
