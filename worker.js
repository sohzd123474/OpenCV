/**
 * Face Recognition Attendance — admin dashboard + API (Cloudflare Worker).
 *
 * Routes:
 *   GET  /                → dashboard UI (single-file, no external assets)
 *   GET  /api/summary     → { employees, events_total, checkins_today, storage }
 *   GET  /api/employees   → [{ id, code, name, created_at }]
 *   POST /api/employees   → { code, name }                  (write-auth if key set)
 *   GET  /api/events      → latest events (?limit=100)
 *   POST /api/checkin     → { employee_code, event_type, occurred_at, similarity, device_id }
 *   POST /api/copilot     → Learning-tab AI tutor: { messages } → { reply }
 *
 * Storage: D1 when a DB binding exists (see wrangler.toml), otherwise an
 * in-memory per-isolate store — good for demos, resets on redeploy/eviction.
 *
 * Copilot: LM_API_BASE + LM_MODEL are plain vars; the token arrives via the
 * Secrets Store binding LM_API_TOKEN. bindingValue() accepts either a plain
 * string or a Secrets Store binding (async .get()). With no token bound this
 * endpoint returns 503 and the UI falls back to its built-in explainers.
 */

const JSON_HEADERS = { 'content-type': 'application/json;charset=utf-8' };

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === '/' || url.pathname === '/index.html') {
        return new Response(PAGE, { headers: { 'content-type': 'text/html;charset=utf-8' } });
      }
      if (url.pathname.startsWith('/api/')) return await handleApi(request, env, url);
      return json({ error: 'not found' }, 404);
    } catch (err) {
      return json({ error: String(err && err.message || err) }, 500);
    }
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: JSON_HEADERS });
}

/** Read a binding that is either a plain string var or a Secrets Store binding. */
async function bindingValue(binding) {
  if (!binding) return null;
  if (typeof binding === 'string') return binding;
  if (typeof binding.get === 'function') return await binding.get();
  return null;
}

/** Write endpoints require a bearer key only when DASHBOARD_API_KEY is bound. */
async function writeAuthError(request, env) {
  const key = await bindingValue(env.DASHBOARD_API_KEY);
  if (!key) return null; // demo mode: no key configured
  const got = (request.headers.get('authorization') || '').replace(/^Bearer\s+/i, '');
  return got === key ? null : json({ error: 'unauthorized' }, 401);
}

// ── API routing ──────────────────────────────────────────────────────────────

async function handleApi(request, env, url) {
  const path = url.pathname;
  if (path === '/api/copilot' && request.method === 'POST') return handleCopilot(request, env);

  const store = await getStore(env);
  if (path === '/api/summary' && request.method === 'GET') return json(await store.summary());
  if (path === '/api/employees' && request.method === 'GET') return json(await store.employees());

  if (path === '/api/employees' && request.method === 'POST') {
    const denied = await writeAuthError(request, env);
    if (denied) return denied;
    const body = await request.json().catch(() => ({}));
    if (!body.code || !body.name) return json({ error: 'code and name required' }, 400);
    return json(await store.addEmployee(String(body.code), String(body.name)), 201);
  }

  if (path === '/api/events' && request.method === 'GET') {
    const limit = Math.min(Number(url.searchParams.get('limit')) || 100, 500);
    return json(await store.events(limit));
  }

  if (path === '/api/checkin' && request.method === 'POST') {
    const denied = await writeAuthError(request, env);
    if (denied) return denied;
    const body = await request.json().catch(() => ({}));
    if (!body.employee_code) return json({ error: 'employee_code required' }, 400);
    if (!['check_in', 'check_out'].includes(body.event_type)) {
      return json({ error: 'event_type must be check_in or check_out' }, 400);
    }
    const event = {
      employee_code: String(body.employee_code),
      event_type: body.event_type,
      occurred_at: body.occurred_at || new Date().toISOString(),
      similarity: typeof body.similarity === 'number' ? body.similarity : null,
      device_id: body.device_id ? String(body.device_id) : null,
    };
    return json(await store.addEvent(event), 201);
  }

  return json({ error: 'not found' }, 404);
}

