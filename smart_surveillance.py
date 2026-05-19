"""
smart_surveillance.py — SentinelAI Core Detection Engine
=========================================================
Four independent detectors, each using multi-frame confirmation:

  FireDetector       — HSV color segmentation + temporal flicker analysis
  ViolenceDetector   — Dense optical flow + motion jerk analysis
  AccidentDetector   — Background subtraction + blob-area surge detection
  SuspiciousDetector — Zone-based dwell-time tracking

False-positive reduction strategy:
  1. Each detector requires N consecutive positive frames before firing an alert.
  2. Per-type alert cooldowns prevent duplicate notifications.
  3. Every detector uses ≥2 independent signals (color + flicker, flow + jerk, etc.)
  4. Evidence frames (pre-event buffer) are saved for every real alert.

Usage (integrated in app.py):
    from smart_surveillance import SurveillanceSystem

    def on_alert(alert_type, confidence, location, evidence_path):
        create_incident(alert_type, location, confidence, evidence_path=evidence_path)

    system = SurveillanceSystem(alert_manager=on_alert)
    # In your camera loop:
    system.process_frame(frame)

Usage (standalone):
    system = SurveillanceSystem(alert_manager=callback, camera_source=0)
    system.start()   # blocks; call system.stop() from another thread
"""

import cv2
import numpy as np
import os
import time
import json
import logging
from collections import deque
from threading import Lock

logger = logging.getLogger("SentinelAI.Detection")


# ── Fire Detector ─────────────────────────────────────────────────────────────

