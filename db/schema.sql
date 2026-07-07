-- Face Recognition Attendance System — PostgreSQL 16 schema
-- Requires: pgvector (embeddings), pgcrypto (column encryption), uuid-ossp
-- Design rules:
--   * Postgres is the SOURCE OF TRUTH for embeddings; FAISS/Milvus is a rebuildable cache.
--   * No raw images in the database. Optional consented enrollment photos live in
--     encrypted object storage, referenced by URI.
--   * Audit tables are append-only (no UPDATE/DELETE grants; enforced by trigger).

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ═══════════════════════════ Identity & Access ═══════════════════════════

CREATE TABLE sites (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL,
    timezone    TEXT NOT NULL DEFAULT 'UTC',
    address     TEXT,
    geo_lat     DOUBLE PRECISION,
    geo_lon     DOUBLE PRECISION,
    geo_radius_m INTEGER          -- geofence for GPS validation, NULL = disabled
);

CREATE TABLE departments (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        TEXT NOT NULL UNIQUE,
    site_id     UUID REFERENCES sites(id)
);

CREATE TABLE employees (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    employee_code  TEXT NOT NULL UNIQUE,          -- HR system identifier
    full_name      TEXT NOT NULL,
    email          TEXT UNIQUE,
    department_id  UUID REFERENCES departments(id),
    site_id        UUID REFERENCES sites(id),
    status         TEXT NOT NULL DEFAULT 'pending_enrollment'
                   CHECK (status IN ('pending_enrollment','active','suspended','offboarded')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    offboarded_at  TIMESTAMPTZ                    -- triggers biometric purge job
);

-- Dashboard users (admins/HR), separate from employees being recognized
CREATE TABLE dashboard_users (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,                  -- argon2id
    role          TEXT NOT NULL CHECK (role IN ('superadmin','hr_admin','site_manager','auditor')),
    site_id       UUID REFERENCES sites(id),      -- NULL = all sites (superadmin/auditor)
    mfa_secret    BYTEA,                          -- encrypted TOTP secret
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ═══════════════════════════ Consent (GDPR Art. 9) ═══════════════════════════

CREATE TABLE consent_records (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    employee_id    UUID NOT NULL REFERENCES employees(id),
    consent_type   TEXT NOT NULL CHECK (consent_type IN
                     ('biometric_processing','photo_retention','gps_logging')),
    granted        BOOLEAN NOT NULL,
    policy_version TEXT NOT NULL,                 -- which privacy notice they saw
    granted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    withdrawn_at   TIMESTAMPTZ,                   -- withdrawal triggers purge workflow
    evidence_uri   TEXT                           -- signed form / e-signature record
);
CREATE INDEX idx_consent_employee ON consent_records(employee_id, consent_type);

-- ═══════════════════════════ Devices ═══════════════════════════

CREATE TABLE devices (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    site_id          UUID NOT NULL REFERENCES sites(id),
    name             TEXT NOT NULL,               -- 'Lobby Kiosk 1'
    device_type      TEXT NOT NULL CHECK (device_type IN ('kiosk','mobile','turnstile')),
    cert_fingerprint TEXT NOT NULL UNIQUE,        -- mTLS client cert pin
    model_version    TEXT,                        -- embedding model currently deployed
    firmware_version TEXT,
    has_ir_depth     BOOLEAN NOT NULL DEFAULT FALSE,
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,
    last_heartbeat   TIMESTAMPTZ,
    registered_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ═══════════════════════════ Biometric Templates ═══════════════════════════

CREATE TABLE face_enrollments (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    employee_id   UUID NOT NULL REFERENCES employees(id),
    enrolled_by   UUID NOT NULL REFERENCES dashboard_users(id),
    model_version TEXT NOT NULL,                  -- e.g. 'arcface_r100_glint360k_v1'
    device_id     UUID REFERENCES devices(id),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','superseded','purged')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_at     TIMESTAMPTZ
);

CREATE TABLE face_embeddings (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    enrollment_id  UUID NOT NULL REFERENCES face_enrollments(id) ON DELETE CASCADE,
    embedding      vector(512) NOT NULL,          -- L2-normalized ArcFace vector
    kind           TEXT NOT NULL DEFAULT 'reference'
                   CHECK (kind IN ('reference','centroid','lowlight_aug')),
    pose_label     TEXT,                          -- 'frontal','yaw_+20','pitch_-15',...
    quality_score  REAL NOT NULL,                 -- 0..1 from quality gate
    source_photo_uri TEXT,                        -- encrypted object storage; NULL if photo not retained
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_embeddings_enrollment ON face_embeddings(enrollment_id);
-- pgvector ANN index: serves small deployments directly and acts as the
-- rebuild source for FAISS/Milvus
CREATE INDEX idx_embeddings_ann ON face_embeddings
    USING hnsw (embedding vector_ip_ops);

-- ═══════════════════════════ Match Audit (append-only) ═══════════════════════════
-- EVERY recognition attempt is recorded — success, buffer, reject, spoof.

CREATE TABLE match_attempts (
    id               UUID NOT NULL DEFAULT uuid_generate_v4(),
    device_id        UUID NOT NULL REFERENCES devices(id),
    occurred_at      TIMESTAMPTZ NOT NULL,        -- capture time (device clock, NTP-synced)
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    recorded_offline BOOLEAN NOT NULL DEFAULT FALSE,
    matched_employee UUID REFERENCES employees(id),  -- NULL on reject
    top1_similarity  REAL,
    top2_margin      REAL,
    liveness_score   REAL NOT NULL,
    quality_score    REAL NOT NULL,
    decision         TEXT NOT NULL CHECK (decision IN
                       ('match','buffer_verified','buffer_failed','reject',
                        'spoof_suspected','fallback_pin','fallback_badge')),
    config_version   INTEGER NOT NULL,            -- which threshold set was live
    gps_lat          DOUBLE PRECISION,            -- mobile devices only, consent-gated
    gps_lon          DOUBLE PRECISION,
    payload_sig_ok   BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (id, occurred_at)
) PARTITION BY RANGE (occurred_at);               -- monthly partitions; retention job drops old ones

CREATE INDEX idx_attempts_employee_time ON match_attempts(matched_employee, occurred_at);
CREATE INDEX idx_attempts_device_time   ON match_attempts(device_id, occurred_at);
CREATE INDEX idx_attempts_decision      ON match_attempts(decision, occurred_at);

-- ═══════════════════════════ Attendance ═══════════════════════════

CREATE TABLE attendance_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    employee_id     UUID NOT NULL REFERENCES employees(id),
    match_attempt_id UUID NOT NULL,               -- references match_attempts(id)
    event_type      TEXT NOT NULL CHECK (event_type IN ('check_in','check_out')),
    occurred_at     TIMESTAMPTZ NOT NULL,
    site_id         UUID NOT NULL REFERENCES sites(id),
    device_id       UUID NOT NULL REFERENCES devices(id),
    confidence      REAL NOT NULL,                -- top1 similarity at decision time
    manual_override BOOLEAN NOT NULL DEFAULT FALSE,
    overridden_by   UUID REFERENCES dashboard_users(id),
    override_reason TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_attendance_emp_time ON attendance_events(employee_id, occurred_at);
CREATE INDEX idx_attendance_site_time ON attendance_events(site_id, occurred_at);

-- ═══════════════════════════ Anomalies & Alerts ═══════════════════════════

CREATE TABLE anomaly_flags (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rule          TEXT NOT NULL,                  -- 'repeated_failures','impossible_travel',
                                                  -- 'spoof_cluster','off_hours','device_drift'
    severity      TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    employee_id   UUID REFERENCES employees(id),
    device_id     UUID REFERENCES devices(id),
    details       JSONB NOT NULL,
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_by UUID REFERENCES dashboard_users(id),
    acknowledged_at TIMESTAMPTZ
);
CREATE INDEX idx_anomaly_open ON anomaly_flags(severity, detected_at)
    WHERE acknowledged_at IS NULL;

-- ═══════════════════════════ Configuration (versioned) ═══════════════════════════

CREATE TABLE system_config (
    version        SERIAL PRIMARY KEY,
    site_id        UUID REFERENCES sites(id),     -- NULL = global default
    accept_threshold  REAL NOT NULL DEFAULT 0.62,
    reject_threshold  REAL NOT NULL DEFAULT 0.50,
    top2_margin_min   REAL NOT NULL DEFAULT 0.05,
    liveness_threshold REAL NOT NULL DEFAULT 0.85,
    dedup_window_s    INTEGER NOT NULL DEFAULT 90,
    retention_days_attempts INTEGER NOT NULL DEFAULT 365,
    changed_by     UUID NOT NULL REFERENCES dashboard_users(id),
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_reason  TEXT NOT NULL,
    CHECK (reject_threshold < accept_threshold)
);

-- ═══════════════════════════ Admin Audit Trail (append-only) ═══════════════════════════

CREATE TABLE admin_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    actor_id    UUID NOT NULL REFERENCES dashboard_users(id),
    action      TEXT NOT NULL,                    -- 'enroll','purge_biometrics','config_change',
                                                  -- 'export_report','manual_override','template_refresh'
    target_type TEXT NOT NULL,
    target_id   UUID,
    details     JSONB,
    ip_address  INET,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    prev_hash   BYTEA,                            -- hash chain: sha256(prev_hash || row)
    row_hash    BYTEA NOT NULL
);

-- Enforce append-only on audit tables
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER no_update_attempts BEFORE UPDATE OR DELETE ON match_attempts
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER no_update_admin_audit BEFORE UPDATE OR DELETE ON admin_audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
