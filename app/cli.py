"""Command-line interface.

  python -m app add-employee --code E001 --name "Ada Lovelace"
  python -m app enroll --code E001 [--images path/to/photos | --camera 0]
  python -m app run [--camera 0]           # live recognition + attendance
  python -m app list
  python -m app report [--from 2026-07-01] [--to 2026-07-31] [--csv out.csv]
  python -m app sync                       # push unsynced events to the dashboard
  python -m app config                     # print effective configuration
"""
import argparse
import csv
import glob
import os
import sys
import time

from . import config as config_mod
from . import db as dbm


# ── helpers ──────────────────────────────────────────────────────────────────

def _open(cfg):
    return dbm.connect(cfg.db_path)


def _pipeline(cfg):
    from .pipeline import FacePipeline  # lazy: needs cv2 + model files
    return FacePipeline(cfg)


def _post_event(cfg, event) -> bool:
    import requests
    headers = {"content-type": "application/json"}
    if cfg.worker_api_key:
        headers["authorization"] = f"Bearer {cfg.worker_api_key}"
    resp = requests.post(
        cfg.worker_url.rstrip("/") + "/api/checkin",
        json={
            "employee_code": event["employee_code"],
            "event_type": event["event_type"],
            "occurred_at": event["occurred_at"],
            "similarity": event["similarity"],
            "device_id": cfg.device_id,
        },
        headers=headers,
        timeout=10,
    )
    return resp.ok


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_add_employee(cfg, args):
    conn = _open(cfg)
    emp_id = dbm.add_employee(conn, args.code, args.name)
    print(f"added employee #{emp_id}: {args.code} {args.name}")


def cmd_list(cfg, _args):
    conn = _open(cfg)
    rows = dbm.list_employees(conn)
    if not rows:
        print("no employees enrolled")
        return
    for r in rows:
        print(f"{r['code']:>8}  {r['name']:<24} {r['status']:<10} embeddings={r['n_embeddings']}")


