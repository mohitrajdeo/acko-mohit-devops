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


def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS services(
            id INTEGER PRIMARY KEY, name TEXT UNIQUE, repo_path TEXT,
            dockerfile TEXT, port INTEGER, health_path TEXT);
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
    host_port = SERVICE_PORT_BASE + svc["id"]
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
        cur = c.execute("INSERT INTO services(name,repo_path,dockerfile,port,health_path) "
                        "VALUES(?,?,?,?,?)",
                        (b["name"], b["repo_path"], b.get("dockerfile", "Dockerfile"),
                         b.get("port", 8080), b.get("health_path", "/health")))
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
            "SELECT d.*, s.name as service FROM deploys d JOIN services s "
            "ON s.id=d.service_id ORDER BY d.id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/events")
def api_events():
    with db() as c:
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
            "SELECT s.name, COALESCE(d.version,'—') as running, d.environment, d.status "
            "FROM services s LEFT JOIN deploys d ON d.id = "
            "(SELECT id FROM deploys WHERE service_id=s.id ORDER BY id DESC LIMIT 1)"
        ).fetchall()
        ev_rows = c.execute("SELECT created_at,type,detail FROM events ORDER BY id DESC LIMIT 15").fetchall()
    table1 = "".join(f"<tr><td>{r['name']}</td><td>{r['running']}</td>"
                     f"<td>{r['environment'] or '—'}</td><td>{r['status'] or '—'}</td></tr>"
                     for r in svc_rows)
    table2 = "".join(f"<tr><td>{r['created_at']}</td><td>{r['type']}</td><td>{r['detail']}</td></tr>"
                     for r in ev_rows)
    return f"""<!doctype html><html><head><meta charset=utf-8><meta http-equiv=refresh content=5>
<title>Launchpad</title><style>body{{font-family:system-ui;margin:24px}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:6px}}</style></head>
<body><h1>🚀 Launchpad</h1>
<h2>What's running where</h2><table><tr><th>Service</th><th>Version</th><th>Env</th><th>Status</th></tr>{table1}</table>
<h2>Recent events</h2><table><tr><th>At</th><th>Event</th><th>Detail</th></tr>{table2}</table>
<p>API: POST /api/services · /api/services/&lt;id&gt;/build · /api/services/&lt;id&gt;/deploy · /api/deploys/&lt;id&gt;/rollback</p>
</body></html>"""


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=7000)
