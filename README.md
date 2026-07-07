# Face Recognition Attendance System

Enterprise-grade attendance system built on OpenCV + InsightFace (ArcFace 512-D
embeddings), with liveness anti-spoofing, a vector-search matching backend, an HR
admin dashboard, and GDPR-compliant biometric data handling.

**Status:** Design phase — architecture, schema, and roadmap complete; implementation
starts at Phase 1.

## Design Documents

| Document | Contents |
|----------|----------|
| [docs/architecture.md](docs/architecture.md) | System overview, recognition pipeline (detect → quality gate → liveness → align → embed), matching & threshold logic with rejection buffer, FAISS→Milvus scaling strategy, services, deployment topology, hardware checklist |
| [db/schema.sql](db/schema.sql) | PostgreSQL 16 + pgvector schema: employees, multi-reference enrollments, embeddings, devices, append-only match audit, attendance events, consent records, anomaly flags, versioned config |
| [docs/security-privacy.md](docs/security-privacy.md) | Biometric data handling, encryption & key management, threat model, RBAC, GDPR Art. 9 compliance, right-to-erasure workflow |
| [docs/roadmap.md](docs/roadmap.md) | 6 phases / ~20 weeks: compliance gate → pipeline → matching+liveness → dashboard → reporting+hardening → pilot & go-live |

## Core Design Decisions

- **Embed at the edge** — kiosks send 512-D vectors, never video or images.
- **Three-zone matching** — accept / rejection-buffer (secondary verification) /
  reject, plus a top-2 margin guard. Thresholds are versioned DB config, calibrated
  per site.
- **Multi-reference enrollment** — 8 poses per employee + centroid + low-light
  augmented embedding.
- **Two-layer liveness** — passive CNN always on, active blink/head-turn challenge in
  the uncertain band; IR/depth hardware tier for high-security sites.
- **Postgres is the source of truth; FAISS/Milvus is a rebuildable cache** — makes
  GDPR erasure and index migration tractable.
- **Everything is audited** — every recognition attempt (including failures and
  suspected spoofs), every admin action (hash-chained), every config change.
- **Permanent non-biometric fallback** (badge/PIN) — required for freely-given consent
  and for graceful false-reject handling.

## Planned Stack

Python · OpenCV · InsightFace (SCRFD + ArcFace) · ONNX Runtime · FastAPI ·
PostgreSQL 16 + pgvector · Redis · FAISS → Milvus · React + TypeScript · Docker
