# Launchpad — a mini deployment platform

Deploy a version, check it is healthy, roll back in one click if it isn't.
Every step announces itself on a webhook — no silent failures.

## What it does

- **Service registry** — register a service (name, git repo, Dockerfile, port, health
  endpoint, owning team) via API or UI.
- **Build** — pick any git tag or commit → Launchpad checks it out (pristine
  `checkout -f` + `clean -fd`), builds the image, pushes it to the local registry,
  and stores the full build log (viewable).
- **Deploy** — runs the image in Docker; staging and prod run side by side
  (staging = 8100+service_id, prod = 8200+service_id).
- **Health gate + auto-rollback** — after deploy, up to 3 probes × 3s on `GET /health`.
  Never healthy → the previous container is restored automatically and the failure is
  classified (`never_started` / `startup_unreachable` / `never_healthy`) and recorded.
- **Environments + promotion** — one-click promote of the currently-healthy staging
  version to prod. Prod requires its own config (a deploy without it is refused by
  the health gate — proven live).
- **Manual rollback** — one click to any version that ever passed the health gate;
  rollback is a **pointer move** (no rebuild), guarded by deploy-time config snapshots.
- **Configuration** — env vars per service per environment, snapshotted per deploy.
- **Delivery metrics** — deploys/day, deploy failure %, MTTR, lead time to prod.
- **Control-plane preflight** — 13 checks (docker, registry, webhook, disk, ports,
  db) surfaced as a green/red banner; standalone via `python3 preflight.py`.
- **Teams & auth** — shared view without a session (read-only); login scopes
  engineer views to their team; `admin` (manager) gets a cross-team console with
  control-plane health, failures/24h, and per-team detail (last 5 deploys/events).
- **Concurrent-deploy guard** — one deploy at a time per (service, environment);
  a second simultaneous deploy is rejected with a logged event.

## Quick start

Prereqs: Docker (with compose), Python 3.10+, git. All commands inside WSL2 on Windows.

```bash
# 1. start the kit (registry :5000 + webhook receiver :9900)
cd ~/acko-mohit-devops/devops-startup-kit/launchpad-starter-kit/launchpad-starter-kit
docker compose up --build -d

# 2. start the control plane (:7000)
cd ~/acko-mohit-devops/launchpad
pip3 install --break-system-packages flask   # first time only
bash restart.sh                              # runs app.py on :7000

# 3. seed demo accounts + teams (one time)
curl -s -X POST localhost:7000/api/admin/seed -H 'Content-Type: application/json' -d '{}'

# 4. open http://localhost:7000 and sign in
#    admin  / 1234  -> manager console (/admin)
#    team1  / 1234  -> team dashboard
#    team2  / 1234  -> team dashboard
#    (no login = shared read-only view of everything)
```

## First-run demo script (5 minutes)

```bash
# register a service (adjust repo path if needed)
curl -s -X POST localhost:7000/api/services -H 'Content-Type: application/json' \
  -d '{"name":"quote-svc","repo_path":"'$HOME'/acko-mohit-devops/devops-startup-kit/launchpad-starter-kit/launchpad-starter-kit/services/quote-svc","port":8080,"team":"team1"}'

# config is required or the service reports unhealthy (this is the rollback trigger)
curl -s -X POST localhost:7000/api/services/1/config -H 'Content-Type: application/json' \
  -d '{"environment":"staging","key":"DB_PASSWORD","value":"stg-secret"}'

# build + deploy a good version
curl -s -X POST localhost:7000/api/services/1/build  -H 'Content-Type: application/json' -d '{"version":"v1.1.0"}'
curl -s -X POST localhost:7000/api/services/1/deploy -H 'Content-Type: application/json' -d '{"version":"v1.1.0","environment":"staging"}'
sleep 12 && curl -s localhost:8101/version    # running

# deploy the BROKEN version -> watch auto-rollback (probes fail -> previous restored)
curl -s -X POST localhost:7000/api/services/1/build  -H 'Content-Type: application/json' -d '{"version":"v1.2.0"}'
curl -s -X POST localhost:7000/api/services/1/deploy -H 'Content-Type: application/json' -d '{"version":"v1.2.0","environment":"staging"}'
sleep 14 && curl -s localhost:7000/api/deploys | python3 -m json.tool | head -20

# see the full event story + structured payloads
xdg-open http://localhost:9900   # webhook receiver page (or open in a browser)
```

