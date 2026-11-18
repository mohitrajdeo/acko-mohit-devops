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
HEALTH_RETRIES = 3
DEPLOY_QUEUE_WAIT = 180     # max seconds a queued deploy waits for the lock          # DECISIONS.md: 3 probes x 30s window, my call
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
    # migrations for pre-existing databases
    with db() as c:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(services)").fetchall()]
        if "team" not in cols:
            c.execute("ALTER TABLE services ADD COLUMN team TEXT DEFAULT ''")
    with db() as c:
        dcols = [r["name"] for r in c.execute("PRAGMA table_info(deploys)").fetchall()]
        if "env_snapshot" not in dcols:
            c.execute("ALTER TABLE deploys ADD COLUMN env_snapshot TEXT DEFAULT ''")
        if "failure_stage" not in dcols:
            c.execute("ALTER TABLE deploys ADD COLUMN failure_stage TEXT DEFAULT ''")
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT UNIQUE,
            password_hash TEXT, team TEXT, role TEXT)""")  # role: engineer|manager
        c.execute("""CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY, user_id INTEGER, created_at TEXT)""")
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
        CREATE TABLE IF NOT EXISTS pointers(
            service_id INTEGER, environment TEXT,
            version TEXT, container TEXT,
            updated_at TEXT,
            PRIMARY KEY (service_id, environment));
        """)


def now():
    return datetime.now().isoformat(timespec="seconds")


SEVERITY = {
    "deploy.started": "info", "deploy.healthy": "info", "service.registered": "info",
    "config.set": "info", "user.login": "info", "user.registered": "info",
    "health.retry": "warning", "deploy.rejected": "warning",
    "health.gate_failed": "alert", "health.failure_stage": "alert",
    "deploy.failed": "alert", "rollback.auto": "alert",
    "rollback.manual": "alert", "deploy.rolled_back": "alert",
    "deploy.rollback_failed": "alert",
}


def notify(event_type, detail, extra=None):
    """Announce everything — no silent failures (DECISIONS.md).
    Structured payload: severity lets a real chat router pick channels."""
    body = json.dumps({
        "source": "launchpad", "type": event_type,
        "severity": SEVERITY.get(event_type, "info"),
        "detail": detail, "at": now(), **(extra or {}),
    }).encode()
    try:
        req = urllib.request.Request(WEBHOOK_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[webhook] unreachable: {e}")


def log_event(deploy_id, event_type, detail, extra=None):
    with db() as c:
        c.execute("INSERT INTO events(deploy_id,type,detail,created_at) VALUES(?,?,?,?)",
                  (deploy_id, event_type, detail, now()))
        ctx = {}
        if deploy_id:
            d = c.execute(
                "SELECT d.version, d.environment, s.name FROM deploys d "
                "JOIN services s ON s.id=d.service_id WHERE d.id=?",
                (deploy_id,)).fetchone()
            if d:
                ctx = {"service": d["name"], "version": d["version"],
                       "environment": d["environment"]}
    notify(event_type, detail, {**ctx, **(extra or {})})


def run(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str),
                          capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- auth (stdlib hash)
import hashlib
import secrets


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return f"{salt}${hashlib.sha256((salt + pw).encode()).hexdigest()}"


def verify_pw(pw, stored):
    try:
        salt, _ = stored.split("$", 1)
        return secrets.compare_digest(hash_pw(pw, salt), stored)
    except (ValueError, AttributeError):
        return False


def current_user():
    """Session token -> user row (None if logged out)."""
    token = request.headers.get("X-Session") or request.args.get("session", "")
    if not token:
        return None
    with db() as c:
        return c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,)).fetchone()


def require_user(min_role=None):
    u = current_user()
    if not u:
        return None, (jsonify(error="login required"), 401)
    if min_role == "manager" and u["role"] != "manager":
        return None, (jsonify(error="manager role required"), 403)
    return u, None


def team_scope(u):
    """SQL clause for team filtering: manager sees all, engineer sees own team."""
    if u["role"] == "manager" or not u["team"]:
        return "", ()
    return " AND s.team=?", (u["team"],)


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

    # fail fast: never continue (esp. never docker build/push) after a failed step —
    # a failed checkout must not produce a mistagged image
    ok = True
    for cmd in (
            f"git -C '{svc['repo_path']}' checkout -f {version}",
            f"git -C '{svc['repo_path']}' clean -fd",
            f"docker build -t localhost:5000/{svc['name']}:{version} '{svc['repo_path']}'",
            f"docker push localhost:5000/{svc['name']}:{version}"):
        if not step(cmd):
            log.append(f"! build aborted: step failed -> {cmd.split()[0]}")
            with db() as c:
                c.execute("UPDATE builds SET log=? WHERE id=?",
                          ("\n".join(log), build_id))
            ok = False
            break
    with db() as c:
        c.execute("UPDATE builds SET status=? WHERE id=?",
                  ("ok" if ok else "failed", build_id))
    return ok, build_id


