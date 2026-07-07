# Security & Privacy Specification

Companion to `architecture.md`. Covers biometric data handling, threat model, and
GDPR/regional compliance.

---

## 1. Biometric Data Handling

### 1.1 Embeddings, not images
The system matches against **512-D float vectors** (ArcFace embeddings), never raw
photos. An embedding cannot be trivially reversed into the source photograph — it is a
lossy mathematical projection.

**Important legal honesty:** embeddings are *not* anonymous data. Research
reconstruction attacks can recover an approximate face from an embedding, and under
GDPR an embedding used to identify a person **is biometric data (Art. 9 special
category)** regardless of reversibility. We therefore protect embeddings with the same
rigor as raw biometrics — "vectors not images" reduces risk, it does not remove the
compliance obligation. Vendors claiming otherwise are wrong; design assuming the
embedding is sensitive.

### 1.2 Storage controls
| Data | At rest | In transit | Access |
|------|---------|-----------|--------|
| Embeddings (Postgres pgvector) | Full-disk + tablespace encryption (LUKS/TDE); DB on isolated network segment | TLS 1.3 | Match service role only; no dashboard read of raw vectors |
| Enrollment photos (optional, consent-gated) | S3 SSE-KMS, per-tenant keys | TLS 1.3 | HR admins, watermarked viewer, download audited |
| Attendance/audit records | Standard DB encryption | TLS 1.3 | Role-scoped (RBAC below) |
| Edge device model + queue | Encrypted local FS; no frames persisted beyond the processing window (≤2 s in RAM) | mTLS to backend | Device service account |

- Kiosks never write video to disk. Frames live in memory only for the pipeline pass.
- Structured logs and metrics must never contain embeddings, photos, or names —
  employee UUIDs only.

### 1.3 Key management
- KMS-backed (cloud KMS or HashiCorp Vault on-prem). Separate keys per data class
  (embeddings / photos / secrets), rotated annually or on suspicion.
- Device credentials: per-device mTLS client certificates, issued by an internal CA,
  revocable individually (compromised kiosk → revoke cert, device is instantly deaf).
- Payloads from edge devices are signed (Ed25519 device key) so a stolen backend API
  token alone cannot forge attendance.

## 2. Threat Model (summary)

| Threat | Mitigation |
|--------|-----------|
| Photo/video spoof at kiosk | Passive liveness always-on; active challenge in uncertain band; IR/depth at high-security sites |
| 3D mask | Depth liveness (hardware tier); anomaly detector flags repeated buffer-zone attempts |
| Replay of a captured payload | Nonce + timestamp in signed payload; server rejects stale/duplicate signatures |
| Stolen kiosk | Per-device cert revocation; no biometric gallery stored on device; disk encrypted |
| Insider exfiltration of embeddings | DB role separation, no bulk-export API for vectors, egress monitoring, access audited |
| Buddy punching via lookalike/twin | Top-2 margin rule; buffer → secondary verification; anomaly rules |
| Admin abuse (fake overrides) | Manual overrides require reason + are hash-chain audited; auditor role reviews |
| Threshold tampering to loosen FAR | Config is versioned, change-reason required, every decision records config version |

## 3. RBAC

| Role | Can |
|------|-----|
| `superadmin` | Everything incl. config, device registration, key ops |
| `hr_admin` | Enroll/offboard employees, view own-site photos (if retained), run reports |
| `site_manager` | View own-site attendance, acknowledge alerts, manual overrides (audited) |
| `auditor` | Read-only across audit logs, config history, consent records; no PII edits |

Dashboard auth: OIDC/SSO preferred, else argon2id + TOTP MFA (mandatory for
`superadmin`/`hr_admin`).

## 4. GDPR / Privacy Compliance

Biometric attendance is **high-risk processing** — the following are requirements, not
options:

1. **DPIA first.** Complete a Data Protection Impact Assessment before pilot. Several
   EU regulators (and jurisdictions like Illinois BIPA) treat workplace biometrics as
   requiring explicit justification; some EU DPAs have ruled attendance alone is
   *insufficient* justification without a genuine, freely-given alternative.
2. **Explicit consent + real alternative.** Consent in an employment context is only
   "freely given" if refusing has no penalty — so a **non-biometric fallback (badge/PIN)
   must exist permanently**, not just as a degraded mode. Consent is recorded per
   purpose (`consent_records`), versioned against the privacy notice shown, withdrawable
   at any time.
3. **Purpose limitation.** Embeddings are used for attendance matching only — no
   secondary use (mood analysis, surveillance, marketing) ever; enforce technically by
   not exposing vector read APIs.
4. **Data minimization.** Photos are optional to retain (consent-gated); default is to
   discard after embedding extraction. GPS only for mobile check-in and only with
   separate consent.
5. **Retention.** Match attempts: 12 months (partitioned, auto-dropped). Attendance
   records: per payroll/labor-law requirement (configurable, typically 2–7 years —
   these outlive the biometrics). Biometrics: deleted at offboarding or consent
   withdrawal.
6. **Right to erasure — the FAISS caveat.** Erasure must remove: Postgres embedding
   rows, object-storage photos, *and* the vector-index entry. FAISS can't hard-delete →
   tombstone immediately (excluded from results) + nightly index rebuild; Milvus
   deletes natively. Erasure completion is logged in `admin_audit_log` with a
   verification step (search for the purged vector must return nothing).
7. **Transparency.** Signage at kiosks, privacy notice at enrollment, employee-facing
   page to view their own attendance data and consent status (subject-access request
   support).
8. **Local law check.** Before each regional rollout, verify: EU (GDPR Art. 9 + member
   state employment law), US Illinois/Texas/Washington (BIPA-style statutes — private
   right of action, written release required), India (DPDP Act), etc. Make this a
   roadmap gate, not an afterthought.

## 5. Model & Fairness Governance

- Evaluate the chosen embedding model's accuracy across demographic groups on your own
  pilot population before go-live; publish FAR/FRR per site to admins.
- The buffer zone + human fallback exists precisely so a false reject never blocks a
  person from clocking in — recognition failure must degrade to badge/PIN, never to
  "denied".
- Model upgrades re-run the calibration procedure and require re-generating embeddings
  from retained photos (or re-enrollment where photos weren't retained) — track via
  `face_enrollments.model_version`.
