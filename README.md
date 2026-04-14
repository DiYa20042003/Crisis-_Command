# SentinelAI — Crisis Command Center

> Real-time AI crisis orchestration for hospitality environments.  
> Detect → Decide → Assign → Coordinate → Track → Report

## Live Demo Flow

```
http://localhost:5000            → Landing page
http://localhost:5000/dashboard  → Crisis Command Dashboard
http://localhost:5000/video_feed → Raw MJPEG stream
http://localhost:5000/api/stats  → System stats JSON
http://localhost:5000/api/incidents → All incidents JSON
http://localhost:5000/api/report    → Incident report JSON
```

---

## Quick Start (Local)

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy env template and fill in your values
cp .env.example .env

# 3. Run in demo mode (no camera needed)
python app.py

# Open: http://localhost:5000/dashboard
```

### With camera (webcam or DroidCam)

```bash
# Terminal 1 — Dashboard
python app.py

# Terminal 2 — Surveillance bridge
python bridge.py --source 0          # webcam
python bridge.py --source "http://192.168.1.15:4747/video"  # DroidCam
```

---

## Email + SMS Notifications

Every new incident automatically triggers:
1. **Gmail email** to all `ALERT_EMAILS` with incident details + assigned staff
2. **SMS via Twilio** to all `ALERT_PHONES` with a concise alert summary (optional)

### Gmail Setup

1. Enable 2FA on your Google Account
2. Go to: Google Account → Security → App Passwords
3. Create a new App Password for "Mail"
4. Add to `.env`: `GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx`

### Twilio SMS Setup (optional)

1. Sign up at [twilio.com](https://twilio.com)
2. Get your Account SID, Auth Token, and a Twilio phone number
3. Add to `.env` (see `.env.example`)

---

## Deploy on Render.com

1. Push this repository to GitHub
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` and configures everything
5. Add env vars in Render dashboard (Settings → Environment)

Start command (auto-set by `render.yaml`):

```
gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:$PORT app:app
```

---

## Project Structure

```
.
├── app.py                  ← Flask backend (API + WebSocket + DB + notifications)
├── bridge.py               ← Surveillance-to-dashboard connector
├── requirements.txt        ← Python dependencies
├── Procfile                ← Heroku/Railway/Render compatible start command
├── render.yaml             ← Render.com deployment config
├── .env.example            ← Environment variables template
└── templates/
    ├── index.html          ← Landing page
    ├── dashboard.html      ← Live crisis command center
    ├── login.html
    ├── privacy.html
    └── terms.html
```

