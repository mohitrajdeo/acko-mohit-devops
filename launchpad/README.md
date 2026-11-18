# Launchpad — a mini deployment platform

Deploy a version, check it is healthy, roll back in one click if it isn't.
Every step announces itself on a webhook — no silent failures.

## What it does

- **Service registry** — register a service (name, git repo, Dockerfile, port, health
  endpoint, owning team) via API or UI.
- **Build** — pick any git tag or commit → Launchpad checks it out (pristine
  `checkout -f` + `clean -fd`), builds the image, pushes it to the local registry,
  and stores the full build log (viewable).
- **Deploy** — runs the image in Docker, staging and prod side by side
  (staging = 8100+service_id, prod = 8200+service_id).
- **Health gate + auto-rollback** — after deploy, up to 3 probes × 3s on `GET /health`.
  Never healthy → the previous container is restored automatically and the failure is
  recorded (metrics capture the MTTR).
- **Environments + promotion** — one-click promote of the currently-healthy staging
  version to prod. Prod requires its own config (a deploy without it is refused by the
  health gate — proven live).
- **Manual rollback** — one click to any version that ever passed the health gate.
- **Configuration** — env vars per service per environment.
- **Delivery metrics** — deploys/day, deploy failure %, MTTR per service.
- **Control-plane preflight** — dashboard banner (green/red) showing docker, registry,
  webhook, disk, git and per-service port status. Standalone script: `python3 preflight.py`.
- **Multi-team basics** — every service carries a team; the event feed filters by
  team/service; concurrent deploys of the same service+env are rejected (guard).

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

# 3. open the dashboard
#    http://localhost:7000  (banner should read: ALL SYSTEMS OK)
```

## First-run demo script (5 minutes)

```bash
# register a service (adjust repo path if needed)
curl -s -X POST localhost:7000/api/services -H 'Content-Type: application/json' \
  -d '{"name":"quote-svc","repo_path":"'$HOME'/acko-mohit-devops/devops-startup-kit/launchpad-starter-kit/launchpad-starter-kit/services/quote-svc","port":8080,"team":"platform"}'

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

# see the full event story
xdg-open http://localhost:9900   # webhook receiver page (or open in a browser)
```

Or do all of it from the UI: Deploy (version dropdown → env dropdown),
Promote→prod, Rollback (healthy versions only).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/services` | register service (name, repo_path, port, team, …) |
| POST | `/api/services/<id>/config` | set env var for an environment |
| POST | `/api/services/<id>/build` | build a tag/commit, push to registry, save log |
| POST | `/api/services/<id>/deploy` | deploy a version to staging/prod (gated) |
| POST | `/api/services/<id>/promote` | promote healthy staging version to prod |
| POST | `/api/deploys/<id>/rollback` | manual rollback to a given good version |
| GET | `/api/deploys` | deployment history (what ran where, when, status) |
| GET | `/api/events?service=&team=` | audit trail (filterable) |
| GET | `/api/metrics` | deploys/day, failure %, MTTR per service |
| GET | `/api/services/<id>/versions` | git tags + build history |
| GET | `/api/services/<id>/all_versions` | tags + built versions merged (deploy picker) |
| GET | `/api/services/<id>/healthy_versions` | valid rollback targets |
| GET | `/api/builds/<id>/log` | full build log |
| GET | `/api/preflight` | control-plane health (docker/registry/disk/ports) |

## Tests

```bash
# preflight (control plane world: docker, registry, webhook, disk, ports, db)
python3 preflight.py                 # expect RESULT: READY

# end-to-end demo/acceptance (register→config→build→deploy→broken→auto-rollback)
bash ../tests/e2e.sh                 # prints PASS/FAIL per step
```

Test coverage in this repo:
- `preflight.py` — 11 environment checks (see banner on the dashboard)
- `tests/e2e.sh` — the acceptance flow incl. the auto-rollback assertion
- live-proven during the build: wrong-tag build prevention (checkout -f),
  prod-left-dark fix, prod-config refusal, concurrent-deploy rejection,
  policy-svc prod auto-rollback (see PROGRESS.md for the full log)

## What works / what doesn't (honest list)

Works: everything in the feature list above, proven live against the starter kit
(quote-svc, policy-svc, their 3 tags each, broken versions detected).

Not built (out of scope or pending PO answer):
- user accounts / per-team login (team field exists; awaiting PO decision)
- Kubernetes target, canary releases, deploy queue at fleet scale
- periodic post-deploy health re-checks (gate is deploy-time only)

## Layout

```
launchpad/
├── app.py          # control plane (API + UI + engine), Flask + SQLite
├── preflight.py    # environment checks (also surfaced on the dashboard)
├── restart.sh      # restart the server after edits
├── install-and-run.sh / requirements.txt
└── tests/e2e.sh    # acceptance test
```

Data: `launchpad.db` (SQLite, auto-migrated). Kit services are never modified —
Launchpad only checks out their tags.