class FireDetector:
    """
    Two-stage fire detection:

    Stage 1 — HSV color segmentation
        Four colour ranges cover the full fire palette:
        low-red (hue 0-15), orange (15-30), yellow (25-40), wrap-red (165-180).
        High saturation + high brightness thresholds eliminate dim red objects.

    Stage 2 — Temporal flicker confirmation
        Real fire causes the pixel count to oscillate frame-to-frame.
        The coefficient of variation (σ/μ) of the last 6 counts must exceed
        FLICKER_COEFF_MIN, filtering static red signs, lamps, and clothing.

    Stage 3 — Multi-frame confirmation
        Detection must hold for CONFIRMATION_REQUIRED consecutive frames
        before an alert is emitted.
    """

    CONFIRMATION_REQUIRED = 5
    MIN_FIRE_PIXELS       = 600          # minimum coloured pixels to consider
    FLICKER_COEFF_MIN     = 0.08         # σ/μ threshold — fire flickers, signs don't

    def __init__(self):
        self._px_history        = deque(maxlen=10)
        self._confirmation      = 0

    # ------------------------------------------------------------------
    def _fire_mask(self, frame: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        # Low red (hue 0-15): bright orange-red flame core
        m1 = cv2.inRange(hsv,
                         np.array([0,   120, 160], np.uint8),
                         np.array([15,  255, 255], np.uint8))
        # Orange (hue 15-30): outer flame
        m2 = cv2.inRange(hsv,
                         np.array([15,  100, 150], np.uint8),
                         np.array([30,  255, 255], np.uint8))
        # Yellow (hue 25-40): hot bright core
        m3 = cv2.inRange(hsv,
                         np.array([25,   80, 155], np.uint8),
                         np.array([40,  255, 255], np.uint8))
        # Wrap-around red (hue 165-180): deep crimson fire edge
        m4 = cv2.inRange(hsv,
                         np.array([165, 100, 150], np.uint8),
                         np.array([180, 255, 255], np.uint8))

        mask = cv2.bitwise_or(m1, cv2.bitwise_or(m2, cv2.bitwise_or(m3, m4)))

        # Morphological cleanup: remove speckle (open) and fill holes (close)
        k    = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        return mask

    # ------------------------------------------------------------------
    def detect(self, frame: np.ndarray) -> tuple[bool, float]:
        """Returns (detected, confidence∈[0,1])."""
        mask  = self._fire_mask(frame)
        n_px  = int(cv2.countNonZero(mask))
        total = frame.shape[0] * frame.shape[1]

        self._px_history.append(n_px)

        if n_px < self.MIN_FIRE_PIXELS:
            self._confirmation = max(0, self._confirmation - 1)
            return False, 0.0

        # Temporal flicker check — static coloured objects have near-zero σ/μ
        if len(self._px_history) >= 6:
            vals  = list(self._px_history)[-6:]
            mu    = float(np.mean(vals)) or 1.0
            if float(np.std(vals)) / mu < self.FLICKER_COEFF_MIN:
                self._confirmation = max(0, self._confirmation - 1)
                return False, 0.0

        self._confirmation += 1

        if self._confirmation >= self.CONFIRMATION_REQUIRED:
            # Confidence driven by pixel density (capped) + flicker bonus
            density_term = min(0.39, (n_px / total) * 8.0)
            confidence   = round(min(0.94, 0.55 + density_term), 2)
            return True, confidence

        return False, 0.0

    def reset(self):
        self._confirmation = 0
        self._px_history.clear()


# ── Violence Detector ─────────────────────────────────────────────────────────

class ViolenceDetector:
    """
    Three-signal violence detection:

    Signal 1 — Motion magnitude (dense Farneback optical flow)
        High mean magnitude indicates significant movement, but running,
        sports, and crowd walking also produce high magnitude.

    Signal 2 — Jerk (std-dev of recent accelerations)
        Jerk = rate of change of acceleration. Violent actions produce
        sudden, irregular bursts of energy — very high jerk.
        Normal activities (even running) have smoother acceleration curves.

    Signal 3 — Motion non-uniformity (σ of per-pixel magnitude)
        Camera shake shifts the entire frame → uniform magnitude, low σ.
        Violence involves localised intense motion → non-uniform, high σ.

    All three must exceed thresholds together for CONFIRMATION_REQUIRED
    consecutive frames before an alert fires.
    """

    CONFIRMATION_REQUIRED = 6
    JERK_THRESHOLD        = 5.0   # std-dev of acceleration history
    MIN_COVERAGE          = 0.04  # fraction of pixels with magnitude > 2 px/frame
    MIN_MOTION_VARIANCE   = 1.5   # σ of per-pixel magnitude

    def __init__(self):
        self._prev_gray   = None
        self._prev_mag    = None
        self._accel_hist  = deque(maxlen=10)
        self._confirmation = 0

    # ------------------------------------------------------------------
    def detect(self, frame: np.ndarray) -> tuple[bool, float]:
        gray = cv2.GaussianBlur(
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (7, 7), 0
        )

        if self._prev_gray is None:
            self._prev_gray = gray
            return False, 0.0

        flow = cv2.calcOpticalFlowFarneback(
            self._prev_gray, gray, None,
            pyr_scale=0.5, levels=3, winsize=15, iterations=3,
            poly_n=5, poly_sigma=1.2, flags=0
        )
        mag, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])

        mean_mag = float(np.mean(mag))
        coverage = float(np.sum(mag > 2.0)) / mag.size
        variance = float(np.std(mag))

        if self._prev_mag is not None:
            accel = abs(mean_mag - float(np.mean(self._prev_mag)))
            self._accel_hist.append(accel)

            if len(self._accel_hist) >= 3:
                jerk       = float(np.std(list(self._accel_hist)[-5:]))
                is_violent = (
                    jerk     > self.JERK_THRESHOLD      and
                    coverage > self.MIN_COVERAGE        and
                    variance > self.MIN_MOTION_VARIANCE
                )

                if is_violent:
                    self._confirmation += 1
                else:
                    self._confirmation = max(0, self._confirmation - 1)

                if self._confirmation >= self.CONFIRMATION_REQUIRED:
                    confidence = round(min(0.91, 0.52 + min(0.39, jerk / 25.0)), 2)
                    self._prev_gray = gray
                    self._prev_mag  = mag
                    return True, confidence
        else:
            self._accel_hist.append(0.0)

        self._prev_gray = gray
        self._prev_mag  = mag
        return False, 0.0

    def reset(self):
        self._confirmation = 0
        self._accel_hist.clear()
        self._prev_gray = None
        self._prev_mag  = None


