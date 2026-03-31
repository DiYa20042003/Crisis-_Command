# SentinelAI — Smart Hotel Surveillance & Crisis Orchestration

> **Hackathon Project** · No Raspberry Pi · Phone Camera · Full Stack AI

A real-time AI surveillance system that **detects, decides, assigns, coordinates, tracks and reports** hotel security incidents — entirely in software, zero hardware cost beyond a laptop and phone.

---

## Live URLs (after running)

| URL | Page |
|-----|------|
| `http://localhost:5000` | Landing Page |
| `http://localhost:5000/dashboard` | Live Crisis Dashboard |
| `http://localhost:5000/video_feed` | Raw MJPEG stream |
| `http://localhost:5000/api/stats` | System stats JSON |
| `http://localhost:5000/api/incidents` | All incidents JSON |
| `http://localhost:5000/api/report` | Incident report JSON |

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the dashboard (demo mode — no camera needed)
```bash
python app.py
```
Open `http://localhost:5000`

### 3. Run with your phone camera (DroidCam)
```bash
# Terminal 1 — Dashboard
python app.py

# Terminal 2 — Surveillance + Bridge
python bridge.py --source "http://192.168.1.15:4747/video"
```

### 4. Run with webcam
```bash
python app.py
python bridge.py --source 0
```

---

## Project Structure

```
sentinelai/
├── app.py              ← Flask backend (API + WebSockets + video stream)
├── bridge.py           ← Connects surveillance detectors to dashboard
├── requirements.txt
├── templates/
│   ├── index.html      ← Landing page (hackathon presentation)
│   └── dashboard.html  ← Live crisis command center
└── alerts/             ← Auto-created, stores alert snapshots
```

**AI Detection modules** live in the `smart_surveillance/` folder (the existing project):
```
smart_surveillance/
├── detectors/
│   ├── fire_detector.py
│   ├── accident_detector.py
│   ├── violence_detector.py
│   └── suspicious_detector.py
└── ...
```

---

## The 6-Stage Workflow

```
DETECT → DECIDE → ASSIGN → COORDINATE → TRACK → REPORT
```

| Stage | What happens |
|-------|-------------|
| **Detect** | AI models analyze live video in real time |
| **Decide** | Orchestration layer classifies severity: CRITICAL / HIGH / MEDIUM |
| **Assign** | Correct staff auto-dispatched: Security, Medical, Manager, etc. |
| **Coordinate** | Centralized dashboard keeps all teams aligned |
| **Track** | Every action logged with timestamps + zone heatmap |
| **Report** | Auto-generated report sent to management |

---

## API Reference

### POST `/api/alert`
Trigger an incident from external system (surveillance bridge).
```json
{
  "type": "fire",
  "location": "Kitchen Floor 2",
  "confidence": 0.92
}
```

### POST `/api/incidents/<id>/resolve`
Mark incident as resolved.

### GET `/api/report`
Get full incident report with stats and recommendations.

### WebSocket Events
| Event | Direction | Description |
|-------|-----------|-------------|
| `new_incident` | Server→Client | New alert fired |
| `incident_update` | Server→Client | Incident status changed |
| `stats_update` | Server→Client | System metrics update |
| `log_event` | Server→Client | Activity log entry |
| `simulate_alert` | Client→Server | Trigger simulated alert |

---

## Detection Modules

| Module | Method | Severity |
|--------|--------|----------|
| Fire | HSV filtering + brightness + flicker + edge analysis | CRITICAL |
| Accident | YOLOv8 IoU overlap logic | HIGH |
| Violence | Optical flow burst analysis | HIGH |
| Suspicious | YOLO crowd count + loitering + intrusion zones | MEDIUM |

---

## Connecting Your Phone (DroidCam)

1. Install **DroidCam** on your phone (Android/iOS — free)
2. Open app → note the IP (e.g. `192.168.1.15:4747`)
3. Both phone and laptop must be on **same WiFi**
4. Run: `python bridge.py --source "http://192.168.1.15:4747/video"`

---

## Tech Stack

- **Backend**: Python, Flask, Flask-SocketIO, eventlet
- **AI/CV**: OpenCV, NumPy, YOLOv8 (ultralytics)
- **Frontend**: Vanilla HTML/CSS/JS, Socket.IO client
- **Streaming**: MJPEG over HTTP
- **Detection**: HSV filtering, Farneback optical flow, MOG2 background subtraction, YOLOv8

---

*Built for Smart Hotel Safety Hackathon · Zero hardware cost · 100% software*