# ---------------------------------------------------------------- deploys
def deploy(service_id, version, environment):
    with db() as c:
        svc = c.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
        # snapshot the env vars AT DEPLOY TIME — rollback restores THIS, not the
        # live table (protects against config rotation between good deploy and failure)
        env_vars = {r["key"]: r["value"] for r in c.execute(
            "SELECT key,value FROM configs WHERE service_id=? AND environment=?",
            (service_id, environment))}
        cur = c.execute(
            "INSERT INTO deploys(service_id,version,environment,status,started_at,env_snapshot) "
            "VALUES(?,?,?,?,?,?)",
            (service_id, version, environment, "deploying", now(),
             json.dumps(env_vars)))
        deploy_id = cur.lastrowid
        container = f"lp-{svc['name']}-{environment}"

    # deploy queue: a concurrent deploy WAITS for the running one (queued events),
    # instead of being rejected outright
    waited = 0
    while not acquire_lock(service_id, environment):
        if waited == 0:
            log_event(deploy_id, "deploy.queued",
                      f"waiting: a deploy for {svc['name']}/{environment} is in progress")
        if waited >= DEPLOY_QUEUE_WAIT:
            _finish_deploy(deploy_id, "rejected",
                           f"queued too long (> {DEPLOY_QUEUE_WAIT}s) for "
                           f"{svc['name']}/{environment}")
            return deploy_id
        time.sleep(2)
        waited += 2
    if waited:
        log_event(deploy_id, "deploy.dequeued",
                  f"lock acquired after {waited}s wait — deploying now")
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

    # health gate with failure-stage telemetry: WHERE did it turn red?
    healthy = False
    ever_reachable = False
    consecutive_ok = 0
    for i in range(1, HEALTH_RETRIES + 1):
        time.sleep(HEALTH_INTERVAL)
        try:
            with urllib.request.urlopen(
                    f"http://localhost:{host_port}{svc['health_path']}", timeout=5) as resp:
                if resp.status == 200:
                    healthy = True
                    break
                ever_reachable = True   # got a real HTTP answer (e.g. 503 "warming")
        except Exception:
            pass
        log_event(deploy_id, "health.retry", f"probe {i}/{HEALTH_RETRIES} failed")
    if not healthy:
        # container state decides the stage
        st = run(f"docker inspect -f '{{{{.State.Status}}}}' {tmp} 2>/dev/null").stdout.strip()
        if st != "running":
            stage = "never_started"          # image/port/entrypoint problem
        elif not ever_reachable:
            stage = "startup_unreachable"    # app crashes before binding (config/startup)
        else:
            stage = "never_healthy"          # reachable but never 200 (e.g. stuck warming)
        with db() as c:
            c.execute("UPDATE deploys SET failure_stage=? WHERE id=?", (stage, deploy_id))
        log_event(deploy_id, "health.failure_stage",
                  f"{stage} — " + {
                      "never_started": "container did not start (image/port/entrypoint)",
                      "startup_unreachable": "app crashed during startup (check config/secrets)",
                      "never_healthy": "reachable but never healthy (stuck startup step)",
                  }[stage])

    if healthy:
        run(f"docker rm {container} 2>/dev/null")   # old stopped container, superseded
        run(f"docker rename {tmp} {container}")
        _set_pointer(service_id, environment, version, container)  # pointer move
        _finish_deploy(deploy_id, "healthy", f"port {host_port}")
    else:
        # failed gate: discard new container, restore the previous one
        run(f"docker rm -f {tmp} 2>/dev/null")
        if old_existed:
            run(f"docker start {container}")
            prev, prev_snap = last_good_version(service_id, environment, deploy_id)
            _set_pointer(service_id, environment, prev, container)
            _finish_deploy(deploy_id, "rolled_back", "restored previous running version",
                           effective_version=prev)
            log_event(deploy_id, "rollback.auto", "restored previous running version")
        else:
            _finish_deploy(deploy_id, "failed", "health gate failed, no previous version")
    release_lock(service_id, environment)
    return deploy_id


def _finish_deploy(deploy_id, status, detail, effective_version=None):
    with db() as c:
        c.execute("UPDATE deploys SET status=?, detail=?, finished_at=? WHERE id=?",
                  (status, detail, now(), deploy_id))
        if effective_version:
            # for rollbacks: the version actually running after recovery
            c.execute("UPDATE deploys SET version=? WHERE id=?",
                      (effective_version, deploy_id))
    log_event(deploy_id, f"deploy.{status}", detail)


def last_good_version(service_id, environment, exclude_deploy):
    """Returns (version, env_snapshot) of the most recent healthy deploy."""
    with db() as c:
        row = c.execute(
            "SELECT version, env_snapshot FROM deploys WHERE service_id=? AND environment=? "
            "AND status='healthy' AND id<? ORDER BY id DESC LIMIT 1",
            (service_id, environment, exclude_deploy)).fetchone()
    if not row:
        return None, None
    snap = json.loads(row["env_snapshot"]) if row["env_snapshot"] else None
    return row["version"], snap


