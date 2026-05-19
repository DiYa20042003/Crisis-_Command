"""
bridge.py — Connects the SurveillanceSystem to the SentinelAI dashboard
========================================================================

Use this when running CV detection on a separate machine / process from
the Flask web server (e.g. Raspberry Pi → cloud-hosted dashboard).

If detection runs on the same machine as app.py, you do NOT need bridge.py —
app.py integrates smart_surveillance.SurveillanceSystem directly.

Usage:
    # Real camera (webcam / USB)
    python bridge.py --source 0

    # IP camera / DroidCam
    python bridge.py --source "http://192.168.1.15:4747/video"

    # Demo mode (random alerts every 15-40 s)
    python bridge.py --source demo

    # Remote dashboard
    python bridge.py --source 0 --dashboard https://myapp.onrender.com
"""

import sys
import os
import time
import requests
import random
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] BRIDGE %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("SentinelAI.Bridge")

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:5000")

LOCATION_MAP = {
    "fire":       "Kitchen / Detected Zone",
    "accident":   "Parking / Entrance Area",
    "violence":   "Corridor / Public Area",
    "suspicious": "CCTV Zone",
}


# ── Alert posting ─────────────────────────────────────────────────────────────
def post_alert(alert_type, confidence=0.85, location=None, evidence_path="", retries=3):
    payload = {
        "type":          alert_type,
        "location":      location or LOCATION_MAP.get(alert_type, "Camera Zone"),
        "confidence":    round(max(0.0, min(1.0, confidence)), 3),
        "evidence_path": evidence_path or "",
    }
    for attempt in range(retries):
        try:
            r = requests.post(
                f"{DASHBOARD_URL}/api/alert",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
            if r.status_code == 200:
                logger.info(f"Alert sent: {alert_type} (conf={confidence:.2f}) → HTTP {r.status_code}")
                return True
            elif r.status_code == 429:
                logger.warning("Rate-limited by dashboard — backing off 3 s.")
                time.sleep(3)
            else:
                logger.warning(f"Dashboard returned HTTP {r.status_code}: {r.text[:120]}")
                return False
        except requests.exceptions.ConnectionError:
            if attempt < retries - 1:
                logger.warning(f"Dashboard not reachable (attempt {attempt + 1}/{retries}), retrying...")
                time.sleep(2)
            else:
                logger.error(f"Dashboard unreachable at {DASHBOARD_URL}. Is app.py running?")
    return False


def _wait_for_dashboard(timeout=30):
    """Block until /health responds or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{DASHBOARD_URL}/health", timeout=2)
            if r.status_code in (200, 503):
                logger.info("Dashboard is up.")
                return True
        except Exception:
            pass
        time.sleep(2)
    logger.warning("Dashboard did not respond within timeout — proceeding anyway.")
    return False


# ── Real surveillance mode ────────────────────────────────────────────────────
def run_surveillance(source):
    """
    Load smart_surveillance.SurveillanceSystem, attach an alert callback
    that forwards detections to the remote dashboard via /api/alert.
    """
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from smart_surveillance import SurveillanceSystem
    except ImportError:
        logger.error(
            "smart_surveillance.py not found next to bridge.py.\n"
            "  Expected: %s/smart_surveillance.py",
            os.path.dirname(os.path.abspath(__file__)),
        )
        logger.info("Falling back to demo alert mode.")
        _demo_alerts()
        return

    _wait_for_dashboard()

    def on_alert(alert_type, confidence, location, evidence_path=""):
        post_alert(
            alert_type=alert_type,
            confidence=confidence,
            location=location,
            evidence_path=evidence_path,
        )

    logger.info(f"Starting SurveillanceSystem on source: {source}")
    system = SurveillanceSystem(alert_manager=on_alert, camera_source=source)
    try:
        system.start()   # blocking
    except KeyboardInterrupt:
        system.stop()
        logger.info("Bridge stopped by user.")
    except RuntimeError as exc:
        logger.error(f"SurveillanceSystem error: {exc}")
        logger.info("Falling back to demo alert mode.")
        _demo_alerts()


# ── Demo mode ─────────────────────────────────────────────────────────────────
def _demo_alerts():
    """Send periodic random alerts for testing without a real camera."""
    types = list(LOCATION_MAP.keys())
    _wait_for_dashboard()
    logger.info(f"Demo mode — sending random alerts every 15-40 s to {DASHBOARD_URL}")

    while True:
        delay = random.randint(15, 40)
        logger.info(f"Next demo alert in {delay} s...")
        time.sleep(delay)
        t = random.choice(types)
        post_alert(
            alert_type=t,
            confidence=round(0.70 + random.random() * 0.25, 2),
        )


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SentinelAI Surveillance Bridge")
    parser.add_argument(
        "--source", default="demo",
        help="Camera source: 0 (webcam), IP URL, or 'demo'",
    )
    parser.add_argument(
        "--dashboard", default=None,
        help="Override dashboard URL (default: http://localhost:5000)",
    )
    args = parser.parse_args()

    if args.dashboard:
        DASHBOARD_URL = args.dashboard

    source = args.source
    if source.isdigit():
        source = int(source)
    elif source == "demo":
        source = None

    print("=" * 56)
    print("  SentinelAI — Surveillance Bridge")
    print("=" * 56)
    print(f"  Source    : {source or 'Demo mode'}")
    print(f"  Dashboard : {DASHBOARD_URL}/dashboard")
    print("=" * 56)

    if source is not None:
        run_surveillance(source)
    else:
        _demo_alerts()
