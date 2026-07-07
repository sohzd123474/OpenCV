"""Runtime configuration with JSON override.

Defaults live here; a config.json next to the repo root (or the path in the
ATTENDANCE_CONFIG env var) overrides any field. Thresholds are calibrated for
OpenCV SFace cosine similarity — OpenCV's published same-identity threshold is
0.363, so accept sits above it and the buffer zone below it (see
docs/architecture.md §3.1 for the three-zone design).
"""
import json
import os
from dataclasses import dataclass, asdict

CONFIG_PATH = os.environ.get("ATTENDANCE_CONFIG", "config.json")


@dataclass
class Config:
    db_path: str = "attendance.sqlite3"
    models_dir: str = "models"
    camera_index: int = 0
    accept_threshold: float = 0.40
    reject_threshold: float = 0.28
    min_top2_margin: float = 0.05
    dedup_window_s: int = 90
    enroll_samples: int = 8
    min_blur: float = 60.0
    min_brightness: float = 40.0
    max_brightness: float = 220.0
    lowlight_luma: float = 80.0
    device_id: str = "kiosk-dev-1"
    # Cloudflare Worker dashboard, e.g. https://opencv-attendance-dashboard.<acct>.workers.dev
    worker_url: str = ""
    worker_api_key: str = ""


def load() -> Config:
    cfg = Config()
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for key, value in data.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
    return cfg


def dump(cfg: Config) -> str:
    return json.dumps(asdict(cfg), indent=2)
