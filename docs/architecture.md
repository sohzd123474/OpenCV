# Face Recognition Attendance System — Technical Architecture

**Status:** Design v1.0 · 2026-07-07
**Scope:** Enterprise-grade attendance via face recognition. Robust, scalable, privacy-compliant.

---

## 1. System Overview

```
┌─────────────────────────────  EDGE (kiosk / camera)  ─────────────────────────────┐
│                                                                                    │
│  Camera ─▶ Frame Grab ─▶ Face Detect ─▶ Quality Gate ─▶ Liveness ─▶ Align ─▶ Embed │
│  (RTSP/USB)  (OpenCV)    (SCRFD)       (blur/pose/    (passive +   (5-pt)  (ArcFace│
│                                         light score)   active)              512-D) │
│                                                                                    │
└───────────────────────────────────┬────────────────────────────────────────────────┘
                                    │  HTTPS/mTLS — signed payload:
                                    │  {embedding, liveness_score, quality, device_id, ts}
                                    ▼
┌─────────────────────────────  BACKEND (FastAPI)  ─────────────────────────────────┐
│                                                                                    │
│   API Gateway ─▶ Match Service ─▶ Decision Engine ─▶ Attendance Service            │
│   (auth, rate      (FAISS/Milvus     (threshold +       (check-in/out state        │
│    limit)           1:N cosine)       reject buffer)      machine, dedup)          │
│         │                                    │                     │               │
│         ▼                                    ▼                     ▼               │
│   PostgreSQL  ◀──────────────  Audit Log (append-only)  ──▶  Anomaly Detector      │
│   (identity, consent,                                        (failed-attempt       │
│    events, config)                                            patterns, alerts)    │
└───────────────────────────────────┬────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────  ADMIN LAYER (React SPA)  ────────────────────────────────┐
│   Onboarding wizard · Role management · Live monitor · Reports/exports · Alerts   │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Key architectural decision — embed at the edge:** raw video never leaves the device.
Only the 512-D embedding, liveness score, quality metrics, and metadata cross the
network. This shrinks the privacy blast radius, cuts bandwidth ~1000×, and makes the
backend stateless with respect to imagery.

---

## 2. Core Recognition Pipeline

### 2.1 Face Detection
- **Model:** SCRFD (InsightFace) — better speed/accuracy trade-off than RetinaFace on
  edge hardware; falls back to RetinaFace-R50 where GPU is available.
- **Output:** bounding box + 5 facial landmarks (eyes, nose, mouth corners) per face.
- **Policy:** if >1 face in frame, select largest box above a minimum size (≥80 px
  inter-eye distance); log a `multi_face` flag for the audit trail.

### 2.2 Quality Gate (runs before liveness — cheap rejects first)
| Check        | Method                                   | Reject below       |
|--------------|------------------------------------------|--------------------|
| Blur         | Variance of Laplacian on face crop       | configurable (~100)|
| Brightness   | Mean luma of face region                 | 40 / above 220     |
| Pose         | Yaw/pitch from landmarks (solvePnP)      | > ±30° yaw, ±20° pitch |
| Occlusion    | Landmark confidence + face-parsing mask  | >30% occluded      |
| Resolution   | Inter-eye pixel distance                 | < 60 px            |

Failed frames are **not** errors — the kiosk UI coaches the user ("move closer",
"remove sunglasses", "face the camera") and retries for up to N seconds.

**Low-light handling:** CLAHE (adaptive histogram equalization) + gamma correction
applied to the face crop when mean luma < 80, *before* embedding. Enrollment also
stores one low-light-augmented embedding per subject (see 2.5).

### 2.3 Liveness / Anti-Spoofing (two layers)
1. **Passive (always on):** MiniFASNet-style CNN (Silent-Face-Anti-Spoofing) on the
   face crop — catches printed photos, screen replays, most 2D attacks. Score ∈ [0,1];
   threshold configurable per site (default 0.85).
2. **Active challenge (triggered):** blink / head-turn prompt, invoked when
   (a) passive score is in the uncertain band (0.5–0.85), (b) the match falls in the
   rejection buffer, or (c) the device is flagged high-risk. Uses landmark tracking
   across ~2 s of frames (EAR for blink, yaw delta for head turn).
3. **Hardware upgrade path:** kiosks with IR/depth cameras (e.g., RealSense) get depth
   liveness as a third signal — recommended for high-security sites; catches 3D masks.

Every attendance event stores the liveness score; below-threshold attempts are logged
as `spoof_suspected` and never silently discarded (feeds the anomaly detector).

### 2.4 Alignment & Embedding
- **Alignment:** similarity transform mapping the 5 detected landmarks to the ArcFace
  canonical template → 112×112 crop. Non-negotiable: skipping alignment costs 5–15%
  accuracy.
- **Embedding:** ArcFace (InsightFace `buffalo_l` / glint360k R100) → **512-D float32
  vector, L2-normalized at creation time** so cosine similarity reduces to a dot
  product everywhere downstream.
- **Runtime:** ONNX Runtime (CPU: quantized INT8 model; GPU/NPU where available).
  Target: ≤150 ms detection→embedding on a Raspberry Pi 5-class device, ≤30 ms on GPU.

### 2.5 Multi-Reference Enrollment
- Onboarding captures **8 images**: frontal, ±20° yaw, ±15° pitch, smile/neutral,
  glasses on/off if applicable. Each must pass the quality gate.
- Stored per employee: all 8 embeddings **plus** a centroid embedding, **plus** one
  synthetic low-light embedding (gamma-darkened augmentation).
- Matching uses the max similarity across the subject's reference set — robust to the
  probe angle matching any one enrollment pose.
- **Template aging:** on each high-confidence match (sim > accept + 0.10), the probe
  embedding may refresh the oldest reference (EMA update, admin-configurable) so the
  gallery tracks appearance drift (hair, weight, aging). Refresh events are audited.

---

## 3. Matching & Decision Engine

### 3.1 Threshold logic with rejection buffer
With L2-normalized embeddings, `sim = dot(probe, reference)` ∈ [-1, 1].

```
sim ≥ T_accept  (default 0.62)        → MATCH   → record attendance
T_reject ≤ sim < T_accept (0.50–0.62) → BUFFER  → secondary verification:
                                                   active liveness re-run + second
                                                   capture; if still in buffer →
                                                   PIN/badge fallback, admin alert