Or do all of it from the UI: Deploy (version dropdown → env dropdown),
Promote→prod, Rollback (healthy versions only).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/services` | register service (name, repo_path, port, team, …) |
| GET | `/api/services` | list services |
| POST | `/api/services/<id>/config` | set env var for an environment |
| POST | `/api/services/<id>/build` | build a tag/commit, push to registry, save log |
| POST | `/api/services/<id>/deploy` | deploy a version to staging/prod (gated) |
| POST | `/api/services/<id>/promote` | promote healthy staging version to prod |
| POST | `/api/deploys/<id>/rollback` | manual rollback to a given good version |
| GET | `/api/deploys` | deployment history (team-scoped with session) |
| GET | `/api/events?service=&team=` | audit trail (filterable, severity-tagged) |
| GET | `/api/metrics` | deploys/day, failure %, MTTR, lead time to prod |
| GET | `/api/services/<id>/versions` | git tags + build history |
| GET | `/api/services/<id>/all_versions` | tags + built versions merged (deploy picker) |
| GET | `/api/services/<id>/healthy_versions` | valid rollback targets |
| GET | `/api/builds/<id>/log` | full build log |
| GET | `/api/preflight` | control-plane health (docker/registry/disk/ports) |
| GET | `/api/admin/overview` | manager: all teams + control plane + failures 24h |
| POST | `/api/admin/seed` | one-time: demo accounts + teams |
| POST | `/api/auth/register` | add user (first user = manager; then manager-only) |
| POST | `/api/auth/login` | session token (X-Session header) |
| POST | `/api/auth/logout` | invalidate session |
| GET | `/api/auth/me` | current user (username, role, team) |

## Pages

| Page | Who | Shows |
|---|---|---|
| `/` | everyone | sign in (no session = shared read-only view) |
| `/dashboard` | engineers | own team's services, control-plane strip, rollback, events |
| `/admin` | managers | all teams, control-plane health, failures/24h, per-team last-5 deploys+events |

## Tests

```bash
# preflight (control plane world: docker, registry, webhook, disk, ports, db)
python3 preflight.py                 # expect RESULT: READY

# end-to-end acceptance (register→config→build→deploy→broken→auto-rollback)
bash tests/e2e.sh                    # expect 15 passed, 0 failed — ALL GREEN
```

Test coverage in this repo:
- `preflight.py` — 13 environment checks (also surfaced on every page's status strip)
- `tests/e2e.sh` — 15 assertions incl. the auto-rollback and build-log checks
- live-proven during the build: wrong-tag build prevention (checkout -f),
  prod-left-dark fix, prod-config refusal, concurrent-deploy rejection,
  policy-svc prod auto-rollback, rollback-after-config-rotation safety
  (see PROGRESS.md for the full log)

## What works / what doesn't (honest list)

Works: everything in the feature list above, proven live against the starter kit
(quote-svc, policy-svc, their 3 tags each, broken versions detected and rolled back).

Not built (out of scope or future work):
- Kubernetes deployment target, canary releases, deploy queue at fleet scale
- Periodic post-deploy health re-checks (gate is deploy-time only)
- Password reset / session expiry beyond logout (demo-grade auth)
- UI polish beyond functional (tokens went to the engine)

## Design notes (why it's built this way)

- **Never prod-dark:** the old container is kept (stopped, not removed) until the new
  one passes the gate; a failed run/gate restores it. Rollback = `docker start`,
  not a rebuild.
- **Pointer-based state:** a `pointers` table records the live version per
  (service × environment); dashboards read it, so state stays correct after rollbacks.
- **Snapshots over live config:** each deploy stores its env vars; rollback restores
  the deploy-time snapshot, immune to later config rotation.
- **Immutable versions:** builds come from pristine `checkout -f` tag states; the
  registry stores each version permanently — "what is running" is always verifiable.

## Layout

```
launchpad/
├── app.py          # control plane (API + UI + engine), Flask + SQLite
├── preflight.py    # environment checks (also surfaced on the dashboard)
├── restart.sh      # restart the server after edits
├── install-and-run.sh / requirements.txt
└── tests/e2e.sh    # acceptance test (15 assertions)
```

Data: `launchpad.db` (SQLite, auto-migrated). Kit services are never modified —
Launchpad only checks out their tags.
