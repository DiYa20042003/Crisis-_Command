"""Unit and API integration tests for SentinelAI Crisis Command Center"""
import unittest
import json
import os
import sys

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import app, db, Incident, LogEvent, seed_incidents


class TestCrisisCommandAPI(unittest.TestCase):

    def setUp(self):
        # Configure app for testing with in-memory SQLite
        app.config["TESTING"] = True
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.client = app.test_client()

        with app.app_context():
            db.create_all()
            seed_incidents()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_stats_endpoint(self):
        """Verify /api/stats returns valid metrics structure"""
        response = self.client.get("/api/stats")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("active_incidents", data)
        self.assertIn("resolved_today", data)
        self.assertIn("teams_deployed", data)
        self.assertIn("detectors", data)
        self.assertIn("camera", data)

    def test_incidents_listing(self):
        """Verify /api/incidents returns list of incidents"""
        response = self.client.get("/api/incidents")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)

    def test_trigger_alert_and_resolution_lifecycle(self):
        """Verify alert triggering and subsequent resolution state change"""
        # Trigger an alert
        alert_payload = {
            "type": "fire",
            "location": "Server Room B",
            "confidence": 0.95,
            "notes": "Smoke detected near rack 4"
        }
        res = self.client.post("/api/alert", json=alert_payload)
        self.assertEqual(res.status_code, 201)
        res_data = res.get_json()
        self.assertTrue(res_data["ok"])
        incident_id = res_data["incident_id"]

        # Check incident is active in listing
        inc_res = self.client.get("/api/incidents")
        incidents = inc_res.get_json()
        created = next((i for i in incidents if i["id"] == incident_id), None)
        self.assertIsNotNone(created)
        self.assertEqual(created["status"], "active")
        self.assertEqual(created["location"], "Server Room B")
        self.assertEqual(created["severity"], "CRITICAL")

        # Resolve the incident
        resolve_res = self.client.post(f"/api/incidents/{incident_id}/resolve")
        self.assertEqual(resolve_res.status_code, 200)
        resolve_data = resolve_res.get_json()
        self.assertTrue(resolve_data["ok"])
        self.assertEqual(resolve_data["incident"]["status"], "resolved")

    def test_alert_input_bounding_and_fallbacks(self):
        """Verify out-of-range confidence is clamped and unrecognized alert types fall back safely"""
        payload = {
            "type": "unrecognized_alien_invasion",
            "location": "Roof Heliport",
            "confidence": 5.0,  # Out of [0, 1] bounds
            "notes": "Testing input bounding"
        }
        res = self.client.post("/api/alert", json=payload)
        self.assertEqual(res.status_code, 201)
        res_data = res.get_json()
        incident_id = res_data["incident_id"]

        with app.app_context():
            inc = db.session.get(Incident, incident_id)
            self.assertEqual(inc.type, "suspicious")  # Fallback type
            self.assertEqual(inc.confidence, 1.0)     # Clamped to max 1.0

    def test_report_endpoint(self):
        """Verify /api/report returns comprehensive incident summary"""
        response = self.client.get("/api/report")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("total", data)
        self.assertIn("resolved", data)
        self.assertIn("resolution_rate", data)
        self.assertIn("recommendations", data)
        self.assertIsInstance(data["recommendations"], list)


if __name__ == "__main__":
    unittest.main()