// ── Learning-tab AI tutor ────────────────────────────────────────────────────

const TUTOR_SYSTEM =
  'You are the built-in tutor for an OpenCV face-recognition attendance system. ' +
  'The stack: YuNet detection, SFace/ArcFace embeddings (L2-normalized, cosine matching), ' +
  'a three-zone threshold decision engine (accept / rejection-buffer / reject with a ' +
  'top-2 margin guard), multi-reference enrollment, liveness anti-spoofing, SQLite/Postgres ' +
  'storage, FAISS-to-Milvus vector search scaling, and GDPR Art. 9 biometric compliance. ' +
  'Answer questions about how the system works, computer vision concepts, and privacy ' +
  'obligations. Be concise and concrete.';

async function handleCopilot(request, env) {
  const token = await bindingValue(env.LM_API_TOKEN);
  if (!token) return json({ error: 'copilot unavailable: no LM token bound' }, 503);

  const body = await request.json().catch(() => ({}));
  const messages = Array.isArray(body.messages) ? body.messages.slice(-10) : null;
  if (!messages || !messages.length) return json({ error: 'messages required' }, 400);

  const resp = await fetch(env.LM_API_BASE.replace(/\/$/, '') + '/chat/completions', {
    method: 'POST',
    headers: { authorization: 'Bearer ' + token, 'content-type': 'application/json' },
    body: JSON.stringify({
      model: env.LM_MODEL,
      messages: [{ role: 'system', content: TUTOR_SYSTEM }, ...messages],
      max_tokens: 700,
      temperature: 0.3,
    }),
  });
  if (!resp.ok) return json({ error: 'upstream error ' + resp.status }, 502);
  const data = await resp.json();
  const reply = data.choices && data.choices[0] && data.choices[0].message
    ? data.choices[0].message.content : '';
  return json({ reply });
}

// ── storage: D1 when bound, in-memory otherwise ─────────────────────────────

let memory = null;   // per-isolate demo store
let d1Ready = false;

async function getStore(env) {
  if (env.DB) return d1Store(env.DB);
  if (!memory) memory = { employees: [], events: [], nextEmp: 1, nextEvt: 1 };
  const m = memory;
  const today = () => new Date().toISOString().slice(0, 10);
  return {
    async summary() {
      return {
        employees: m.employees.length,
        events_total: m.events.length,
        checkins_today: m.events.filter(
          (e) => e.event_type === 'check_in' && String(e.occurred_at).startsWith(today())
        ).length,
        storage: 'memory (demo — bind a D1 database in wrangler.toml for persistence)',
      };
    },
    async employees() {
      return [...m.employees].sort((a, b) => a.code.localeCompare(b.code));
    },
    async addEmployee(code, name) {
      if (m.employees.some((e) => e.code === code)) throw new Error('duplicate employee code');
      const row = { id: m.nextEmp++, code, name, created_at: new Date().toISOString() };
      m.employees.push(row);
      return row;
    },
    async events(limit) {
      return [...m.events]
        .sort((a, b) => b.occurred_at.localeCompare(a.occurred_at))
        .slice(0, limit);
    },
    async addEvent(event) {
      const row = { id: m.nextEvt++, ...event };
      m.events.push(row);
      return row;
    },
  };
}