def _auto_rollback(svc, failed_deploy_id, environment):
    good, snapshot = last_good_version(svc["id"], environment, failed_deploy_id)
    if not good:
        _finish_deploy(failed_deploy_id, "rollback_failed",
                       "no previous good version — leaving current state")
        return
    # prefer the deploy-time snapshot (config as it was when this version was healthy);
    # fall back to live table only for very old rows without snapshots
    env_vars = snapshot if snapshot is not None else _get_env_vars(svc["id"], environment)
    log_event(failed_deploy_id, "rollback.auto",
              f"pointer -> {good} (config snapshot {'deploy-time' if snapshot is not None else 'live'})")
    container = f"lp-{svc['name']}-{environment}"
    env_flags = " ".join(f"-e {k}={v}" for k, v in env_vars.items())
    env_offset = 100 if environment == "prod" else 0
    host_port = SERVICE_PORT_BASE + svc["id"] + env_offset
    run(f"docker rm -f {container} 2>/dev/null")
    r = run(f"docker run -d --name {container} {env_flags} "
            f"-p {host_port}:{svc['port']} localhost:5000/{svc['name']}:{good}")
    _set_pointer(svc["id"], environment, good if ok_ok(r) else None, container)
    ok = r.returncode == 0
    _finish_deploy(failed_deploy_id, "rolled_back" if ok else "rollback_failed",
                   f"recovered to {good}" if ok else r.stderr,
                   effective_version=good if ok else None)
    if ok:
        # record the recovery as its own healthy deploy
        with db() as c:
            c.execute("INSERT INTO deploys(service_id,version,environment,status,detail,started_at,finished_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (svc["id"], good, environment, "healthy", "auto-rollback", now(), now()))


def ok_ok(r):
    return r.returncode == 0


def _set_pointer(service_id, environment, version, container):
    if version is None:
        return
    with db() as c:
        c.execute("INSERT OR REPLACE INTO pointers(service_id,environment,version,container,updated_at) "
                  "VALUES(?,?,?,?,?)", (service_id, environment, version, container, now()))


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
    good, _snap = last_good_version(svc["id"], d["environment"], did)
    if not good:
        return jsonify(error="no earlier good version"), 400
    log_event(did, "rollback.manual", f"to {good}")
    deploy(svc["id"], good, d["environment"])
    return jsonify(ok=True, version=good)


@app.post("/api/auth/register")
def api_register_user():
    """First user becomes manager automatically; after that, only managers add users."""
    b = request.json or {}
    with db() as c:
        count = c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        role = "manager" if count == 0 else "engineer"
        if count > 0:
            u, err = require_user("manager")
            if err:
                return err
            if (b.get("role") or "engineer") == "manager":
                role = "manager"
        cur = c.execute("INSERT INTO users(username,password_hash,team,role) VALUES(?,?,?,?)",
                        (b["username"], hash_pw(b["password"]),
                         b.get("team", ""), role))
    notify("user.registered", f"{b['username']} ({role}, {b.get('team', 'no team')})")
    return jsonify(id=cur.lastrowid, role=role)


@app.post("/api/auth/login")
def api_login():
    b = request.json or {}
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (b.get("username"),)).fetchone()
        if not u or not verify_pw(b.get("password", ""), u["password_hash"]):
            return jsonify(error="invalid credentials"), 401
        token = secrets.token_hex(16)
        c.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
                  (token, u["id"], now()))
    notify("user.login", u["username"])
    return jsonify(token=token, role=u["role"], team=u["team"], username=u["username"])


@app.post("/api/auth/logout")
def api_logout():
    token = request.headers.get("X-Session", "")
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))
    return jsonify(ok=True)