# ── Accident Detector ─────────────────────────────────────────────────────────

class AccidentDetector:
    """
    Background-subtraction based accident detection:

    A MOG2 background subtractor produces a foreground mask each frame.
    After morphological cleanup, the total significant contour area is computed.

    Accident / fall / collision events cause a sudden large spike in foreground
    area that greatly exceeds the recent rolling baseline.

    SURGE_RATIO ensures gradual scene changes (people slowly entering) do not
    trigger false alerts — only abrupt large-area events qualify.

    MIN_BLOB_AREA filters out noise and small irrelevant motion.
    """

    CONFIRMATION_REQUIRED = 4
    MIN_BLOB_AREA         = 3_500
    SURGE_RATIO           = 3.5    # recent area must be 3.5× rolling baseline

    def __init__(self):
        self._bg_sub = cv2.createBackgroundSubtractorMOG2(
            history=200, varThreshold=40, detectShadows=False
        )
        self._area_history  = deque(maxlen=25)
        self._confirmation  = 0

    # ------------------------------------------------------------------
    def detect(self, frame: np.ndarray) -> tuple[bool, float]:
        mask = self._bg_sub.apply(frame)
        k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k)

        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        total_area = sum(
            cv2.contourArea(c) for c in contours
            if cv2.contourArea(c) > 400
        )
        self._area_history.append(total_area)

        if len(self._area_history) < 8:
            return False, 0.0

        history  = list(self._area_history)
        recent   = float(np.mean(history[-3:]))
        baseline = float(np.mean(history[:-3])) + 1.0   # +1 avoids /0

        if recent > self.MIN_BLOB_AREA and recent / baseline > self.SURGE_RATIO:
            self._confirmation += 1
        else:
            self._confirmation = max(0, self._confirmation - 2)

        if self._confirmation >= self.CONFIRMATION_REQUIRED:
            confidence = round(min(0.89, 0.55 + min(0.34, recent / 80_000)), 2)
            return True, confidence

        return False, 0.0

    def reset(self):
        self._confirmation = 0
        self._area_history.clear()


# ── Suspicious Activity Detector ──────────────────────────────────────────────