sim < T_reject  (default 0.50)        → REJECT  → log attempt, generic UI message
```

Additional guards:
- **Top-2 margin:** require `sim(top1) − sim(top2) ≥ 0.05` between *different*
  identities; otherwise route to BUFFER (protects against look-alikes/twins).
- **Thresholds are configuration, not code** — stored in DB per site/device, versioned,
  and every decision logs the threshold version used (see schema `system_config` and
  `match_attempts.config_version`). Calibrate per deployment using an on-site
  validation set; publish the resulting FAR/FRR curve to admins.
- **Dedup:** ignore a repeat check-in for the same employee/device within a 90 s
  window (Redis key with TTL).

### 3.2 1:N Search — vector index strategy
| Employee count | Index                             | Notes                          |
|---------------:|-----------------------------------|--------------------------------|
| < 10k          | FAISS `IndexFlatIP` in-process    | Exact, ~ms latency, simplest   |
| 10k–100k       | FAISS HNSW (`IndexHNSWFlat`)      | ANN, still single-node         |
| > 100k / multi-site | **Milvus** (or Qdrant) cluster | Replication, deletes, filters, per-tenant partitions |

Design for the migration from day one: the Match Service exposes a `VectorStore`
interface (`add`, `remove`, `search(vec, k)`), with FAISS and Milvus implementations.
FAISS deletions are awkward — maintain a tombstone set and rebuild the index nightly;
Milvus handles deletes natively (this matters for GDPR erasure, see security doc).
PostgreSQL keeps the **authoritative** copy of embeddings (pgvector column); the vector
index is a rebuildable cache, never the source of truth.

---

## 4. Services & Components

| Component            | Technology                          | Responsibility |
|----------------------|-------------------------------------|----------------|
| Edge agent           | Python + OpenCV + ONNX Runtime      | Capture, detect, quality, liveness, align, embed, sign & ship payloads; offline queue |
| API gateway          | FastAPI + mTLS per device           | AuthN/Z, rate limiting, payload signature verification |
| Match service        | Python, `VectorStore` abstraction   | 1:N search, decision engine |
| Attendance service   | FastAPI                             | Check-in/out state machine, shift rules, dedup |
| Anomaly detector     | Scheduled job + streaming rules     | Repeated failures, impossible travel (two sites, short interval), off-hours patterns, spoof clusters |
| Reporting service    | SQL views + export workers          | CSV/XLSX/PDF exports, payroll integration webhooks |
| Admin dashboard      | React + TypeScript (Vite), REST/WS  | Onboarding wizard, live monitor, reports, config, alerts |
| Primary DB           | PostgreSQL 16 + pgvector + pgcrypto | Identity, events, consent, audit, embedding source of truth |
| Cache/queue          | Redis                               | Dedup windows, session cache, job queue (RQ/Celery) |
| Object storage       | S3-compatible, SSE-KMS encrypted    | Consented enrollment photos only (optional, see privacy doc) |

**Offline resilience:** edge agents queue events locally (SQLite) when the backend is
unreachable and replay with original capture timestamps on reconnect; events carry a
`recorded_offline` flag for audit honesty.

**Observability:** structured logs (no biometric payloads in logs), Prometheus metrics
(match latency, buffer-rate, spoof-rate per device — a rising buffer rate is the #1
early signal of a lighting/camera problem), Grafana dashboards, alerting on device
heartbeat loss.

---

## 5. Deployment Topology

- **Single site (≤500 employees):** one VM/container host — FastAPI + Postgres +
  Redis + FAISS in-process. Kiosks on the LAN.
- **Multi-site:** central backend (cloud or HQ), Milvus cluster, per-site edge agents
  over mTLS WAN links with offline queueing. Postgres read replicas for reporting.
- Everything containerized (Docker Compose → Kubernetes when multi-site). Model files
  versioned and pulled by edge agents at boot (hash-pinned — a model swap is a
  security-relevant event and is audited).

## 6. Hardware & Environment Requirements (deployment checklist)

Recognition quality is capped by capture quality. Site requirements, enforced at
installation sign-off:
- Camera at eye level (1.5–1.6 m), subject distance 0.5–1.2 m.
- Consistent, diffuse front lighting ≥300 lux at the face; no strong backlight
  (no camera facing a window/door). Add a kiosk-mounted LED fill light by default.
- 1080p camera minimum, ≥15 fps, fixed focus at kiosk distance.
- Per-device calibration step at install: capture test set, verify quality-gate pass
  rate > 95% and record baseline buffer rate.
