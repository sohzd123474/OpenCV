# Face Recognition Attendance System

Enterprise-grade attendance system built on OpenCV, with a three-zone matching engine,
multi-reference enrollment, a cloud dashboard with an AI tutor, and GDPR-aware
biometric data handling.

**Working today:** local recognition kiosk (Python/OpenCV) + Cloudflare Worker
dashboard. **Designed for production:** see the docs for the full InsightFace /
FAISS / PostgreSQL architecture this implementation is shaped to grow into.

## Quickstart — kiosk (Python)

```bash
pip install -r requirements.txt
python scripts/download_models.py        # YuNet detector + SFace recognizer (ONNX)

python -m app add-employee --code E001 --name "Ada Lovelace"
python -m app enroll --code E001         # captures 8 reference poses from the webcam
python -m app run                        # live recognition -> check-in/check-out
python -m app report --csv july.csv      # export attendance
```

Configuration (thresholds, camera, dashboard URL) lives in `config.json` —
see `python -m app config` for all fields and defaults.

## Quickstart — dashboard (Cloudflare Worker)

```bash
npm i -g wrangler
wrangler deploy                          # serves the dashboard + API
```

Then set `worker_url` in `config.json`; the kiosk pushes events to
`POST /api/checkin` (and `python -m app sync` replays anything recorded offline).
The **Learning tab** is an AI tutor: with the `LM_API_TOKEN` Secrets Store binding
configured (see `wrangler.toml`) it answers via the configured LLM; without a token
it falls back to built-in explainers. Storage is in-memory demo mode until you bind
a D1 database (instructions in `wrangler.toml`); set `DASHBOARD_API_KEY` to require
auth on write endpoints.

## How matching works

Embeddings are L2-normalized, so cosine similarity is a dot product, with three zones:

```
sim >= accept (0.40 SFace default)  -> attendance recorded
reject <= sim < accept              -> buffer: secondary verification, never auto-accept
sim < reject (0.28 default)         -> unknown
```

plus a top-2 margin guard against look-alikes. Each employee enrolls ~8 reference
embeddings; matching takes the max across them.

⚠️ **Liveness anti-spoofing is not yet implemented** — the pipeline accepts a printed
photo today. Architecture §2.3 specifies the passive + active liveness design; do not
deploy at a real entrance before integrating it.

## Design documents

| Document | Contents |
|----------|----------|
| [docs/architecture.md](docs/architecture.md) | Recognition pipeline (detect → quality gate → liveness → align → embed), threshold logic, FAISS→Milvus scaling, deployment topology, hardware checklist |
| [db/schema.sql](db/schema.sql) | Production PostgreSQL 16 + pgvector schema: consent records, append-only match audit, versioned config |
| [docs/security-privacy.md](docs/security-privacy.md) | Threat model, encryption/keys, RBAC, GDPR Art. 9 compliance, right-to-erasure workflow |
| [docs/roadmap.md](docs/roadmap.md) | 6 phases / ~20 weeks to production, with a legal compliance gate first |

## Repository layout

```
app/            kiosk application: pipeline, matcher, SQLite storage, CLI
  pipeline.py   YuNet detect -> quality gate -> CLAHE low-light -> SFace embed
  matcher.py    three-zone decision engine (accept / buffer / reject + margin)
  db.py         local storage (employees, embeddings, attempts, attendance)
  cli.py        add-employee / enroll / run / report / sync
worker.js       Cloudflare Worker: dashboard UI + API + AI tutor (/api/copilot)
wrangler.toml   worker config: LM vars, Secrets Store token binding, optional D1
db/schema.sql   production-grade PostgreSQL schema (design target)
docs/           architecture, security & privacy, roadmap
tests/smoke.py  matcher + DB tests (no camera needed)
```

## Production upgrade path

The local stack is deliberately swappable: `pipeline.py` (YuNet+SFace, 128-D) →
InsightFace SCRFD+ArcFace (512-D) is a two-call change; `db.py` (SQLite) →
`db/schema.sql` (Postgres+pgvector); brute-force matching → FAISS/Milvus behind the
same matcher interface. The dashboard, CLI, and decision logic stay unchanged.
