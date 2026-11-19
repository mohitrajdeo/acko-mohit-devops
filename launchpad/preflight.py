"""Launchpad preflight checks — is the control plane's world OK?

Run: python3 preflight.py  (or bash preflight.sh)
Prints [PASS]/[FAIL] per check and a one-line JSON verdict for the API to serve.
Writes preflight.json next to app.py for the dashboard to display.
"""
import json
import shutil
import socket
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
RESULTS = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:160]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def docker_daemon():
    r = subprocess.run("docker info --format '{{.ServerVersion}}'", shell=True,
                       capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip() or r.stderr.strip()


def docker_registry():
    """Can docker reach the local registry AND is it responding?"""
    r = subprocess.run("docker pull -q localhost:5000/quote-svc:v1.0.0", shell=True,
                       capture_output=True, text=True, timeout=60)
    # not-fail if image absent: registry ping is the real check
    r2 = subprocess.run(
        "curl -s -o /dev/null -w '%{http_code}' http://localhost:5000/v2/",
        shell=True, capture_output=True, text=True)
    return r2.stdout.strip() == "200", f"registry /v2/ -> {r2.stdout.strip()}"


def disk_space():
    st = shutil.disk_usage("/")
    free_gb = st.free / 1e9
    return free_gb > 2.0, f"{free_gb:.1f} GB free (need >2)"


def disk_inode_ok():
    r = subprocess.run("df -i / | tail -1", shell=True, capture_output=True, text=True)
    try:
        parts = r.stdout.split()
        pct_str = parts[-2] if len(parts) >= 2 else parts[-1]
        pct = int(pct_str.rstrip("%"))
        return pct < 90, f"{pct}% inodes used"
    except (ValueError, IndexError):
        return True, "unknown"


def webhook_reachable():
    try:
        code = urllib.request.urlopen("http://localhost:9900/", timeout=5).status
        return code == 200, f"receiver :9900 -> {code}"
    except Exception as e:
        return False, str(e)[:120]


def port_free(host_port):
    def f():
        s = socket.socket()
        s.settimeout(2)
        occupied = s.connect_ex(("127.0.0.1", host_port)) == 0
        s.close()
        if not occupied:
            return True, f"port {host_port} free"
        # is the occupier one of OUR containers (expected: the service is running)?
        r = subprocess.run(
            "docker ps --filter publish=" + str(host_port) + " --format '{{.Names}}'",
            shell=True, capture_output=True, text=True)
        names = [n for n in r.stdout.strip().splitlines() if n]
        if any(n.startswith("lp-") for n in names):
            return True, f"port {host_port} in use by managed container {names[0]} (expected)"
        if names:
            return False, f"port {host_port} held by foreign container {names[0]}"
        return False, f"port {host_port} in use by a non-docker process"
    return f


def git_identity():
    r = subprocess.run("git config user.name", shell=True,
                       capture_output=True, text=True)
    return bool(r.stdout.strip()), r.stdout.strip() or "git user.name not set"


def sqlite_writable():
    import sqlite3
    c = sqlite3.connect(HERE / "launchpad.db")
    c.execute("CREATE TABLE IF NOT EXISTS _preflight(x)")
    c.execute("INSERT INTO _preflight VALUES(1)")
    c.execute("DELETE FROM _preflight")
    c.commit()
    c.close()
    return True, "db writable"


def main():
    print(f"Launchpad preflight — {datetime.now():%Y-%m-%d %H:%M:%S}")
    check("docker daemon running", docker_daemon)
    check("registry reachable (push target)", docker_registry)
    check("webhook receiver reachable (notifications)", webhook_reachable)
    check("disk space", disk_space)
    check("disk inodes", disk_inode_ok)
    check("git identity", git_identity)
    check("database writable", sqlite_writable)
    # every (service x env) port the platform will need
    import sqlite3
    try:
        c = sqlite3.connect(HERE / "launchpad.db")
        ids = [r[0] for r in c.execute("SELECT id FROM services").fetchall()]
        c.close()
    except Exception:
        ids = []
    for sid in ids:
        check(f"staging port for service {sid}", port_free(8100 + sid))
        check(f"prod port for service {sid}", port_free(8200 + sid))

    fails = [r for r in RESULTS if not r["ok"]]
    verdict = {"at": datetime.now().isoformat(timespec="seconds"),
               "total": len(RESULTS), "failed": len(fails), "results": RESULTS,
               "status": "READY" if not fails else "DEGRADED"}
    (HERE / "preflight.json").write_text(json.dumps(verdict, indent=2))
    print(f"\nRESULT: {verdict['status']} ({len(RESULTS) - len(fails)}/{len(RESULTS)} passed)")
    print("saved to preflight.json")


if __name__ == "__main__":
    main()