function d1Store(db) {
  const ensure = async () => {
    if (d1Ready) return;
    await db.batch([
      db.prepare(
        'CREATE TABLE IF NOT EXISTS employees (id INTEGER PRIMARY KEY AUTOINCREMENT, ' +
        'code TEXT NOT NULL UNIQUE, name TEXT NOT NULL, created_at TEXT NOT NULL)'
      ),
      db.prepare(
        'CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ' +
        'employee_code TEXT NOT NULL, event_type TEXT NOT NULL, occurred_at TEXT NOT NULL, ' +
        'similarity REAL, device_id TEXT)'
      ),
    ]);
    d1Ready = true;
  };
  return {
    async summary() {
      await ensure();
      const employees = await db.prepare('SELECT COUNT(*) AS n FROM employees').first('n');
      const total = await db.prepare('SELECT COUNT(*) AS n FROM events').first('n');
      const todayCount = await db
        .prepare("SELECT COUNT(*) AS n FROM events WHERE event_type='check_in' AND occurred_at >= ?")
        .bind(new Date().toISOString().slice(0, 10))
        .first('n');
      return { employees, events_total: total, checkins_today: todayCount, storage: 'D1' };
    },
    async employees() {
      await ensure();
      const { results } = await db.prepare('SELECT * FROM employees ORDER BY code').all();
      return results;
    },
    async addEmployee(code, name) {
      await ensure();
      const created = new Date().toISOString();
      const res = await db
        .prepare('INSERT INTO employees(code, name, created_at) VALUES (?,?,?)')
        .bind(code, name, created)
        .run();
      return { id: res.meta.last_row_id, code, name, created_at: created };
    },
    async events(limit) {
      await ensure();
      const { results } = await db
        .prepare('SELECT * FROM events ORDER BY occurred_at DESC, id DESC LIMIT ?')
        .bind(limit)
        .all();
      return results;
    },
    async addEvent(event) {
      await ensure();
      const res = await db
        .prepare(
          'INSERT INTO events(employee_code, event_type, occurred_at, similarity, device_id) ' +
          'VALUES (?,?,?,?,?)'
        )
        .bind(event.employee_code, event.event_type, event.occurred_at, event.similarity, event.device_id)
        .run();
      return { id: res.meta.last_row_id, ...event };
    },
  };
}

// ── dashboard UI ─────────────────────────────────────────────────────────────
// No template interpolation below; the client script uses string concatenation
// only, so this stays a single dependency-free file.

