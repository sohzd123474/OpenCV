# Implementation Roadmap

Six phases, ~20 weeks to production at one site, with compliance gates built in.
Team assumption: 2 engineers (1 CV/backend, 1 full-stack) + part-time DevOps.

---

## Phase 0 — Foundations & Compliance Gate (Weeks 1–2)
- [ ] **DPIA / legal review of biometric processing in target jurisdictions** ← hard gate
- [ ] Privacy notice + consent flow copy; decide photo-retention policy
- [ ] Repo, CI (lint, tests, model-hash pinning), Docker Compose dev stack
      (Postgres+pgvector, Redis, MinIO)
- [ ] Apply `db/schema.sql`; seed scripts; migration tooling (Alembic)
- [ ] Procure pilot hardware: 1080p camera, kiosk compute, LED fill light

**Exit:** legal sign-off, dev environment up, schema migrated.

## Phase 1 — Recognition Pipeline Core (Weeks 3–6)
- [ ] Edge agent skeleton: OpenCV capture loop (USB/RTSP), frame budget management
- [ ] SCRFD detection + 5-point alignment → 112×112 (unit tests with fixture images)
- [ ] ArcFace ONNX embedding, L2-normalized; INT8 quantized CPU path
- [ ] Quality gate (blur/brightness/pose/occlusion/resolution) with coaching messages
- [ ] Low-light path: CLAHE + gamma correction
- [ ] Offline benchmark harness: LFW-style eval + a self-collected 20-person set at
      varied angles/lighting → produces similarity distributions for threshold calibration

**Exit:** ≥99% TAR @ FAR 0.1% on benchmark set; ≤150 ms/frame on target hardware.

## Phase 2 — Matching Service & Liveness (Weeks 7–10)
- [ ] `VectorStore` interface + FAISS `IndexFlatIP` implementation; tombstone + rebuild job
- [ ] Decision engine: accept/buffer/reject thresholds, top-2 margin, config versioning
- [ ] Passive liveness (MiniFASNet) integrated at edge; spoof test session (printed
      photos, phone screens) — record baseline attack rejection rate
- [ ] Active challenge (blink/head-turn) for buffer-zone flows
- [ ] FastAPI backend: device mTLS, signed-payload verification, match + attendance
      endpoints, Redis dedup window
- [ ] `match_attempts` audit writing (every attempt, all decisions)

**Exit:** end-to-end kiosk→backend check-in works; spoof rejection ≥98% on test kit.

## Phase 3 — Admin Dashboard & Onboarding (Weeks 11–14)
- [ ] React SPA: auth (SSO/OIDC + MFA), RBAC per spec
- [ ] **Onboarding wizard**: guided 8-pose capture with live quality feedback,
      consent capture (versioned notice), enrollment review/approve
- [ ] Employee lifecycle: suspend, offboard → automatic biometric purge job
- [ ] Live monitor: recent events stream (WebSocket), device health/heartbeat board
- [ ] Config UI: thresholds per site with change-reason (writes `system_config`)

**Exit:** HR can onboard an employee start-to-finish without engineering help.

## Phase 4 — Reporting, Anomalies & Hardening (Weeks 15–17)
- [ ] Reports: daily/weekly/monthly attendance, late/absence trends, CSV/XLSX/PDF export,
      payroll webhook
- [ ] Anomaly rules: repeated failed attempts, impossible travel, off-hours, spoof
      clusters, device drift (rising buffer rate) → `anomaly_flags` + email/Slack alerts
- [ ] Employee self-service page (own records, consent status) — GDPR subject access
- [ ] Retention jobs: partition drop for `match_attempts`, purge verification
- [ ] Security pass: pen test on API, dependency audit, secrets scan, log-redaction check
- [ ] Load test: 50 concurrent check-ins, 10k-employee synthetic gallery

**Exit:** security review signed off; reports validated against manual timesheets.

## Phase 5 — Pilot → Production (Weeks 18–20)
- [ ] Pilot: one entrance, 30–50 volunteers, 2 weeks, badge system running in parallel
- [ ] Calibrate thresholds from pilot data (site-specific FAR/FRR curve); fix top
      quality-gate failure causes (usually lighting)
- [ ] Site installation checklist enforcement (camera height, lux, backlight)
- [ ] Runbook: kiosk offline, index rebuild, cert revocation, erasure request
- [ ] Go-live + 2-week hypercare with daily metric review (buffer rate, FRR complaints)

**Exit criteria for full rollout:** pilot FRR < 2%, zero confirmed false accepts,
buffer rate < 5%, no unresolved privacy complaints.

## Later (post-v1 backlog)
- Milvus migration path (>100k gallery / multi-site)
- IR/depth liveness hardware tier
- Mobile check-in app (GPS geofence, consent-gated)
- Template aging / EMA reference refresh (design in architecture §2.5, ship after
  audit review)
- HR system integrations (Workday/BambooHR sync)

## Key Risks
| Risk | Mitigation |
|------|-----------|
| Poor site lighting tanks accuracy | Hardware checklist is a gate; fill light standard; buffer-rate alerting |
| Legal blocks biometric use | Phase 0 gate before any build spend on jurisdiction-specific features; badge/PIN fallback is permanent |
| Threshold set from public benchmarks doesn't fit real population | Pilot-driven calibration is mandatory, not optional |
| FAISS deletes vs. erasure requests | Tombstone + nightly rebuild from day one; Milvus when scale demands |
