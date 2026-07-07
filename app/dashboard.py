"""Local web dashboard — Python stdlib only, no Node/Cloudflare needed.

    python -m app dashboard [--port 8000]

Reads the same SQLite database the kiosk writes, so run it in a second
terminal while `python -m app run` is live and watch events appear.
Binds to 127.0.0.1 only — this is a local monitor, not a hosted service.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import db as dbm


def _summary(conn):
    today = dbm.now_iso()[:10]
    row = lambda q, p=(): conn.execute(q, p).fetchone()[0]
    decisions = {
        r["decision"]: r["n"]
        for r in conn.execute(
            "SELECT decision, COUNT(*) AS n FROM match_attempts WHERE occurred_at >= ? GROUP BY decision",
            (today,),
        )
    }
    return {
        "employees": row("SELECT COUNT(*) FROM employees"),
        "events_total": row("SELECT COUNT(*) FROM attendance"),
        "checkins_today": row(
            "SELECT COUNT(*) FROM attendance WHERE event_type='check_in' AND occurred_at >= ?", (today,)
        ),
        "attempts_today": decisions,
    }


def _employees(conn):
    return [dict(r) for r in dbm.list_employees(conn)]


def _events(conn, limit=200):
    rows = conn.execute(
        "SELECT a.occurred_at, e.code, e.name, a.event_type, a.similarity, a.synced "
        "FROM attendance a JOIN employees e ON e.id = a.employee_id "
        "ORDER BY a.occurred_at DESC, a.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _attempts(conn, limit=200):
    rows = conn.execute(
        "SELECT a.occurred_at, a.decision, a.similarity, a.margin, a.quality, e.code, e.name "
        "FROM match_attempts a LEFT JOIN employees e ON e.id = a.employee_id "
        "ORDER BY a.occurred_at DESC, a.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def serve(cfg, port: int = 8000):
    db_path = cfg.db_path

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # keep the terminal quiet
            pass

        def _send(self, body: bytes, ctype: str, status: int = 200):
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(json.dumps(obj).encode("utf-8"), "application/json;charset=utf-8", status)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(PAGE.encode("utf-8"), "text/html;charset=utf-8")
                return
            if not path.startswith("/api/"):
                self._json({"error": "not found"}, 404)
                return
            conn = dbm.connect(db_path)
            try:
                if path == "/api/summary":
                    self._json(_summary(conn))
                elif path == "/api/employees":
                    self._json(_employees(conn))
                elif path == "/api/events":
                    self._json(_events(conn))
                elif path == "/api/attempts":
                    self._json(_attempts(conn))
                else:
                    self._json({"error": "not found"}, 404)
            finally:
                conn.close()

        def do_POST(self):
            path = urlparse(self.path).path
            if path != "/api/employees":
                self._json({"error": "not found"}, 404)
                return
            try:
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "invalid JSON"}, 400)
                return
            code = str(body.get("code", "")).strip()
            name = str(body.get("name", "")).strip()
            if not code or not name:
                self._json({"error": "code and name required"}, 400)
                return
            conn = dbm.connect(db_path)
            try:
                emp_id = dbm.add_employee(conn, code, name)
                self._json({"id": emp_id, "code": code, "name": name}, 201)
            except Exception:
                self._json({"error": f"employee code {code!r} already exists"}, 400)
            finally:
                conn.close()

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as httpd:
        print(f"dashboard: http://127.0.0.1:{port}  (Ctrl+C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


# ── single-file UI (same look as the cloud dashboard in worker.js) ───────────

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attendance — Local Dashboard</title>
<style>
  :root {
    --bg: #0e1116; --panel: #171c24; --border: #262d38; --text: #e6e9ee;
    --muted: #8b95a5; --accent: #4fa3ff; --ok: #3ecf7a; --warn: #f0a840; --bad: #ef5d5d;
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, "Segoe UI", sans-serif; }
  .layout { display: flex; min-height: 100vh; }
  nav { width: 210px; background: var(--panel); border-right: 1px solid var(--border); padding: 20px 12px; flex-shrink: 0; }
  nav h1 { font-size: 15px; padding: 0 10px 16px; color: var(--accent); }
  nav .sub { font-size: 11.5px; color: var(--muted); padding: 0 10px 14px; }
  nav button { display: block; width: 100%; text-align: left; background: none; border: 0; color: var(--muted);
    padding: 9px 10px; border-radius: 8px; font-size: 14px; cursor: pointer; }
  nav button.active, nav button:hover { background: #202836; color: var(--text); }
  main { flex: 1; padding: 26px 30px; max-width: 1100px; }
  h2 { font-size: 19px; margin-bottom: 16px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 22px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
  .card .num { font-size: 28px; font-weight: 650; }
  .card .lbl { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .card .num.ok { color: var(--ok); } .card .num.warn { color: var(--warn); } .card .num.bad { color: var(--bad); }
  .tablewrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; font-size: 12.5px; text-transform: uppercase; letter-spacing: .04em; }
  tr:last-child td { border-bottom: 0; }
  .pill { padding: 2px 9px; border-radius: 99px; font-size: 12.5px; }
  .pill.in     { background: rgba(62,207,122,.15);  color: var(--ok); }
  .pill.out    { background: rgba(240,168,64,.15);  color: var(--warn); }
  .pill.match  { background: rgba(62,207,122,.15);  color: var(--ok); }
  .pill.buffer { background: rgba(240,168,64,.15);  color: var(--warn); }
  .pill.reject { background: rgba(239,93,93,.15);   color: var(--bad); }
  .note { color: var(--muted); font-size: 13px; margin: 10px 2px; }
  form.inline { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
  input, button.primary { border-radius: 8px; border: 1px solid var(--border); background: #10151d;
    color: var(--text); padding: 9px 12px; font-size: 14px; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #08111f; font-weight: 600; cursor: pointer; }
  .view { display: none; } .view.active { display: block; }
</style>
</head>
<body>
<div class="layout">
  <nav>
    <h1>◉ Attendance</h1>
    <div class="sub">local · attendance.sqlite3</div>
    <button data-view="overview" class="active">Overview</button>
    <button data-view="employees">Employees</button>
    <button data-view="events">Attendance log</button>
    <button data-view="attempts">Match attempts</button>
  </nav>
  <main>
    <section id="overview" class="view active">
      <h2>Overview</h2>
      <div class="cards">
        <div class="card"><div class="num" id="c-emp">–</div><div class="lbl">Employees</div></div>
        <div class="card"><div class="num" id="c-today">–</div><div class="lbl">Check-ins today</div></div>
        <div class="card"><div class="num" id="c-total">–</div><div class="lbl">Events total</div></div>
        <div class="card"><div class="num ok" id="c-match">–</div><div class="lbl">Matches today</div></div>
        <div class="card"><div class="num warn" id="c-buffer">–</div><div class="lbl">Buffer today</div></div>
        <div class="card"><div class="num bad" id="c-reject">–</div><div class="lbl">Rejects today</div></div>
      </div>
      <p class="note">A rising buffer share is the first sign of a lighting or camera problem
        (docs/architecture.md §4). Auto-refreshes every 3 s.</p>
      <h2>Latest events</h2>
      <div class="tablewrap"><table id="t-recent">
        <thead><tr><th>Time (UTC)</th><th>Code</th><th>Name</th><th>Event</th><th>Similarity</th><th>Synced</th></tr></thead>
        <tbody></tbody></table></div>
    </section>

    <section id="employees" class="view">
      <h2>Employees</h2>
      <form class="inline" id="f-emp">
        <input id="f-code" placeholder="Code (E001)" required>
        <input id="f-name" placeholder="Full name" required>
        <button class="primary" type="submit">Add employee</button>
      </form>
      <p class="note" id="emp-msg">After adding, enroll their face at the kiosk:
        <code>python -m app enroll --code E001</code> (webcam capture can't run from the browser).</p>
      <div class="tablewrap"><table id="t-emp">
        <thead><tr><th>Code</th><th>Name</th><th>Status</th><th>Embeddings</th><th>Registered</th></tr></thead>
        <tbody></tbody></table></div>
    </section>

    <section id="events" class="view">
      <h2>Attendance log</h2>
      <div class="tablewrap"><table id="t-events">
        <thead><tr><th>Time (UTC)</th><th>Code</th><th>Name</th><th>Event</th><th>Similarity</th><th>Synced</th></tr></thead>
        <tbody></tbody></table></div>
    </section>

    <section id="attempts" class="view">
      <h2>Match attempts (audit)</h2>
      <p class="note">Every recognition attempt, including buffers and rejects —
        the raw material for threshold calibration.</p>
      <div class="tablewrap"><table id="t-attempts">
        <thead><tr><th>Time (UTC)</th><th>Decision</th><th>Matched</th><th>Similarity</th><th>Margin</th><th>Quality</th></tr></thead>
        <tbody></tbody></table></div>
    </section>
  </main>
</div>
<script>
(function () {
  'use strict';
  var navButtons = document.querySelectorAll('nav button');
  navButtons.forEach(function (b) {
    b.addEventListener('click', function () {
      navButtons.forEach(function (x) { x.classList.remove('active'); });
      document.querySelectorAll('.view').forEach(function (v) { v.classList.remove('active'); });
      b.classList.add('active');
      document.getElementById(b.dataset.view).classList.add('active');
    });
  });

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function get(path) { return fetch(path).then(function (r) { return r.json(); }); }
  function num(v, digits) { return v == null ? '—' : Number(v).toFixed(digits); }

  function eventRow(e) {
    var pill = e.event_type === 'check_in'
      ? '<span class="pill in">check-in</span>' : '<span class="pill out">check-out</span>';
    return '<tr><td>' + esc(e.occurred_at) + '</td><td>' + esc(e.code) + '</td><td>' + esc(e.name) +
      '</td><td>' + pill + '</td><td>' + num(e.similarity, 3) + '</td><td>' +
      (e.synced ? 'yes' : '—') + '</td></tr>';
  }

  function refresh() {
    get('/api/summary').then(function (s) {
      document.getElementById('c-emp').textContent = s.employees;
      document.getElementById('c-today').textContent = s.checkins_today;
      document.getElementById('c-total').textContent = s.events_total;
      var a = s.attempts_today || {};
      document.getElementById('c-match').textContent = a.match || 0;
      document.getElementById('c-buffer').textContent = a.buffer || 0;
      document.getElementById('c-reject').textContent = a.reject || 0;
    });
    get('/api/events').then(function (rows) {
      var html = rows.map(eventRow).join('') || '<tr><td colspan="6">no events yet</td></tr>';
      document.querySelector('#t-events tbody').innerHTML = html;
      document.querySelector('#t-recent tbody').innerHTML =
        rows.slice(0, 8).map(eventRow).join('') || '<tr><td colspan="6">no events yet</td></tr>';
    });
    get('/api/employees').then(function (rows) {
      document.querySelector('#t-emp tbody').innerHTML = rows.map(function (e) {
        return '<tr><td>' + esc(e.code) + '</td><td>' + esc(e.name) + '</td><td>' + esc(e.status) +
          '</td><td>' + e.n_embeddings + '</td><td>' + esc(e.created_at) + '</td></tr>';
      }).join('') || '<tr><td colspan="5">no employees yet</td></tr>';
    });
    get('/api/attempts').then(function (rows) {
      document.querySelector('#t-attempts tbody').innerHTML = rows.map(function (a) {
        var who = a.code ? esc(a.code) + ' ' + esc(a.name) : '—';
        return '<tr><td>' + esc(a.occurred_at) + '</td><td><span class="pill ' + esc(a.decision) +
          '">' + esc(a.decision) + '</span></td><td>' + who + '</td><td>' + num(a.similarity, 3) +
          '</td><td>' + num(a.margin, 3) + '</td><td>' + num(a.quality, 0) + '</td></tr>';
      }).join('') || '<tr><td colspan="6">no attempts yet</td></tr>';
    });
  }
  refresh();
  setInterval(refresh, 3000);

  document.getElementById('f-emp').addEventListener('submit', function (ev) {
    ev.preventDefault();
    fetch('/api/employees', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        code: document.getElementById('f-code').value.trim(),
        name: document.getElementById('f-name').value.trim(),
      }),
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (r) {
        var msg = document.getElementById('emp-msg');
        if (r.ok) {
          ev.target.reset();
          msg.textContent = 'Added ' + r.data.code + '. Now enroll their face at the kiosk: ' +
            'python -m app enroll --code ' + r.data.code;
          refresh();
        } else {
          msg.textContent = r.data.error || 'failed to add employee';
        }
      });
  });
})();
</script>
</body>
</html>"""