const PAGE = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attendance Dashboard</title>
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
  nav button { display: block; width: 100%; text-align: left; background: none; border: 0; color: var(--muted);
    padding: 9px 10px; border-radius: 8px; font-size: 14px; cursor: pointer; }
  nav button.active, nav button:hover { background: #202836; color: var(--text); }
  main { flex: 1; padding: 26px 30px; max-width: 1060px; }
  h2 { font-size: 19px; margin-bottom: 16px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 22px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
  .card .num { font-size: 30px; font-weight: 650; }
  .card .lbl { color: var(--muted); font-size: 13px; margin-top: 2px; }
  .tablewrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; font-size: 12.5px; text-transform: uppercase; letter-spacing: .04em; }
  tr:last-child td { border-bottom: 0; }
  .pill { padding: 2px 9px; border-radius: 99px; font-size: 12.5px; }
  .pill.in  { background: rgba(62,207,122,.15); color: var(--ok); }
  .pill.out { background: rgba(240,168,64,.15); color: var(--warn); }
  form.inline { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
  input, button.primary { border-radius: 8px; border: 1px solid var(--border); background: #10151d;
    color: var(--text); padding: 9px 12px; font-size: 14px; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #08111f; font-weight: 600; cursor: pointer; }
  .note { color: var(--muted); font-size: 13px; margin: 10px 2px; }
  #chatlog { display: flex; flex-direction: column; gap: 10px; margin-bottom: 14px; }
  .msg { max-width: 78%; padding: 10px 14px; border-radius: 12px; white-space: pre-wrap; }
  .msg.user { align-self: flex-end; background: #24446b; }
  .msg.bot  { align-self: flex-start; background: var(--panel); border: 1px solid var(--border); }
  .msg.bot.offline { border-style: dashed; }
  .view { display: none; } .view.active { display: block; }
  .err { color: var(--bad); }
</style>
</head>
<body>
<div class="layout">
  <nav>
    <h1>◉ Attendance</h1>
    <button data-view="overview" class="active">Overview</button>
    <button data-view="employees">Employees</button>
    <button data-view="events">Attendance log</button>
    <button data-view="learning">Learning · AI tutor</button>
  </nav>
  <main>
    <section id="overview" class="view active">
      <h2>Overview</h2>
      <div class="cards">
        <div class="card"><div class="num" id="c-emp">–</div><div class="lbl">Employees</div></div>
        <div class="card"><div class="num" id="c-today">–</div><div class="lbl">Check-ins today</div></div>
        <div class="card"><div class="num" id="c-total">–</div><div class="lbl">Events total</div></div>
      </div>
      <p class="note" id="storage-note"></p>
      <h2>Latest events</h2>
      <div class="tablewrap"><table id="t-recent">
        <thead><tr><th>Time (UTC)</th><th>Employee</th><th>Event</th><th>Similarity</th><th>Device</th></tr></thead>
        <tbody></tbody></table></div>
    </section>

    <section id="employees" class="view">
      <h2>Employees</h2>
      <form class="inline" id="f-emp">
        <input id="f-code" placeholder="Code (E001)" required>
        <input id="f-name" placeholder="Full name" required>
        <button class="primary" type="submit">Add employee</button>
      </form>
      <p class="note">Face enrollment happens at the kiosk: <code>python -m app enroll --code E001</code>.
        Biometric embeddings never pass through this dashboard.</p>
      <div class="tablewrap"><table id="t-emp">
        <thead><tr><th>Code</th><th>Name</th><th>Registered</th></tr></thead>
        <tbody></tbody></table></div>
    </section>

    <section id="events" class="view">
      <h2>Attendance log</h2>
      <div class="tablewrap"><table id="t-events">
        <thead><tr><th>Time (UTC)</th><th>Employee</th><th>Event</th><th>Similarity</th><th>Device</th></tr></thead>
        <tbody></tbody></table></div>
    </section>

    <section id="learning" class="view">
      <h2>Learning — AI tutor</h2>
      <p class="note">Ask how the system works: thresholds, embeddings, liveness, GDPR.
        Falls back to built-in explainers when no LM token is configured.</p>
      <div id="chatlog"></div>
      <form class="inline" id="f-chat">
        <input id="f-q" placeholder="e.g. why is there a rejection buffer?" style="flex:1" required>
        <button class="primary" type="submit">Ask</button>
      </form>
    </section>
  </main>
</div>
<script>
(function () {
  'use strict';

  // ── tabs ──
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
  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().then(function (data) { return { ok: r.ok, status: r.status, data: data }; });
    });
  }
  function eventRow(e) {
    var pill = e.event_type === 'check_in'
      ? '<span class="pill in">check-in</span>' : '<span class="pill out">check-out</span>';
    var sim = (e.similarity == null) ? '—' : Number(e.similarity).toFixed(3);
    return '<tr><td>' + esc(e.occurred_at) + '</td><td>' + esc(e.employee_code) + '</td><td>' +
      pill + '</td><td>' + sim + '</td><td>' + esc(e.device_id || '—') + '</td></tr>';
  }

  function refresh() {
    api('/api/summary').then(function (r) {
      if (!r.ok) return;
      document.getElementById('c-emp').textContent = r.data.employees;
      document.getElementById('c-today').textContent = r.data.checkins_today;
      document.getElementById('c-total').textContent = r.data.events_total;
      document.getElementById('storage-note').textContent = 'Storage: ' + r.data.storage;
    });
    api('/api/events?limit=100').then(function (r) {
      if (!r.ok) return;
      var rows = r.data.map(eventRow).join('');
      document.querySelector('#t-events tbody').innerHTML = rows || '<tr><td colspan="5">no events yet</td></tr>';
      document.querySelector('#t-recent tbody').innerHTML =
        r.data.slice(0, 8).map(eventRow).join('') || '<tr><td colspan="5">no events yet</td></tr>';
    });
    api('/api/employees').then(function (r) {
      if (!r.ok) return;
      document.querySelector('#t-emp tbody').innerHTML = r.data.map(function (e) {
        return '<tr><td>' + esc(e.code) + '</td><td>' + esc(e.name) + '</td><td>' +
          esc(e.created_at) + '</td></tr>';
      }).join('') || '<tr><td colspan="3">no employees yet</td></tr>';
    });
  }
  refresh();
  setInterval(refresh, 15000);

  document.getElementById('f-emp').addEventListener('submit', function (ev) {
    ev.preventDefault();
    api('/api/employees', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        code: document.getElementById('f-code').value.trim(),
        name: document.getElementById('f-name').value.trim(),
      }),
    }).then(function (r) {
      if (r.ok) { ev.target.reset(); refresh(); }
      else alert(r.data.error || 'failed');
    });
  });

  // ── Learning tab: AI tutor with built-in fallback explainers ──
  var EXPLAINERS = [
    [/threshold|buffer|zone|accept|reject/i,
      'Matching uses cosine similarity between L2-normalized embeddings with three zones: ' +
      'above the accept threshold it records attendance; between reject and accept it lands in ' +
      'the rejection buffer, which triggers secondary verification (re-capture, active liveness, ' +
      'then badge/PIN fallback) instead of guessing; below reject it is unknown. A top-2 margin ' +
      'guard also sends look-alike matches to the buffer. This keeps the false-acceptance rate ' +
      'low without hard-blocking anyone on a false reject.'],
    [/liveness|spoof|photo attack|mask/i,
      'Liveness detection stops spoofing: a passive CNN scores every frame for print/screen ' +
      'attacks, and an active blink or head-turn challenge fires when confidence is uncertain. ' +
      'High-security sites add IR/depth cameras. In this repo the module is a documented ' +
      'integration point (docs/architecture.md §2.3) — the demo pipeline does not yet enforce it.'],
    [/embedding|vector|arcface|sface|cosine/i,
      'A face is converted to a fixed-length numeric vector (128-D SFace here; 512-D ArcFace in ' +
      'the production design). Vectors are L2-normalized so cosine similarity is a dot product. ' +
      'Similar faces produce nearby vectors; matching means finding the enrolled vector with the ' +
      'highest similarity. Raw images are not needed after enrollment.'],
    [/gdpr|privacy|consent|delete|erasure|biometric/i,
      'Embeddings are still biometric data under GDPR Art. 9, so the design requires explicit ' +
      'consent with a permanent badge/PIN alternative, purpose limitation, retention limits, and ' +
      'a right-to-erasure workflow that purges DB rows, photos, and vector-index entries ' +
      '(see docs/security-privacy.md).'],
    [/enroll|onboard|reference|register/i,
      'Enrollment captures ~8 quality-checked reference images at slightly different angles and ' +
      'stores one embedding per image. Matching takes the max similarity across the set, which ' +
      'makes recognition robust to pose and lighting changes. Run: python -m app enroll --code E001'],
    [/faiss|milvus|scale|1:n|search/i,
      'For 1:N search the design starts with exact FAISS IndexFlatIP in-process (<10k people), ' +
      'moves to FAISS HNSW to ~100k, then Milvus for multi-site scale. Postgres/SQLite keeps the ' +
      'authoritative embedding copies, so the vector index is always a rebuildable cache.'],
  ];
  var FALLBACK_DEFAULT =
    'Built-in explainer topics: thresholds and the rejection buffer, embeddings, liveness, ' +
    'enrollment, vector search scaling, and GDPR/privacy. Ask about one of those, or bind an ' +
    'LM token (wrangler.toml) to enable the full AI tutor.';
  function explain(q) {
    for (var i = 0; i < EXPLAINERS.length; i++) {
      if (EXPLAINERS[i][0].test(q)) return EXPLAINERS[i][1];
    }
    return FALLBACK_DEFAULT;
  }

  var history = [];
  function addMsg(text, cls) {
    var div = document.createElement('div');
    div.className = 'msg ' + cls;
    div.textContent = text;
    document.getElementById('chatlog').appendChild(div);
    div.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }
  document.getElementById('f-chat').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var input = document.getElementById('f-q');
    var q = input.value.trim();
    if (!q) return;
    input.value = '';
    addMsg(q, 'user');
    history.push({ role: 'user', content: q });
    api('/api/copilot', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ messages: history }),
    }).then(function (r) {
      if (r.ok && r.data.reply) {
        history.push({ role: 'assistant', content: r.data.reply });
        addMsg(r.data.reply, 'bot');
      } else {
        var ans = explain(q);
        history.push({ role: 'assistant', content: ans });
        addMsg(ans, 'bot offline');
      }
    }).catch(function () {
      addMsg(explain(q), 'bot offline');
    });
  });
})();
</script>
</body>
</html>`;