@app.get("/api/admin/overview")
def api_admin_overview():
    """Manager-only: all teams' status + control-plane health in one payload."""
    u, err = require_user("manager")
    if err:
        return err
    with db() as c:
        teams = {}
        for svc in c.execute("SELECT id, name, team FROM services ORDER BY id").fetchall():
            t = svc["team"] or "unassigned"
            ptrs = c.execute(
                "SELECT environment, version FROM pointers WHERE service_id=?",
                (svc["id"],)).fetchall()
            rows = []
            if ptrs:
                for p_ in ptrs:
                    rows.append({"service": svc["name"], "version": p_["version"],
                                 "env": p_["environment"], "status": "running"})
            else:
                # no pointer (deployed before pointer table, or rollback ended it):
                # fall back to the latest deploy row — never show a blank lie
                d = c.execute(
                    "SELECT version, environment, status FROM deploys "
                    "WHERE service_id=? ORDER BY id DESC LIMIT 1",
                    (svc["id"],)).fetchone()
                if d:
                    rows.append({"service": svc["name"], "version": d["version"],
                                 "env": d["environment"],
                                 "status": ("running" if d["status"] == "healthy"
                                            else d["status"])})
                else:
                    rows.append({"service": svc["name"], "version": "—",
                                 "env": "—", "status": "never deployed"})
            teams.setdefault(t, []).extend(rows)
        # failed deploys last 24h per team
        recent = c.execute(
            "SELECT s.team, COUNT(*) n FROM deploys d JOIN services s ON s.id=d.service_id "
            "WHERE d.status IN ('failed','rolled_back','rollback_failed') "
            "AND d.finished_at > datetime('now','-1 day') GROUP BY s.team").fetchall()
    try:
        preflight = json.loads((Path(__file__).parent / "preflight.json").read_text())
    except Exception:
        preflight = {"status": "UNKNOWN", "failed": None, "total": None}

    # per-team deep view: last 5 deploys, last 5 events, quick stats
    team_detail = {}
    svc_ids = {}
    with db() as c:
        for sid, name, team in c.execute("SELECT id, name, team FROM services").fetchall():
            svc_ids.setdefault(team or "unassigned", []).append((sid, name))
        for team, svcs in svc_ids.items():
            ids = [sid for sid, _ in svcs]
            qmarks = ",".join("?" * len(ids))
            last_deploys = c.execute(
                f"SELECT d.id, s.name as service, d.version, d.environment, d.status, "
                f"d.finished_at FROM deploys d JOIN services s ON s.id=d.service_id "
                f"WHERE d.service_id IN ({qmarks}) ORDER BY d.id DESC LIMIT 5",
                ids).fetchall()
            last_events = c.execute(
                f"SELECT e.created_at, e.type, e.detail FROM events e "
                f"LEFT JOIN deploys d ON e.deploy_id=d.id LEFT JOIN services s ON d.service_id=s.id "
                f"WHERE d.service_id IN ({qmarks}) OR e.deploy_id IS NULL "
                f"ORDER BY e.id DESC LIMIT 5", ids).fetchall()
            healthy_now = sum(1 for sid, _ in svcs if c.execute(
                "SELECT 1 FROM pointers WHERE service_id=? ", (sid,)).fetchone())
            team_detail[team] = {
                "services": [n for _, n in svcs],
                "services_total": len(svcs),
                "healthy_now": healthy_now,
                "last_deploys": [dict(d) for d in last_deploys],
                "last_events": [dict(e) for e in last_events],
            }

    return jsonify(teams=teams,
                   team_detail=team_detail,
                   resources=_system_resources(),
                   failures_24h={r["team"]: r["n"] for r in recent},
                   control_plane=preflight["status"],
                   control_plane_detail=f"{preflight.get('total', '?') - (preflight.get('failed') or 0)}"
                                        f"/{preflight.get('total', '?')} checks passed")


@app.get("/api/auth/me")
def api_me():
    u, err = require_user()
    if err:
        return err
    return jsonify(username=u["username"], role=u["role"], team=u["team"])


@app.get("/api/services")
def api_list_services():
    with db() as c:
        rows = c.execute("SELECT * FROM services ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/deploys")
def api_deploys():
    u, err = require_user()
    scope, args = ("", ()) if u is None else team_scope(u)
    with db() as c:
        rows = c.execute(
            "SELECT d.*, s.name as service, s.team FROM deploys d JOIN services s "
            "ON s.id=d.service_id WHERE 1=1" + scope + " ORDER BY d.id DESC", args).fetchall()
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


@app.get("/api/services/<int:sid>/diff")
def api_diff(sid):
    """Physical proof of what changed between two versions: git diff."""
    with db() as c:
        svc = c.execute("SELECT * FROM services WHERE id=?", (sid,)).fetchone()
    if not svc:
        return jsonify(error="no such service"), 404
    a, b = request.args.get("from"), request.args.get("to")
    if not a or not b:
        return jsonify(error="from and to query params required"), 400
    r = run(f"git -C '{svc['repo_path']}' diff {a} {b} --stat")
    stat = r.stdout or r.stderr
    r2 = run(f"git -C '{svc['repo_path']}' diff {a} {b}")
    full = r2.stdout or r2.stderr
    # which commits (change history) sit between them
    r3 = run(f"git -C '{svc['repo_path']}' log --oneline {a}..{b}")
    commits = r3.stdout.strip().splitlines() if r3.stdout else []
    return jsonify(frm=a, to=b, stat=stat, diff=full[:20000],
                   commits=commits, identical=(full.strip() == ""))


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


# --- post-deploy watchdog: notify when a running service goes DOWN ---
_down_alerted = set()


def _watchdog_once():
    """Check every pointer's container; alert (only) when it stopped running."""
    with db() as c:
        ptrs = c.execute(
            "SELECT p.service_id, p.environment, p.container, s.name "
            "FROM pointers p JOIN services s ON s.id=p.service_id").fetchall()
    for p_ in ptrs:
        key = (p_["service_id"], p_["environment"])
        st = run(f"docker inspect -f '{{{{.State.Status}}}}' {p_['container']} "
                 f"2>/dev/null").stdout.strip()
        if st == "running":
            _down_alerted.discard(key)
            continue
        if key in _down_alerted:
            continue            # already told — no notification spam
        _down_alerted.add(key)
        log_event(None, "service.down",
                  f"{p_['name']} ({p_['environment']}) is DOWN — container "
                  f"{p_['container']} is '{st or 'gone'}'; manual rollback advised")


def _watchdog_loop():
    while True:
        try:
            _watchdog_once()
        except Exception:
            pass
        time.sleep(60)


