"""Recognition pipeline: detect -> quality gate -> (low-light enhance) -> align -> embed.

Default stack is pure OpenCV so it runs anywhere opencv-python installs:
  * YuNet  (cv2.FaceDetectorYN)    — detection + 5 landmarks
  * SFace  (cv2.FaceRecognizerSF)  — aligned crop + 128-D embedding

docs/architecture.md specifies InsightFace SCRFD + ArcFace (512-D) for
production; this module is the same pipeline shape, so swapping the two
model calls upgrades it without touching matching, storage, or the CLI.

Liveness is NOT implemented here — see docs/architecture.md §2.3. The
`liveness_score` returned is None and the dashboard displays it as such;
do not deploy to a real entrance without integrating an anti-spoofing model.
"""
import os
from dataclasses import dataclass

import cv2
import numpy as np

DETECTOR_MODEL = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = "face_recognition_sface_2021dec.onnx"


@dataclass
class QualityResult:
    ok: bool
    blur: float
    brightness: float
    reasons: list


class FacePipeline:
    def __init__(self, cfg):
        det_path = os.path.join(cfg.models_dir, DETECTOR_MODEL)
        rec_path = os.path.join(cfg.models_dir, RECOGNIZER_MODEL)
        for path in (det_path, rec_path):
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"model missing: {path} — run: python scripts/download_models.py"
                )
        self.cfg = cfg
        self.detector = cv2.FaceDetectorYN.create(det_path, "", (320, 320), 0.8, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(rec_path, "")
        self.embedding_dim = 128

    # ── detection ────────────────────────────────────────────────────────────
    def detect_best(self, frame):
        """Largest detected face (row of [x,y,w,h, 5x(lx,ly), score]) or None."""
        h, w = frame.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(frame)
        if faces is None or len(faces) == 0:
            return None
        return max(faces, key=lambda f: f[2] * f[3])

    # ── quality gate ─────────────────────────────────────────────────────────
    def quality(self, frame, face) -> QualityResult:
        x, y, fw, fh = (int(v) for v in face[:4])
        h, w = frame.shape[:2]
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + fw, w), min(y + fh, h)
        reasons = []
        if x1 - x0 < 60 or y1 - y0 < 60:
            reasons.append("too_small")
            return QualityResult(False, 0.0, 0.0, reasons)
        crop = frame[y0:y1, x0:x1]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        blur = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        brightness = float(gray.mean())
        if blur < self.cfg.min_blur:
            reasons.append("blurry")
        if brightness < self.cfg.min_brightness:
            reasons.append("too_dark")
        if brightness > self.cfg.max_brightness:
            reasons.append("too_bright")
        return QualityResult(not reasons, blur, brightness, reasons)

    # ── low-light enhancement ────────────────────────────────────────────────
    def enhance_if_dark(self, frame):
        """CLAHE on luma + mild gamma lift when the frame is dark (arch §2.2)."""
        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        if float(ycrcb[:, :, 0].mean()) >= self.cfg.lowlight_luma:
            return frame
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        ycrcb[:, :, 0] = clahe.apply(ycrcb[:, :, 0])
        out = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2BGR)
        lut = np.array([(i / 255.0) ** 0.75 * 255 for i in range(256)], dtype=np.uint8)
        return cv2.LUT(out, lut)

    # ── embedding ────────────────────────────────────────────────────────────
    def embed(self, frame, face) -> np.ndarray:
        """Aligned crop -> L2-normalized embedding (cosine == dot downstream)."""
        aligned = self.recognizer.alignCrop(frame, face)
        feat = self.recognizer.feature(aligned).flatten().astype(np.float32)
        norm = np.linalg.norm(feat)
        return feat / norm if norm > 0 else feat
