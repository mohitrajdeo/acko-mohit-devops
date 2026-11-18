"""Launchpad control plane — spine build (Gate 2 push).

Flow: register service -> build (git tag -> docker build/push) -> deploy ->
health gate -> auto-rollback -> webhook notifications. HTML UI.
"""
import json
import sqlite3
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request

DB = Path(__file__).parent / "launchpad.db"
HEALTH_RETRIES = 3          # DECISIONS.md: 3 probes x 30s window, my call
HEALTH_INTERVAL = 3         # shortened for demo speed (logged in DECISIONS.md)
WEBHOOK_URL = "http://localhost:9900/hook"
SERVICE_PORT_BASE = 8100    # deployed containers get 8100, 8101, ...

app = Flask(__name__)

# one deploy at a time per (service, environment) — concurrent deploys are rejected
_deploy_locks = {}


def acquire_lock(service_id, environment):
    key = (service_id, environment)
    if _deploy_locks.get(key):
        return False
    _deploy_locks[key] = True
    return True


def release_lock(service_id, environment):
    _deploy_locks[(service_id, environment)] = False


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # migration: add team column to pre-existing databases
    with db() as c:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(services)").fetchall()]
        if "team" not in cols:
            c.execute("ALTER TABLE services ADD COLUMN team TEXT DEFAULT ''")
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS services(
            id INTEGER PRIMARY KEY, name TEXT UNIQUE, repo_path TEXT,
            dockerfile TEXT, port INTEGER, health_path TEXT, team TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS builds(
            id INTEGER PRIMARY KEY, service_id INTEGER, version TEXT,
            status TEXT, log TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS deploys(
            id INTEGER PRIMARY KEY, service_id INTEGER, version TEXT,
            environment TEXT, status TEXT, detail TEXT,
            started_at TEXT, finished_at TEXT);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY, deploy_id INTEGER, type TEXT,
            detail TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS configs(
            id INTEGER PRIMARY KEY, service_id INTEGER, environment TEXT,
            key TEXT, value TEXT);
        """)


def now():
    return datetime.now().isoformat(timespec="seconds")


def notify(event_type, detail):
    """Announce everything — no silent failures (DECISIONS.md)."""
    body = json.dumps({"source": "launchpad", "type": event_type,
                       "detail": detail, "at": now()}).encode()
    try:
        req = urllib.request.Request(WEBHOOK_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[webhook] unreachable: {e}")


def log_event(deploy_id, event_type, detail):
    with db() as c:
        c.execute("INSERT INTO events(deploy_id,type,detail,created_at) VALUES(?,?,?,?)",
                  (deploy_id, event_type, detail, now()))
    notify(event_type, detail)


def run(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str),
                          capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- builds
def do_build(service_id, version):
    with db() as c:
        svc = c.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        cur = c.execute(
            "INSERT INTO builds(service_id,version,status,log,created_at) VALUES(?,?,?,?,?)",
            (service_id, version, "building", "", now()))
        build_id = cur.lastrowid

    log = []
    def step(cmd):
        r = run(cmd)
        log.append(f"$ {cmd}\n{r.stdout}{r.stderr}")
        ok = r.returncode == 0
        with db() as c:
            c.execute("UPDATE builds SET log=? WHERE id=?", ("\n".join(log), build_id))
        return ok

    ok = all([
        step(f"git -C '{svc['repo_path']}' checkout -f {version}"),
        step(f"git -C '{svc['repo_path']}' clean -fd"),
        step(f"docker build -t localhost:5000/{svc['name']}:{version} '{svc['repo_path']}'"),
        step(f"docker push localhost:5000/{svc['name']}:{version}"),
    ])
    with db() as c:
        c.execute("UPDATE builds SET status=? WHERE id=?",
                  ("ok" if ok else "failed", build_id))
    return ok, build_id


# ---------------------------------------------------------------- deploys
def deploy(service_id, version, environment):
    with db() as c:
        svc = c.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        cur = c.execute(
            "INSERT INTO deploys(service_id,version,environment,status,started_at) VALUES(?,?,?,?,?)",
            (service_id, version, environment, "deploying", now()))
        deploy_id = cur.lastrowid
        container = f"lp-{svc['name']}-{environment}"
        env_vars = {r["key"]: r["value"] for r in c.execute(
            "SELECT key,value FROM configs WHERE service_id=? AND environment=?",
            (service_id, environment))}

    if not acquire_lock(service_id, environment):
        _finish_deploy(deploy_id, "rejected",
                       f"a deploy for {svc['name']}/{environment} is already in progress")
        return deploy_id
    log_event(deploy_id, "deploy.started", f"{svc['name']} {version} -> {environment}")

    # unique port per (service, environment) so staging and prod can run side by side
    env_offset = 100 if environment == "prod" else 0
    host_port = SERVICE_PORT_BASE + service_id + env_offset
    env_flags = " ".join(f"-e {k}={v}" for k, v in env_vars.items())
    image = f"localhost:5000/{svc['name']}:{version}"
    # stop (not remove) old container so we can recover if the new one fails
    old_existed = run(f"docker stop {container} 2>/dev/null").returncode == 0
    tmp = f"{container}-new"
    run(f"docker rm -f {tmp} 2>/dev/null")
    r = run(f"docker run -d --name {tmp} {env_flags} "
            f"-p {host_port}:{svc['port']} {image}")
    if r.returncode != 0:
        run(f"docker rm -f {tmp} 2>/dev/null")
        if old_existed:
            run(f"docker start {container}")  # bring prod back on the old version
        _finish_deploy(deploy_id, "failed", r.stderr)
        release_lock(service_id, environment)
        return deploy_id

    # health gate
    healthy = False
    for i in range(1, HEALTH_RETRIES + 1):
        time.sleep(HEALTH_INTERVAL)
        try:
            with urllib.request.urlopen(
                    f"http://localhost:{host_port}{svc['health_path']}", timeout=5) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except Exception:
            pass
        log_event(deploy_id, "health.retry", f"probe {i}/{HEALTH_RETRIES} failed")

    if healthy:
        run(f"docker rm {container} 2>/dev/null")   # old stopped container, superseded
        run(f"docker rename {tmp} {container}")
        _finish_deploy(deploy_id, "healthy", f"port {host_port}")
    else:
        # failed gate: discard new container, restore the previous one
        run(f"docker rm -f {tmp} 2>/dev/null")
        if old_existed:
            run(f"docker start {container}")
            _finish_deploy(deploy_id, "rolled_back", "restored previous running version")
            log_event(deploy_id, "rollback.auto", "restored previous running version")
        else:
            _finish_deploy(deploy_id, "failed", "health gate failed, no previous version")
    release_lock(service_id, environment)
    return deploy_id


def _finish_deploy(deploy_id, status, detail):
    with db() as c:
        c.execute("UPDATE deploys SET status=?, detail=?, finished_at=? WHERE id=?",
                  (status, detail, now(), deploy_id))
    log_event(deploy_id, f"deploy.{status}", detail)


def last_good_version(service_id, environment, exclude_deploy):
    with db() as c:
        row = c.execute(
            "SELECT version FROM deploys WHERE service_id=? AND environment=? "
            "AND status='healthy' AND id<? ORDER BY id DESC LIMIT 1",
            (service_id, environment, exclude_deploy)).fetchone()
    return row["version"] if row else None


def _auto_rollback(svc, failed_deploy_id, environment):
    good = last_good_version(svc["id"], environment, failed_deploy_id)
    if not good:
        _finish_deploy(failed_deploy_id, "rollback_failed",
                       "no previous good version — leaving current state")
        return
    log_event(failed_deploy_id, "rollback.auto", f"redeploying last good: {good}")
    # deploy the good version without recursion into gating
    container = f"lp-{svc['name']}-{environment}"
    env_vars = _get_env_vars(svc["id"], environment)
    env_flags = " ".join(f"-e {k}={v}" for k, v in env_vars.items())
    env_offset = 100 if environment == "prod" else 0
    host_port = SERVICE_PORT_BASE + svc["id"] + env_offset
    run(f"docker rm -f {container} 2>/dev/null")
    r = run(f"docker run -d --name {container} {env_flags} "
            f"-p {host_port}:{svc['port']} localhost:5000/{svc['name']}:{good}")
    ok = r.returncode == 0
    _finish_deploy(failed_deploy_id, "rolled_back" if ok else "rollback_failed",
                   f"recovered to {good}" if ok else r.stderr)
    if ok:
        # record the recovery as its own healthy deploy
        with db() as c:
            c.execute("INSERT INTO deploys(service_id,version,environment,status,detail,started_at,finished_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (svc["id"], good, environment, "healthy", "auto-rollback", now(), now()))


def _get_env_vars(service_id, environment):
    with db() as c:
        return {r["key"]: r["value"] for r in c.execute(
            "SELECT key,value FROM configs WHERE service_id=? AND environment=?",
            (service_id, environment))}


# ---------------------------------------------------------------- API
@app.post("/api/services")
def api_register():
    b = request.json or {}
    with db() as c:
        cur = c.execute("INSERT INTO services(name,repo_path,dockerfile,port,health_path,team) "
                        "VALUES(?,?,?,?,?,?)",
                        (b["name"], b["repo_path"], b.get("dockerfile", "Dockerfile"),
                         b.get("port", 8080), b.get("health_path", "/health"),
                         b.get("team", "")))
    notify("service.registered", b["name"])
    return jsonify(id=cur.lastrowid)


@app.post("/api/services/<int:sid>/build")
def api_build(sid):
    version = (request.json or {}).get("version", "")
    ok, build_id = do_build(sid, version)
    return jsonify(build_id=build_id, ok=ok)


@app.post("/api/services/<int:sid>/deploy")
def api_deploy(sid):
    b = request.json or {}
    did = deploy(sid, b["version"], b.get("environment", "staging"))
    return jsonify(deploy_id=did)


@app.post("/api/services/<int:sid>/promote")
def api_promote(sid):
    """Promote the version currently healthy in staging to prod (one action)."""
    b = request.json or {}
    with db() as c:
        row = c.execute(
            "SELECT version FROM deploys WHERE service_id=? AND environment='staging' "
            "AND status='healthy' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        if not row:
            return jsonify(error="no healthy staging version to promote"), 400
    version = row["version"]
    did = deploy(sid, version, b.get("environment", "prod"))
    return jsonify(deploy_id=did, promoted=version)


@app.post("/api/deploys/<int:did>/rollback")
def api_rollback(did):
    with db() as c:
        d = c.execute("SELECT * FROM deploys WHERE id=?", (did,)).fetchone()
        svc = c.execute("SELECT * FROM services WHERE id=?", (d["service_id"],)).fetchone()
    good = last_good_version(svc["id"], d["environment"], did)
    if not good:
        return jsonify(error="no earlier good version"), 400
    log_event(did, "rollback.manual", f"to {good}")
    deploy(svc["id"], good, d["environment"])
    return jsonify(ok=True, version=good)


@app.get("/api/deploys")
def api_deploys():
    with db() as c:
        rows = c.execute(
            "SELECT d.*, s.name as service, s.team FROM deploys d JOIN services s "
            "ON s.id=d.service_id ORDER BY d.id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/services/<int:sid>/versions")
def api_versions(sid):
    """Git tags available in the repo + recent builds of this service."""
    with db() as c:
        svc = c.execute("SELECT repo_path FROM services WHERE id=?", (sid,)).fetchone()
        if not svc:
            return jsonify(error="unknown service"), 404
    r = run(f"git -C '{svc['repo_path']}' tag --sort=-creatordate")
    tags = [t for t in r.stdout.split() if t]
    with db() as c:
        builds = c.execute(
            "SELECT id, version, status, created_at FROM builds "
            "WHERE service_id=? ORDER BY id DESC LIMIT 10", (sid,)).fetchall()
    return jsonify(tags=tags, builds=[dict(b) for b in builds])


@app.get("/api/builds/<int:bid>/log")
def api_build_log(bid):
    with db() as c:
        b = c.execute("SELECT log FROM builds WHERE id=?", (bid,)).fetchone()
    if not b:
        return jsonify(error="unknown build"), 404
    return jsonify(log=b["log"])


@app.get("/api/services/<int:sid>/all_versions")
def api_all_versions(sid):
    """Every deployable version: git tags + versions we built before (deduped)."""
    with db() as c:
        svc = c.execute("SELECT repo_path FROM services WHERE id=?", (sid,)).fetchone()
        if not svc:
            return jsonify(error="unknown service"), 404
    r = run(f"git -C '{svc['repo_path']}' tag --sort=-creatordate")
    tags = [t for t in r.stdout.split() if t]
    with db() as c:
        built = [row[0] for row in c.execute(
            "SELECT DISTINCT version FROM builds WHERE service_id=? AND status='ok' "
            "ORDER BY version DESC", (sid,)).fetchall()]
    merged = list(dict.fromkeys(tags + built))
    return jsonify({"tags": tags, "built": built, "all": merged})


@app.get("/api/services/<int:sid>/healthy_versions")
def api_healthy_versions(sid):
    """Distinct versions that ever passed the health gate — valid rollback targets."""
    with db() as c:
        rows = c.execute(
            "SELECT DISTINCT version FROM deploys WHERE service_id=? AND status='healthy' "
            "ORDER BY version DESC", (sid,)).fetchall()
    return jsonify([r["version"] for r in rows])


@app.get("/api/preflight")
def api_preflight():
    """Live health of the control plane's dependencies (docker, registry, disk, ports)."""
    import subprocess, json as _json
    from pathlib import Path
    here = Path(__file__).parent
    try:
        r = subprocess.run(["python3", str(here / "preflight.py")],
                           capture_output=True, text=True, timeout=90)
        verdict = _json.loads((here / "preflight.json").read_text())
        return jsonify(verdict)
    except Exception as e:
        return jsonify(status="UNKNOWN", error=str(e)[:120], results=[])


@app.get("/api/metrics")
def api_metrics():
    """Delivery metrics per service: ship frequency, failure rate, MTTR."""
    out = []
    with db() as c:
        for svc in c.execute("SELECT id, name FROM services").fetchall():
            deploys = c.execute(
                "SELECT status, started_at, finished_at, environment FROM deploys "
                "WHERE service_id=? ORDER BY id", (svc["id"],)).fetchall()
            total = len(deploys)
            failed = sum(1 for d in deploys if d["status"] in
                         ("failed", "rolled_back", "rollback_failed"))
            # deploys per day (from first deploy to now)
            days = max(1, _days_span(d["started_at"] for d in deploys))
            # MTTR: for each failure, time until next healthy deploy of same service
            mttrs = []
            for i, d in enumerate(deploys):
                if d["status"] in ("failed", "rolled_back", "rollback_failed"):
                    # recovery via the auto-rollback itself (same deploy row)
                    if d["status"] == "rolled_back" and d["started_at"] and d["finished_at"]:
                        gap = _minutes_between(d["started_at"], d["finished_at"])
                        if gap is not None:
                            mttrs.append(gap)
                        continue
                    # recovery via a later healthy deploy
                    for later in deploys[i + 1:]:
                        if later["status"] == "healthy":
                            gap = _minutes_between(d["finished_at"], later["finished_at"])
                            if gap is not None:
                                mttrs.append(gap)
                            break
            # avg build duration
            builds = c.execute(
                "SELECT started, finished FROM build_timings WHERE service_id=?",
                (svc["id"],)).fetchall() if False else []
            out.append({
                "service": svc["name"],
                "deploys_total": total,
                "deploys_per_day": round(total / days, 1),
                "deploy_failure_pct": round(100 * failed / total, 1) if total else 0.0,
                "mttr_minutes": round(sum(mttrs) / len(mttrs), 1) if mttrs else None,
                "last_status": deploys[-1]["status"] if deploys else None,
            })
    return jsonify(out)


def _days_span(timestamps):
    """Span in days between the first and last timestamp (min 1 day)."""
    import datetime as dt
    ts = sorted(t for t in timestamps if t)
    if len(ts) < 2:
        return 1
    fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        a = dt.datetime.strptime(ts[0][:19], fmt)
        b = dt.datetime.strptime(ts[-1][:19], fmt)
        return max(1, round((b - a).total_seconds() / 86400, 2))
    except ValueError:
        return 1


def _minutes_between(a, b):
    import datetime as dt
    fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        return round((dt.datetime.strptime(b[:19], fmt) -
                      dt.datetime.strptime(a[:19], fmt)).total_seconds() / 60, 1)
    except (ValueError, TypeError):
        return None


@app.get("/api/events")
def api_events():
    team = request.args.get("team", "")
    svc = request.args.get("service", "")
    with db() as c:
        if team or svc:
            rows = c.execute(
                "SELECT e.* FROM events e LEFT JOIN deploys d ON e.deploy_id=d.id "
                "LEFT JOIN services s ON d.service_id=s.id "
                "WHERE (e.deploy_id IS NULL OR (?='' OR s.team=?) AND (?='' OR s.name=?)) "
                "ORDER BY e.id DESC LIMIT 100",
                (team, team, svc, svc)).fetchall()
        else:
            rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/services/<int:sid>/config")
def api_config(sid):
    b = request.json  # {"environment": "staging", "key": "DB_PASSWORD", "value": "x"}
    with db() as c:
        c.execute("INSERT INTO configs(service_id,environment,key,value) VALUES(?,?,?,?)",
                  (sid, b["environment"], b["key"], b["value"]))
    notify("config.set", f"service {sid} {b['environment']} {b['key']}")
    return jsonify(ok=True)


# ---------------------------------------------------------------- UI (plain HTML)
def rows(cursor):
    return "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in cursor)


@app.get("/")
def ui():
    with db() as c:
        svc_rows = c.execute(
            "SELECT s.name, s.id, s.team, COALESCE(d.version,'—') as version, d.environment, d.status "
            "FROM services s LEFT JOIN deploys d ON d.id = "
            "(SELECT id FROM deploys WHERE service_id=s.id ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        ev_rows = c.execute("SELECT created_at,type,detail FROM events ORDER BY id DESC LIMIT 15").fetchall()
        metrics = {r["service"]: r for r in _metrics_rows(c)}
    teams = sorted({r["team"] for r in svc_rows if r["team"]})
    svc_table = ""
    for r in svc_rows:
        m = metrics.get(r["name"], {})
        dep_n = m.get("deploys_total", 0)
        fail = m.get("deploy_failure_pct", 0.0)
        mttr = m.get("mttr_minutes")
        mttr_s = f"{mttr} min" if mttr is not None else "—"
        status = r["status"] or "never deployed"
        svc_table += (
            f"<tr><td>{r['name']}</td><td>{r['team'] or '—'}</td>"
            f"<td>{status}</td>"
            f"<td>{r['version']} ({r['environment'] or '—'})</td>"
            f"<td>{dep_n} deploys · {fail}% failed · MTTR {mttr_s}</td>"
            f"<td class=actions>"
            f"<button onclick=deploySvc({r['id']},'{r['name']}')>Deploy</button> "
            f"<button onclick=promoteSvc({r['id']})>Promote→prod</button> "
            f"<button onclick=rollbackSvc('{r['name']}',{r['id']})>Rollback</button></td></tr>")
    ev_table = "".join(
        f"<tr><td>{r['created_at']}</td><td><b>{r['type']}</b></td>"
        f"<td>{str(r['detail'])[:90]}</td></tr>"
        for r in ev_rows)
    team_opts = "".join(f'<option value="{t}">{t}</option>' for t in teams)
    return PAGE.format(services=svc_table, events=ev_table, teams=team_opts)


def _metrics_rows(c):
    # reuse the metrics computation, but from an existing connection
    out = []
    for svc in c.execute("SELECT id, name FROM services").fetchall():
        deploys = c.execute(
            "SELECT status, started_at, finished_at FROM deploys "
            "WHERE service_id=? ORDER BY id", (svc["id"],)).fetchall()
        total = len(deploys)
        failed = sum(1 for d in deploys if d["status"] in
                     ("failed", "rolled_back", "rollback_failed"))
        mttrs = []
        for i, d in enumerate(deploys):
            if d["status"] == "rolled_back" and d["started_at"] and d["finished_at"]:
                gap = _minutes_between(d["started_at"], d["finished_at"])
                if gap is not None:
                    mttrs.append(gap)
            elif d["status"] in ("failed", "rollback_failed"):
                for later in deploys[i + 1:]:
                    if later["status"] == "healthy":
                        gap = _minutes_between(d["finished_at"], later["finished_at"])
                        if gap is not None:
                            mttrs.append(gap)
                        break
        out.append({"service": svc["name"], "deploys_total": total,
                    "deploy_failure_pct": round(100 * failed / total, 1) if total else 0.0,
                    "mttr_minutes": round(sum(mttrs) / len(mttrs), 1) if mttrs else None})
    return out


PAGE = """<!doctype html><html><head><meta charset=utf-8><meta http-equiv=refresh content=8>
<title>Launchpad</title><style>
body{{font-family:system-ui,sans-serif;margin:24px;background:#fafafa}}
h1{{color:#1a3c6e}} table{{border-collapse:collapse;width:100%;background:#fff;margin-bottom:28px}}
td,th{{border:1px solid #d8d8d8;padding:7px 10px;text-align:left;font-size:14px}}
th{{background:#eef2f7}} .actions button{{margin:2px;padding:4px 10px;cursor:pointer}}
.lead{{color:#555}} code{{background:#eef;padding:1px 4px}}
</style></head><body>
<h1>Launchpad</h1>
<p class=lead>Deploy a version, check it is healthy, roll back in one click if it isn't.
Every action announces itself on the webhook — no silent failures.</p>
<div id=sysstatus style="padding:10px 14px;margin-bottom:20px;border:1px solid #ccc;background:#fff">
  <b>Control plane:</b> <span id=sysline>checking…</span>
  <button style="margin-left:12px" onclick=runPreflight()>Re-run checks</button>
  <details style="margin-top:8px"><summary style="cursor:pointer">Details</summary>
    <table id=sysdetail style="margin-top:8px"></table></details>
</div>
<h2>Services — what's running where</h2>
<table><tr><th>Service</th><th>Team</th><th>Current state</th><th>Version (env)</th><th>Delivery metrics</th><th>Actions</th></tr>
{services}</table>
<h2>Recent events</h2>
<p class=lead>Filter: <select onchange="location.href='/?team='+this.value">
<option value="">All teams</option>{teams}</select>
<span class=lead> · every event announces itself, no silent failures</span></p>
<table><tr><th>At</th><th>Event</th><th>Detail</th></tr>{events}</table>
<script>
async function call(url, body) {{
  const r = await fetch(url, {{method:"POST", headers:{{"Content-Type":"application/json"}},
                               body: JSON.stringify(body || {{}})}});
  const j = await r.json();
  alert(JSON.stringify(j)); location.reload();
}}
async function deploySvc(id, name) {{
  const r = await fetch("/api/services/" + id + "/all_versions");
  const info = await r.json();
  const versions = info.all || [];
  if (!versions.length) {{ alert("No versions found in the repo."); return; }}
  const opts = versions.map(v =>
    `<option value="${{v}}">${{v}}${{info.built.includes(v) ? " (built)" : " (tag)"}}</option>`).join("");
  const v = prompt2("Deploy " + name, "Version", opts);
  if (!v) return;
  const env = prompt2("Deploy " + name + " — environment", "Environment",
                      `<option value="staging">staging</option><option value="prod">prod</option>`);
  if (!env) return;
  call("/api/services/" + id + "/build", {{version: v}}).then(() =>
    call("/api/services/" + id + "/deploy", {{version: v, environment: env}}));
}}

async function loadPreflight() {{
  const r = await fetch("/api/preflight"); const v = await r.json();
  const line = document.getElementById("sysline");
  const det = document.getElementById("sysdetail");
  if (v.status === "READY") {{
    line.innerHTML = "<span style=color:green>● ALL SYSTEMS OK</span> (" + v.total + "/" + v.total + " checks)";
    document.getElementById("sysstatus").style.borderColor = "green";
  }} else {{
    const bad = (v.results || []).filter(x => !x.ok)
      .map(x => x.check + ": " + x.detail).join(" | ");
    line.innerHTML = "<span style=color:red>● DEGRADED</span> — " + bad;
    document.getElementById("sysstatus").style.borderColor = "red";
  }}
  det.innerHTML = "<tr><th>Check</th><th>Result</th></tr>" +
    (v.results || []).map(x =>
      `<tr><td>${{x.check}}</td><td style="color:${{x.ok ? "green" : "red"}}">${{x.ok ? "PASS" : "FAIL"}} — ${{x.detail}}</td></tr>`).join("");
}}
function runPreflight() {{
  document.getElementById("sysline").textContent = "running checks…";
  loadPreflight();
}}
loadPreflight();

function prompt2(title, label, optionsHtml) {{
  return new Promise(resolve => {{
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:9";
    ov.innerHTML = `<div style="background:#fff;padding:18px 22px;border-radius:8px;min-width:320px">
      <b>${{title}}</b><p style="margin:8px 0 4px">${{label}}</p>
      <select id="pp-sel" style="width:100%;padding:6px">${{optionsHtml}}</select>
      <div style="margin-top:12px;text-align:right">
        <button id="pp-cancel">Cancel</button>
        <button id="pp-ok" style="background:#1a3c6e;color:#fff">Select</button></div></div>`;
    document.body.appendChild(ov);
    ov.querySelector("#pp-cancel").onclick = () => {{ ov.remove(); resolve(null); }};
    ov.querySelector("#pp-ok").onclick = () => {{ const v = ov.querySelector("#pp-sel").value; ov.remove(); resolve(v); }};
  }});
}}
async function rollbackSvc(name, id) {{
  const r = await fetch("/api/services/" + id + "/healthy_versions");
  const versions = await r.json();
  if (!versions.length) {{ alert("No healthy version to roll back to."); return; }}
  const opts = versions.map(v => `<option value="${{v}}">${{v}}</option>`).join("");
  const v = await prompt2("Roll back " + name, "Target version (healthy only)", opts);
  if (!v) return;
  const dr = await fetch("/api/deploys"); const deploys = await dr.json();
  const d = deploys.find(x => x.service === name);
  if (d) call("/api/deploys/" + d.id + "/rollback", {{version: v}});
}}
function promoteSvc(id) {{ if (confirm("Promote current staging version to prod?")) call("/api/services/" + id + "/promote", {{}}); }}
async function rollbackSvc(name) {{
  const r = await fetch("/api/deploys"); const deploys = await r.json();
  const d = deploys.find(x => x.service === name);
  if (d && confirm("Roll back " + name + " to the last good version?")) call("/api/deploys/" + d.id + "/rollback", {{}});
}}
</script></body></html>"""


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=7000)