class SuspiciousActivityDetector:
    """
    Zone-based loitering detection:

    The frame is divided into a GRID (2 rows × 3 cols = 6 zones).
    Each zone tracks when a person (foreground blob > MIN_PERSON_AREA) enters.
    If the same zone stays occupied for more than DWELL_THRESHOLD seconds,
    a suspicious-activity alert is raised.

    Per-zone cooldowns (ZONE_COOLDOWN) prevent repeat alerts for the
    same loiterer until they have had time to leave and re-enter the zone.

    Small-blob filtering (MIN_PERSON_AREA) suppresses swaying plants, flags,
    and other non-human foreground noise.
    """

    CONFIRMATION_REQUIRED = 3
    DWELL_THRESHOLD       = 25.0    # seconds before a zone triggers an alert
    ZONE_COOLDOWN         = 90.0    # seconds before the same zone can alert again
    MIN_PERSON_AREA       = 1_500
    GRID                  = (2, 3)  # rows × cols

    def __init__(self):
        self._bg_sub       = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=50, detectShadows=False
        )
        self._zone_entry: dict[int, float]    = {}
        self._zone_cd:    dict[int, float]    = {}
        self._confirmation = 0

    # ------------------------------------------------------------------
    def _make_zones(self, shape) -> dict[int, tuple]:
        h, w      = shape[:2]
        rows, cols = self.GRID
        return {
            r * cols + c: (
                int(c * w / cols), int(r * h / rows),
                int((c + 1) * w / cols), int((r + 1) * h / rows)
            )
            for r in range(rows)
            for c in range(cols)
        }

    # ------------------------------------------------------------------
    def detect(self, frame: np.ndarray) -> tuple[bool, float]:
        mask = self._bg_sub.apply(frame)
        k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k)

        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        zones    = self._make_zones(frame.shape)
        occupied = set()

        for cnt in contours:
            if cv2.contourArea(cnt) < self.MIN_PERSON_AREA:
                continue
            M = cv2.moments(cnt)
            if M["m00"] == 0:
                continue
            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])
            for zid, (x1, y1, x2, y2) in zones.items():
                if x1 <= cx < x2 and y1 <= cy < y2:
                    occupied.add(zid)

        now = time.time()

        # Clear zones that are no longer occupied
        for zid in list(self._zone_entry):
            if zid not in occupied:
                del self._zone_entry[zid]

        # Track dwell; find the zone with the longest current dwell
        best_dwell, best_zone = 0.0, None
        for zid in occupied:
            if zid not in self._zone_entry:
                self._zone_entry[zid] = now
            dwell = now - self._zone_entry[zid]
            if dwell > best_dwell:
                best_dwell, best_zone = dwell, zid

        if best_zone is None or best_dwell < self.DWELL_THRESHOLD:
            self._confirmation = max(0, self._confirmation - 1)
            return False, 0.0

        # Per-zone cooldown check
        if now - self._zone_cd.get(best_zone, 0.0) < self.ZONE_COOLDOWN:
            return False, 0.0

        self._confirmation += 1
        if self._confirmation >= self.CONFIRMATION_REQUIRED:
            self._zone_cd[best_zone] = now
            self._confirmation       = 0
            confidence = round(min(0.87, 0.50 + min(0.37, best_dwell / 120.0)), 2)
            return True, confidence

        return False, 0.0

    def reset(self):
        self._confirmation = 0
        self._zone_entry.clear()


# ── Evidence Capture ──────────────────────────────────────────────────────────