def _system_resources():
    """Control-plane host resources, read straight from /proc (no deps)."""
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = int(v.strip().split()[0])  # kB
        total_kb, avail_kb = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        ram_pct = round(100 * (1 - avail_kb / total_kb), 1) if total_kb else None

        # CPU: /proc/stat since boot (user+nice+system columns vs total)
        cpu_pct = None
        f = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        if len(f) >= 4:
            vals = [int(x) for x in f]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            cpu_pct = round(100 * (1 - idle / total), 1) if total else None

        import shutil
        du = shutil.disk_usage("/")
        disk_pct = round(100 * du.used / du.total, 1)
        ncpu = None
        try:
            ncpu = len(Path("/proc/cpuinfo").read_text().split(
                "processor")) - 1
        except Exception:
            pass
        return {"ram_used_pct": ram_pct,
                "ram_total_gb": round(total_kb / 1024 / 1024, 1),
                "cpu_used_pct": cpu_pct, "cpu_cores": ncpu,
                "disk_used_pct": disk_pct,
                "disk_free_gb": round(du.free / 1024**3, 1)}
    except Exception as e:
        return {"error": str(e)[:120]}


@app.get("/api/system/resources")
def api_system_resources():
    """Live CPU / RAM / disk of the control-plane host."""
    return jsonify(_system_resources())


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
            lead = _lead_time_to_prod(c, svc["id"])
            out.append({
                "service": svc["name"],
                "deploys_total": total,
                "deploys_per_day": round(total / days, 1),
                "deploy_failure_pct": round(100 * failed / total, 1) if total else 0.0,
                "mttr_minutes": round(sum(mttrs) / len(mttrs), 1) if mttrs else None,
                "lead_time_to_prod_minutes": lead,
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


def _lead_time_to_prod(c, service_id):
    """Median minutes from a version's first staging deploy to its first prod deploy."""
    import statistics
    rows = c.execute(
        "SELECT version, environment, started_at FROM deploys "
        "WHERE service_id=? AND started_at IS NOT NULL ORDER BY id", (service_id,)).fetchall()
    stg, prod = {}, {}
    for r in rows:
        if r["environment"] == "staging" and r["version"] not in stg:
            stg[r["version"]] = r["started_at"]
        if r["environment"] == "prod" and r["version"] not in prod:
            prod[r["version"]] = r["started_at"]
    gaps = [_minutes_between(stg[v], prod[v]) for v in prod if v in stg]
    gaps = [g for g in gaps if g is not None]
    return round(statistics.median(gaps), 1) if gaps else None


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
    u, err = require_user()          # a session narrows scope to the user's team
    scoped = u is not None and u["role"] != "manager"
    with db() as c:
        if team or svc or scoped:
            scope_team = u["team"] if scoped else team
            rows = c.execute(
                "SELECT e.* FROM events e LEFT JOIN deploys d ON e.deploy_id=d.id "
                "LEFT JOIN services s ON d.service_id=s.id "
                "WHERE (e.deploy_id IS NULL OR (?='' OR s.team=?) AND (?='' OR s.name=?)) "
                "ORDER BY e.id DESC LIMIT 100",
                (scope_team, scope_team, svc, svc)).fetchall()
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


@app.post("/api/admin/seed")
def api_seed():
    """One-time setup: team1/team2 demo teams + admin manager account."""
    with db() as c:
        c.execute("UPDATE services SET team='team1' WHERE team IN ('platform','qa')")
        c.execute("UPDATE services SET team='team2' WHERE team IN ('policy')")
        if not c.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
            c.execute("INSERT INTO users(username,password_hash,team,role) VALUES(?,?,?,?)",
                      ("admin", hash_pw("1234"), "", "manager"))
        for name, team, role in (("team1", "team1", "engineer"),
                                 ("team2", "team2", "engineer")):
            if not c.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
                c.execute("INSERT INTO users(username,password_hash,team,role) VALUES(?,?,?,?)",
                          (name, hash_pw("1234"), team, role))
    return jsonify(ok=True, accounts={
        "admin": {"password": "1234", "role": "manager"},
        "team1": {"password": "1234", "role": "engineer"},
        "team2": {"password": "1234", "role": "engineer"}})


@app.get("/")
def ui():
    return LOGIN_PAGE


@app.get("/diff")
def diff_ui():
    return DIFF_PAGE


@app.get("/admin")
def admin_ui():
    return ADMIN_PAGE


@app.get("/dashboard")
def dashboard_ui():
    return DASH_PAGE


DIFF_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Launchpad — version diff</title>
<style>
body{font-family:ui-monospace,monospace;background:#f0f2f5;margin:20px}
.card{background:#fff;border:1px solid #d8d8d8;border-radius:8px;padding:16px;margin-bottom:14px}
select,input{padding:6px;margin:0 8px}
h1{color:#1a3c6e;font-size:20px}
h2{font-size:15px;margin:10px 0 6px}
pre{background:#0d1117;color:#e6edf3;padding:12px;border-radius:6px;overflow:auto;white-space:pre;font-size:12px}
.add{color:#3fb950}.del{color:#f85149}
table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:4px 10px;font-size:13px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;color:#fff}
.good{background:green}.bad{background:#c0392b}.unknown{background:#888}
</style></head><body>
<h1>Launchpad — what physically changed between versions</h1>
<div class=card>
<b>Service</b> <select id=svc></select>
<b>From</b> <select id=frm></select>
<b>To</b> <select id=to></select>
<button onclick=show() style="background:#1a3c6e;color:#fff;padding:7px 16px;border:none;border-radius:5px;cursor:pointer">Show diff</button>
<span id=status style=margin-left:10px></span>
</div>
<div class=card id=out>pick two versions…</div>
<script>
const H = {"X-Session": localStorage.getItem("lp_session")};
async function init(){
  const svcs = await (await fetch("/api/services",{headers:H})).json();
  svc.innerHTML = svcs.map(x=>`<option value="${x.id}">${x.name}</option>`).join("");
  await fillVers();
  svc.onchange = fillVers;
  show();
}
async function fillVers(){
  const id = svc.value;
  const v = await (await fetch("/api/services/"+id+"/all_versions",{headers:H})).json();
  const deploys = await (await fetch("/api/deploys",{headers:H})).json();
  const good = new Set(), bad = new Set();
  for (const d of deploys){
    if (d.service_id != id) continue;
    if (d.status === "healthy") good.add(d.version);
    else if (["failed","rolled_back","rollback_failed"].includes(d.status)) bad.add(d.version);
  }
  const badge = t => good.has(t) ? ' <span class="badge good">good</span>'
                 : bad.has(t) ? ' <span class="badge bad">bad</span>' : "";
  const opts = (v.all||[]).map(x=>`<option>${x}</option>`).join("");
  frm.innerHTML = opts; to.innerHTML = opts;
  [...frm.options].forEach(o=>o.innerHTML = o.value + badge(o.value));
  [...to.options].forEach(o=>o.innerHTML = o.value + badge(o.value));
  if (to.options.length>1) to.selectedIndex = 1;
}
function esc(t){return t.replace(/&/g,"&amp;").replace(/</g,"&lt;")}
function colorize(line){
  if (line.startsWith("+") && !line.startsWith("+++")) return '<span class=add>'+esc(line)+'</span>';
  if (line.startsWith("-") && !line.startsWith("---")) return '<span class=del>'+esc(line)+'</span>';
  return esc(line);
}
async function show(){
  const id = svc.value, a = frm.value, b = to.value;
  if (!a || !b || a===b){ out.innerHTML = "<i>pick two different versions</i>"; return; }
  const d = await (await fetch(`/api/services/${id}/diff?from=${a}&to=${b}`,{headers:H})).json();
  if (d.error){ out.textContent = d.error; return; }
  status.innerHTML = d.identical
    ? '<span class="badge good" style=background:green>identical content — same code</span>'
    : `<b style=color:#1a3c6e>${a} → ${b}</b> — real git diff of the checked-out source`;
  out.innerHTML = `
    <h2>Commits between ${a} and ${b}</h2>
    ${d.commits.length ? `<table>${d.commits.map(c=>`<tr><td>${esc(c)}</td></tr>`).join("")}</table>` : "<i>none — same commit (tags point at the same tree)</i>"}
    <h2>Files changed</h2><pre>${esc(d.stat) || "no file changes"}</pre>
    <h2>Full diff (proof)</h2><pre>${d.diff ? d.diff.split("\n").map(colorize).join("\n") : "no content changes"}</pre>`;
}
init();
</script></body></html>"""


LOGIN_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Launchpad — sign in</title><style>
body{font-family:system-ui,sans-serif;background:#f0f2f5;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#fff;padding:30px 36px;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.08);width:300px}
h1{color:#1a3c6e;font-size:22px;margin:0 0 6px} .lead{color:#666;font-size:13px;margin-bottom:18px}
input,select{width:100%;padding:9px;margin:6px 0;border:1px solid #ccc;border-radius:6px;box-sizing:border-box}
button{width:100%;padding:10px;background:#1a3c6e;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px}
#msg{font-size:13px;margin-top:10px;min-height:16px}
</style></head><body><div class=box>
<h1>Launchpad</h1><p class=lead>Sign in to your team's view. No session = shared view (read-only).</p>
<input id=u placeholder=username><input id=p type=password placeholder=password>
<button onclick=doLogin()>Sign in</button><div id=msg></div>
<script>
async function doLogin(){
  const r = await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({username:u.value,password:p.value})});
  const j = await r.json();
  if(!r.ok){ msg.textContent=j.error||"login failed"; msg.style.color="red"; return; }
  localStorage.setItem("lp_session",j.token); localStorage.setItem("lp_role",j.role);
  location.href = j.role==="manager" ? "/admin" : "/dashboard";
}
</script></div></body></html>"""


DASH_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Launchpad — team view</title><style>
body{font-family:system-ui,sans-serif;margin:20px;background:#fafafa}
h1{color:#1a3c6e} table{border-collapse:collapse;width:100%;background:#fff;margin-bottom:24px}
td,th{border:1px solid #d8d8d8;padding:6px 10px;text-align:left;font-size:14px}
th{background:#eef2f7} .top{display:flex;justify-content:space-between;align-items:center}
button{padding:4px 10px;cursor:pointer} .lead{color:#555;font-size:13px}
</style></head><body>
<div class=top><h1>Launchpad — team view</h1><div><span id=who></span>
<button onclick='location.href="/"'>Switch user</button></div></div>
<div id=status style="padding:8px 12px;margin:10px 0;border:1px solid #ccc;background:#fff"></div>
<h2>Your team's services</h2>
<table><tr><th>Service</th><th>State</th><th>Version (env)</th><th>Actions</th></tr>
<tbody id=svcs></tbody></table>
<h2>Recent events (your team)</h2>
<table><tr><th>At</th><th>Event</th><th>Detail</th></tr><tbody id=evs></tbody></table>
<script>
const S = localStorage.getItem("lp_session");
const H = {"X-Session": S};
async function load(){
  const me = await (await fetch("/api/auth/me",{headers:H})).json();
  who.textContent = me.username + " (" + me.role + ", " + me.team + ")";
  const p = await (await fetch("/api/preflight",{headers:H})).json();
  status.innerHTML = p.status==="READY"
    ? "<b style=color:green>Control plane: ALL OK</b> ("+p.total+"/"+p.total+")"
    : "<b style=color:red>Control plane: DEGRADED</b> — " + p.failed + " failing";
  const ds = await (await fetch("/api/deploys",{headers:H})).json();
  const seen = {};
  svcs.innerHTML = ds.filter(d=>!seen[d.service]++).map(d =>
    `<tr><td>${d.service}</td><td>${d.status}</td><td>${d.version} (${d.environment})</td>
     <td><button onclick=rb('${d.service}',${d.id},${d.service_id})>Rollback</button></td></tr>`).join("")
    || "<tr><td colspan=4>no deploys visible for your team</td></tr>";
  const ev = await (await fetch("/api/events",{headers:H})).json();
  evs.innerHTML = ev.slice(0,12).map(e =>
    `<tr><td>${e.created_at}</td><td><b>${e.type}</b></td><td>${String(e.detail).slice(0,80)}</td></tr>`).join("");
}
function pselect(title, label, optionsHtml) {
  return new Promise(resolve => {
    const ov = document.createElement("div");
    ov.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;z-index:9";
    ov.innerHTML = `<div style="background:#fff;padding:18px 22px;border-radius:8px;min-width:320px">
      <b>${title}</b><p style="margin:8px 0 4px">${label}</p>
      <select id="ps-sel" style="width:100%;padding:6px">${optionsHtml}</select>
      <div style="margin-top:12px;text-align:right">
        <button id="ps-cancel">Cancel</button>
        <button id="ps-ok" style="background:#1a3c6e;color:#fff">Roll back</button></div></div>`;
    document.body.appendChild(ov);
    ov.querySelector("#ps-cancel").onclick = () => { ov.remove(); resolve(null); };
    ov.querySelector("#ps-ok").onclick = () => { const v = ov.querySelector("#ps-sel").value; ov.remove(); resolve(v); };
  });
}
async function rb(name, did, sid){
  const r = await fetch("/api/services/"+sid+"/healthy_versions",{headers:H});
  const versions = await r.json();
  if (!versions.length) { alert("No healthy version to roll back to."); return; }
  const opts = versions.map(v=>`<option value="${v}">${v}</option>`).join("");
  const v = await pselect("Roll back "+name, "Target version (healthy only)", opts);
  if(!v) return;
  await fetch("/api/deploys/"+did+"/rollback",{method:"POST",headers:{...H,"Content-Type":"application/json"},body:JSON.stringify({version:v})});
  load();
}
load();
}
load();
</script></body></html>"""


ADMIN_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Launchpad — admin</title><style>
body{font-family:system-ui,sans-serif;margin:20px;background:#fafafa}
h1,h2{color:#1a3c6e} table{border-collapse:collapse;width:100%;background:#fff;margin-bottom:24px}
td,th{border:1px solid #d8d8d8;padding:6px 10px;text-align:left;font-size:14px}
th{background:#eef2f7} .top{display:flex;justify-content:space-between;align-items:center}
.ok{color:green;font-weight:bold} .bad{color:red;font-weight:bold}
.grid{display:flex;gap:20px;flex-wrap:wrap}.card{background:#fff;border:1px solid #d8d8d8;border-radius:8px;padding:14px 18px;min-width:260px}
button{padding:4px 10px;cursor:pointer}
</style></head><body>
<div class=top><h1>Launchpad — admin console</h1><div><span id=who></span>
<button onclick='location.href="/"'>Switch user</button>
<button onclick=runPreflight()>Re-run control-plane checks</button></div></div>
<div class=grid>
<div class=card style=min-width:340px><h2>Control plane</h2><div id=cp>checking…</div></div>
<div class=card style=min-width:340px><h2>Resources</h2><div id=res>checking…</div></div>
<div class=card style=min-width:340px><h2>Failures (24h)</h2><div id=fails></div></div>
<div class=card style="min-width:520px;flex:2"><h2>Delivery metrics</h2><div id=mets>loading…</div></div>
</div>
<h2>All teams — what's running where</h2>
<table><tr><th>Team</th><th>Service</th><th>State</th><th>Version (env)</th></tr>
<tbody id=rows></tbody></table>
<h2>Team detail (latest 5 per team)</h2>
<div id=teamcards></div>
<script>
const S = localStorage.getItem("lp_session");
const H = {"X-Session": S};
async function load(){
  const me = await (await fetch("/api/auth/me",{headers:H})).json();
  who.textContent = me.username + " (manager)";
  const o = await (await fetch("/api/admin/overview",{headers:H})).json();
  cp.innerHTML = o.control_plane==="READY"
    ? "<span class=ok>● READY</span> — " + o.control_plane_detail
    : "<span class=bad>● " + o.control_plane + "</span> — " + o.control_plane_detail;
  const bar = (label, pct, extra, warn=70, crit=85) => {
    const c = pct > crit ? "#c0392b" : pct > warn ? "#b35400" : "green";
    return `<div style="margin-top:6px"><b>${label}</b>
      <div style="background:#e0e4ea;border-radius:4px;height:12px;width:100%">
      <div style="background:${c};height:12px;width:${Math.min(pct,100)}%;border-radius:4px"></div></div>
      <small>${pct}% ${extra||""}</small></div>`;
  };
  // resources card (own card so the bars are always visible)
  const r = o.resources || {};
  res.innerHTML = r.error ? ("error: " + r.error) :
    bar("CPU", r.cpu_used_pct ?? 0, `· ${r.cpu_cores||"?"} cores`)
    + bar("RAM", r.ram_used_pct ?? 0, `· ${r.ram_total_gb} GB total`)
    + bar("Disk", r.disk_used_pct ?? 0, `· ${r.disk_free_gb} GB free`);
  // failures (24h) as a bar graph per team
  const f = o.failures_24h || {};
  const fmax = Math.max(1, ...Object.values(f));
  fails.innerHTML = Object.keys(f).length
    ? Object.entries(f).map(([t,n])=>
        `<div style="margin-top:6px"><b>${t}</b>
         <div style="background:#e0e4ea;border-radius:4px;height:12px;width:100%">
         <div style="background:#c0392b;height:12px;width:${100*n/fmax}%;border-radius:4px"></div></div>
         <small>${n} failed / rolled back</small></div>`).join("")
    : "<span class=ok>none</span>";
  let html = "";
  for (const [team, svcs] of Object.entries(o.teams||{}))
    for (const s of svcs)
      html += `<tr><td><b>${team}</b></td><td>${s.service}</td>
        <td style="color:${s.status==='healthy'?'green':'#b35400'}">${s.status}</td>
        <td>${s.version} (${s.env})</td></tr>`;
  rows.innerHTML = html;

  // delivery metrics: per service bars for failure%, deploys/day, MTTR, lead time
  const m = await (await fetch("/api/metrics",{headers:H})).json();
  const row = (label, pct, color, sub) =>
    `<div style="margin:3px 0"><small style=color:#555>${label}</small>
      <span style="display:inline-block;background:#e0e4ea;border-radius:2px;height:6px;width:120px;vertical-align:middle;margin:0 6px">
      <span style="display:inline-block;background:${color};height:6px;width:${Math.min(pct,100)}%;border-radius:2px"></span></span>
      <small>${sub}</small></div>`;
  mets.innerHTML = (m||[]).map(x => {
    const fp = x.deploy_failure_pct ?? 0;
    return `<div style="margin-bottom:5px;padding-bottom:4px;border-bottom:1px solid #f0f0f0">
      <small><b>${x.service}</b> (${x.deploys_total} deploys)</small>
      ${row("fail%", fp, fp>30?"#c0392b":"#b35400", `${fp}%`)}
      ${row("d/day", Math.min(100,(x.deploys_per_day??0)*5), "green", `${x.deploys_per_day}`)}
      ${x.mttr_minutes!=null ? row("MTTR", Math.min(100, x.mttr_minutes*100), "#1a6ec0", `${x.mttr_minutes}m`) : ""}
      ${x.lead_time_to_prod_minutes!=null ? row("lead", Math.min(100, x.lead_time_to_prod_minutes), "#7b52a8", `${x.lead_time_to_prod_minutes}m`) : ""}
    </div>`;
  }).join("");

  // team detail cards: last 5 deploys + events per team
  const td = o.team_detail || {};
  teamcards.innerHTML = Object.entries(td).map(([team, t]) => `
    <div class=card style="margin-bottom:14px;min-width:100%">
      <b style=color:#1a3c6e>${team}</b> — ${t.healthy_now}/${t.services_total} services healthy
      <table style="margin-top:8px"><tr><th colspan=3>last 5 deploys</th></tr>
        ${t.last_deploys.map(d=>`<tr><td>${d.finished_at||""}</td><td>${d.service} ${d.version}</td><td>${d.status} (${d.environment})</td></tr>`).join("")}
      </table>
      <table style="margin-top:6px"><tr><th colspan=3>last 5 events</th></tr>
        ${t.last_events.map(e=>`<tr><td>${e.created_at}</td><td><b>${e.type}</b></td><td>${String(e.detail).slice(0,60)}</td></tr>`).join("")}
      </table>
    </div>`).join("");
}
async function runPreflight(){
  cp.textContent = "running checks…";
  await (await fetch("/api/preflight",{headers:H})).json();
  load();
}
load();
</script></body></html>"""


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
  if (d) call("/api/deploys/" + d.id + "/rollback", {{}});
}}
</script></body></html>"""


if __name__ == "__main__":
    init_db()
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=7000)