def cmd_enroll(cfg, args):
    import cv2
    conn = _open(cfg)
    emp = dbm.get_employee_by_code(conn, args.code)
    if not emp:
        sys.exit(f"unknown employee code {args.code!r} — run add-employee first")
    pipe = _pipeline(cfg)
    captured = 0

    def try_frame(frame) -> bool:
        nonlocal captured
        frame = pipe.enhance_if_dark(frame)
        face = pipe.detect_best(frame)
        if face is None:
            return False
        q = pipe.quality(frame, face)
        if not q.ok:
            print(f"  skipped ({', '.join(q.reasons)})")
            return False
        vec = pipe.embed(frame, face)
        dbm.add_embedding(conn, emp["id"], vec, q.blur)
        captured += 1
        print(f"  captured {captured}/{cfg.enroll_samples}")
        return True

    if args.images:
        paths = sorted(
            p for pat in ("*.jpg", "*.jpeg", "*.png")
            for p in glob.glob(os.path.join(args.images, pat))
        )
        if not paths:
            sys.exit(f"no images found in {args.images}")
        for path in paths:
            if captured >= cfg.enroll_samples:
                break
            frame = cv2.imread(path)
            if frame is None:
                continue
            print(path)
            try_frame(frame)
    else:
        cam = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
        if not cam.isOpened():
            sys.exit(f"cannot open camera {args.camera}")
        print("look at the camera; vary your angle slightly. ESC to abort.")
        last_capture = 0.0
        while captured < cfg.enroll_samples:
            ok, frame = cam.read()
            if not ok:
                break
            display = frame.copy()
            cv2.putText(display, f"enroll {emp['code']}: {captured}/{cfg.enroll_samples}",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.imshow("enroll", display)
            if cv2.waitKey(1) & 0xFF == 27:
                break
            if time.time() - last_capture >= 0.5 and try_frame(frame):
                last_capture = time.time()
        cam.release()
        cv2.destroyAllWindows()

    print(f"enrolled {captured} reference embeddings for {emp['code']}")


def cmd_run(cfg, args):
    import cv2
    conn = _open(cfg)
    pipe = _pipeline(cfg)
    from .matcher import Matcher
    gallery = dbm.gallery(conn, pipe.embedding_dim)
    if not gallery:
        sys.exit("gallery is empty — enroll at least one employee first")
    matcher = Matcher(gallery, cfg.accept_threshold, cfg.reject_threshold, cfg.min_top2_margin)
    names = {r["id"]: (r["code"], r["name"]) for r in dbm.list_employees(conn)}

    cam = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    if not cam.isOpened():
        sys.exit(f"cannot open camera {args.camera}")
    print("running — press q to quit")
    last_event: dict[int, float] = {}  # employee_id -> monotonic time of last event

    while True:
        ok, frame = cam.read()
        if not ok:
            break
        frame = pipe.enhance_if_dark(frame)
        face = pipe.detect_best(frame)
        label, color = "no face", (128, 128, 128)

        if face is not None:
            q = pipe.quality(frame, face)
            x, y, fw, fh = (int(v) for v in face[:4])
            if not q.ok:
                label, color = f"quality: {', '.join(q.reasons)}", (0, 200, 255)
            else:
                decision = matcher.match(pipe.embed(frame, face))
                dbm.record_attempt(conn, decision.outcome, decision.employee_id,
                                   decision.similarity, decision.margin, q.blur)
                if decision.outcome == "match":
                    code, name = names[decision.employee_id]
                    since = time.monotonic() - last_event.get(decision.employee_id, -1e9)
                    if since >= cfg.dedup_window_s:
                        last = dbm.last_attendance(conn, decision.employee_id)
                        event_type = "check_out" if last and last["event_type"] == "check_in" else "check_in"
                        event_id = dbm.record_attendance(conn, decision.employee_id,
                                                         event_type, decision.similarity)
                        last_event[decision.employee_id] = time.monotonic()
                        print(f"{event_type}: {code} {name} (sim={decision.similarity:.3f})")
                        if cfg.worker_url:
                            row = dict(conn.execute(
                                "SELECT a.event_type, a.occurred_at, a.similarity, e.code AS employee_code "
                                "FROM attendance a JOIN employees e ON e.id=a.employee_id WHERE a.id=?",
                                (event_id,)).fetchone())
                            try:
                                if _post_event(cfg, row):
                                    dbm.mark_synced(conn, event_id)
                            except Exception as exc:  # offline: sync later via `sync`
                                print(f"  sync deferred: {exc}")
                    label, color = f"{name} {decision.similarity:.2f}", (0, 220, 0)
                elif decision.outcome == "buffer":
                    label, color = f"verify... {decision.similarity:.2f}", (0, 165, 255)
                else:
                    label, color = f"unknown {decision.similarity:.2f}", (0, 0, 220)
            cv2.rectangle(frame, (x, y), (x + fw, y + fh), color, 2)
            cv2.putText(frame, label, (x, max(y - 8, 16)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

        cv2.imshow("attendance", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cam.release()
    cv2.destroyAllWindows()


def cmd_report(cfg, args):
    conn = _open(cfg)
    rows = dbm.attendance_report(conn, args.date_from, args.date_to)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["occurred_at", "code", "name", "event_type", "similarity"])
            writer.writerows([tuple(r) for r in rows])
        print(f"wrote {len(rows)} rows to {args.csv}")
    else:
        for r in rows:
            print(f"{r['occurred_at']}  {r['code']:>8}  {r['name']:<24} "
                  f"{r['event_type']:<10} {r['similarity']:.3f}")
        print(f"({len(rows)} events)")


def cmd_sync(cfg, _args):
    if not cfg.worker_url:
        sys.exit("worker_url not configured — set it in config.json")
    conn = _open(cfg)
    events = dbm.unsynced_events(conn)
    pushed = 0
    for event in events:
        if _post_event(cfg, dict(event)):
            dbm.mark_synced(conn, event["id"])
            pushed += 1
    print(f"synced {pushed}/{len(events)} events to {cfg.worker_url}")


def cmd_config(cfg, _args):
    print(config_mod.dump(cfg))


# ── entry point ──────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(prog="app", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-employee")
    p.add_argument("--code", required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_add_employee)

    p = sub.add_parser("enroll")
    p.add_argument("--code", required=True)
    p.add_argument("--images", help="folder of reference photos (default: use camera)")
    p.add_argument("--camera", type=int, default=None)
    p.set_defaults(func=cmd_enroll)

    p = sub.add_parser("run")
    p.add_argument("--camera", type=int, default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("report")
    p.add_argument("--from", dest="date_from")
    p.add_argument("--to", dest="date_to")
    p.add_argument("--csv")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("sync")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("config")
    p.set_defaults(func=cmd_config)

    args = parser.parse_args(argv)
    cfg = config_mod.load()
    if getattr(args, "camera", None) is None and hasattr(args, "camera"):
        args.camera = cfg.camera_index
    args.func(cfg, args)