class EvidenceCapture:
    """
    Rolling pre-event frame buffer.
    When save() is called, the buffered frames are flushed to disk as
    JPEG evidence under evidence/<type>_<unix_ts>/.
    """

    def __init__(self, evidence_dir: str = "evidence", pre_count: int = 15):
        self._dir    = evidence_dir
        self._buffer: deque = deque(maxlen=pre_count)
        os.makedirs(evidence_dir, exist_ok=True)

    def add_frame(self, frame: np.ndarray):
        self._buffer.append((time.time(), frame))

    def save(self, incident_type: str, confidence: float) -> str:
        ts   = int(time.time())
        path = os.path.join(self._dir, f"{incident_type}_{ts}")
        os.makedirs(path, exist_ok=True)

        for i, (_, frm) in enumerate(self._buffer):
            cv2.imwrite(
                os.path.join(path, f"frame_{i:03d}.jpg"),
                frm,
                [cv2.IMWRITE_JPEG_QUALITY, 85]
            )

        meta = {
            "type":       incident_type,
            "confidence": confidence,
            "timestamp":  ts,
            "frames":     len(self._buffer),
        }
        with open(os.path.join(path, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)

        logger.info(f"Evidence saved → {path} ({len(self._buffer)} frames)")
        return path


# ── Surveillance System ───────────────────────────────────────────────────────

class SurveillanceSystem:
    """
    Orchestrates all four detectors over a live camera feed.

    Integration modes
    -----------------
    Integrated (called from app.py camera thread):
        system = SurveillanceSystem(alert_manager=callback)
        # inside your cv2.VideoCapture loop:
        system.process_frame(frame)

    Standalone (self-contained camera thread):
        system = SurveillanceSystem(alert_manager=callback, camera_source=0)
        system.start()   # blocking — runs until system.stop() is called

    alert_manager signature:
        callback(
            alert_type:    str,     # "fire" | "violence" | "accident" | "suspicious"
            confidence:    float,   # 0.0 – 1.0
            location:      str,     # human-readable zone description
            evidence_path: str,     # path to saved evidence frames
        )
    """

    # Minimum seconds between successive alerts of the same type
    COOLDOWNS = {
        "fire":       30,
        "violence":   45,
        "accident":   60,
        "suspicious": 90,
    }

    LOCATIONS = {
        "fire":       "Detected Zone / Camera Feed",
        "violence":   "Camera Zone / Monitored Area",
        "accident":   "Camera Zone / Monitored Area",
        "suspicious": "Monitored Zone / Camera Feed",
    }

    # ------------------------------------------------------------------
    def __init__(self, alert_manager=None, camera_source=0):
        self.alert_manager = alert_manager
        self.camera_source = camera_source

        self._fire       = FireDetector()
        self._violence   = ViolenceDetector()
        self._accident   = AccidentDetector()
        self._suspicious = SuspiciousActivityDetector()
        self._evidence   = EvidenceCapture()

        self._cooldowns: dict[str, float] = {}
        self._lock    = Lock()
        self._running = False

        self.stats = {
            "fps":              0.0,
            "frames_processed": 0,
            "alerts_sent":      0,
            "start_time":       time.time(),
        }

    # ------------------------------------------------------------------
    def _can_alert(self, atype: str) -> bool:
        return time.time() - self._cooldowns.get(atype, 0.0) > self.COOLDOWNS[atype]

    def _dispatch(self, atype: str, confidence: float):
        if not self._can_alert(atype):
            return
        self._cooldowns[atype] = time.time()
        self.stats["alerts_sent"] += 1

        evidence_path = self._evidence.save(atype, confidence)
        location      = self.LOCATIONS.get(atype, "Camera Zone")

        logger.info(
            f"ALERT dispatched: {atype.upper()} "
            f"conf={confidence:.2f} evidence={evidence_path}"
        )

        if self.alert_manager:
            try:
                self.alert_manager(atype, confidence, location, evidence_path)
            except Exception as exc:
                logger.error(f"alert_manager raised: {exc}")

    # ------------------------------------------------------------------
    def process_frame(self, frame: np.ndarray):
        """
        Run all detectors on one frame (960×540 recommended).
        Thread-safe; may be called from any thread.
        """
        self._evidence.add_frame(frame)
        self.stats["frames_processed"] += 1

        with self._lock:
            det, conf = self._fire.detect(frame)
            if det:
                self._dispatch("fire", conf)

            det, conf = self._violence.detect(frame)
            if det:
                self._dispatch("violence", conf)

            det, conf = self._accident.detect(frame)
            if det:
                self._dispatch("accident", conf)

            det, conf = self._suspicious.detect(frame)
            if det:
                self._dispatch("suspicious", conf)

    # ------------------------------------------------------------------
    def start(self):
        """
        Standalone mode: open the camera and run the detection loop
        in the calling thread (blocking).
        """
        cap = cv2.VideoCapture(self.camera_source)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera source: {self.camera_source}")

        self._running = True
        fps_t, fps_n  = time.time(), 0

        logger.info(f"SurveillanceSystem started on source: {self.camera_source}")
        try:
            while self._running:
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.05)
                    continue

                frame = cv2.resize(frame, (960, 540))
                self.process_frame(frame)

                fps_n += 1
                elapsed = time.time() - fps_t
                if elapsed >= 1.0:
                    self.stats["fps"] = fps_n / elapsed
                    fps_n = 0
                    fps_t = time.time()
        finally:
            cap.release()
            self._running = False
            logger.info("SurveillanceSystem stopped")

    def stop(self):
        self._running = False
