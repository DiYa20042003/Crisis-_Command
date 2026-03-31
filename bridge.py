"""
bridge.py — Connects your surveillance system to the SentinelAI dashboard
==========================================================================
Run this INSTEAD of main.py when using the full dashboard.

Usage:
    python bridge.py --source 0                              # Webcam
    python bridge.py --source "http://192.168.1.15:4747/video"  # DroidCam
    python bridge.py --source demo                          # Demo mode

The Flask dashboard must already be running:
    python app.py
"""

import sys, os, time, requests, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'smart_surveillance'))

DASHBOARD_URL = "http://localhost:5000"

LOCATION_MAP = {
    "fire":       "Kitchen / Detected Zone",
    "accident":   "Parking / Entrance Area",
    "violence":   "Corridor / Public Area",
    "suspicious": "CCTV Zone",
}

def post_alert(alert_type, confidence=0.85, location=None):
    """Send a detection alert to the SentinelAI dashboard."""
    payload = {
        "type": alert_type,
        "location": location or LOCATION_MAP.get(alert_type, "Camera Zone"),
        "confidence": confidence,
    }
    try:
        r = requests.post(f"{DASHBOARD_URL}/api/alert", json=payload, timeout=2)
        print(f"[BRIDGE] Alert sent: {alert_type} → {r.status_code}")
    except requests.exceptions.ConnectionError:
        print(f"[BRIDGE] Dashboard not reachable. Is app.py running at {DASHBOARD_URL}?")

def run_surveillance(source):
    """Run the AI surveillance system and forward alerts to dashboard."""
    try:
        from surveillance import SurveillanceSystem

        system = SurveillanceSystem(
            source=source,
            features={"fire": True, "accident": True, "violence": True, "suspicious": True},
            alert_dir="alerts",
            confidence=0.5,
        )

        # Patch the alert handler to also POST to dashboard
        original_handle = system.alert_manager.handle

        def bridge_handle(alert, frame):
            original_handle(alert, frame)
            post_alert(
                alert_type=alert["type"],
                confidence=alert.get("confidence", 0.8),
                location=LOCATION_MAP.get(alert["type"], "Camera Zone"),
            )

        system.alert_manager.handle = bridge_handle

        print(f"[BRIDGE] Surveillance running → posting alerts to {DASHBOARD_URL}")
        system.run()

    except ImportError:
        print("[BRIDGE] smart_surveillance not found. Running in demo alert mode.")
        _demo_alerts()

def _demo_alerts():
    """Send periodic demo alerts to the dashboard for testing."""
    import random
    types = ["fire", "violence", "suspicious", "accident"]
    while True:
        time.sleep(random.randint(15, 40))
        t = random.choice(types)
        post_alert(t, confidence=round(0.7 + random.random() * 0.25, 2))

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="demo")
    args = parser.parse_args()

    source = args.source
    if source.isdigit():
        source = int(source)
    elif source == "demo":
        source = None

    print("=" * 55)
    print("  SentinelAI — Surveillance Bridge")
    print("=" * 55)
    print(f"  Source    : {source or 'Demo mode'}")
    print(f"  Dashboard : {DASHBOARD_URL}/dashboard")
    print("=" * 55)

    if source is not None:
        run_surveillance(source)
    else:
        print("[BRIDGE] Demo mode — sending random alerts every 15–40s")
        _demo_alerts()
