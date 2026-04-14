"""
bridge.py — Connects external surveillance to the SentinelAI dashboard
=======================================================================

Run the Flask dashboard first:
    python app.py

Then in a second terminal:
    python bridge.py --source 0          # webcam
    python bridge.py --source "http://192.168.1.15:4747/video"  # DroidCam
    python bridge.py --source demo       # random demo alerts

FIX: Added retry logic + proper content-type header for POST requests
"""

import sys, os, time, requests, random, argparse

DASHBOARD_URL = os.environ.get('DASHBOARD_URL', 'http://localhost:5000')

LOCATION_MAP = {
    "fire":       "Kitchen / Detected Zone",
    "accident":   "Parking / Entrance Area",
    "violence":   "Corridor / Public Area",
    "suspicious": "CCTV Zone",
}

def post_alert(alert_type, confidence=0.85, location=None, retries=3):
    payload = {
        "type":       alert_type,
        "location":   location or LOCATION_MAP.get(alert_type, "Camera Zone"),
        "confidence": round(confidence, 3),
    }
    for attempt in range(retries):
        try:
            r = requests.post(
                f"{DASHBOARD_URL}/api/alert",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=3
            )
            print(f"[BRIDGE] Alert sent: {alert_type} → HTTP {r.status_code}")
            return True
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"[BRIDGE] Dashboard not reachable at {DASHBOARD_URL}. Is app.py running?")
    return False


def run_surveillance(source):
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'smart_surveillance'))
        from surveillance import SurveillanceSystem

        system = SurveillanceSystem(
            source=source,
            features={"fire": True, "accident": True, "violence": True, "suspicious": True},
            alert_dir="alerts",
            confidence=0.5,
        )

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
        print("[BRIDGE] smart_surveillance not found. Falling back to demo alert mode.")
        _demo_alerts()


def _demo_alerts():
    """Send periodic demo alerts to the dashboard for testing."""
    types = ["fire", "violence", "suspicious", "accident"]
    print(f"[BRIDGE] Demo mode — sending random alerts every 15–40s to {DASHBOARD_URL}")
    # Wait for dashboard to be ready
    print("[BRIDGE] Waiting for dashboard to start...")
    for _ in range(10):
        try:
            requests.get(f"{DASHBOARD_URL}/api/stats", timeout=2)
            print("[BRIDGE] Dashboard is up! Starting demo alerts.")
            break
        except Exception:
            time.sleep(2)

    while True:
        delay = random.randint(15, 40)
        print(f"[BRIDGE] Next alert in {delay}s...")
        time.sleep(delay)
        t = random.choice(types)
        post_alert(t, confidence=round(0.70 + random.random() * 0.25, 2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SentinelAI Surveillance Bridge")
    parser.add_argument("--source", default="demo", help="Camera source: 0, URL, or demo")
    parser.add_argument("--dashboard", default=None, help="Override dashboard URL")
    args = parser.parse_args()

    if args.dashboard:
        DASHBOARD_URL = args.dashboard

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
        _demo_alerts()
