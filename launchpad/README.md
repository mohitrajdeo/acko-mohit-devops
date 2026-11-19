# Launchpad — mini deployment platform

Launchpad is a local Flask/SQLite prototype for registering a service, building
a Git version into Docker, deploying it to staging or production, health-gating
the replacement and attempting automatic recovery. It uses the Acko starter
kit's local image registry and webhook receiver.

**Status:** a previous acceptance run reported 15/15, but the current product
has important known gaps below. It is not ready for production. This README is
available both at the repository root and in `launchpad/`.

## Requirements

- Linux/WSL2, Docker Engine with Compose, Git and Python 3.10+ with Flask.
- The Launchpad starter kit extracted with its original local Git repositories
  and tags. A plain Git clone may not contain nested repositories' refs: check
  that `quote-svc` and `policy-svc` are Git repos and have their original tags.
- Ports 5000 (registry), 9900 (webhook receiver), 7000 (control plane), and
  per-service ports in the 8100/8200 ranges available.

Run commands inside WSL. The example assumes a clone at `~/acko-mohit-devops`.
Windows is used for viewing; do not overwrite WSL changes with an old Windows
copy. **Builds run `git checkout -f` and `git clean -fd` on the registered repo:**
use disposable service repo copies, not repositories with uncommitted work.

## Start from scratch (One-Command Startup)

```bash
bash start.sh
```

`start.sh` automatically checks Docker, starts the local registry (`:5000`) and webhook receiver (`:9900`), verifies Flask, auto-seeds default demo credentials, and boots the Launchpad control plane (`:7000`).

Open <http://localhost:7000/>. Demo credentials:
- **Manager**: `admin` / `1234` (Full access at `/admin`)
- **Team 1**: `team1` / `1234` (Team dashboard at `/dashboard`)
- **Team 2**: `team2` / `1234` (Team dashboard at `/dashboard`)

Check <http://localhost:9900/> for received webhook notifications.

## Demonstration

Use the API or available UI to register a service, set staging and production
config, build a real kit tag, and deploy it. For the acceptance scenario run the
test below: it builds `v1.1.0`, deploys to staging, builds/deploys broken
`v1.2.0`, then checks rollback, metrics and a webhook event. The starter services
require appropriate `DB_PASSWORD` configuration. The script uses an
`acceptance-svc` registration and calculates the staging port as `8100 + id`.

Promotion uses `POST /api/services/<id>/promote`: it selects the most recent
historically healthy staging version and deploys it to production, where a new
health check runs. It does not currently verify that staging is still running
that version. Do not promise an arbitrary manual rollback target: the rollback
API currently ignores the selected version.

## Tests

With the app and kit running, from `launchpad/`:

```bash
python3 preflight.py
bash tests/e2e.sh
```

`preflight.py` checks dependencies and local ports. `tests/e2e.sh` is a live
integration/acceptance script, not a unit-test suite. It changes registrations,
config, Git checkouts and running containers and uses fixed delays; run it only
against disposable demo state. A prior recorded run reported 15 passed and 0
failed; that is not a guarantee for a fresh environment or the latest code.

## Pages and API

| Page | Purpose |
|---|---|
| `/` | Demo login |
| `/dashboard` | Team deployment and event view |
| `/admin` | Manager overview, resource figures and team activity |
| `/envs`, `/diff` | Comparison prototypes; known browser syntax errors |

Important endpoints include `POST /api/services`,
`POST /api/services/<id>/config`, `/build`, `/deploy` and `/promote`,
`POST /api/deploys/<id>/rollback`, `GET /api/deploys`, `GET /api/events`,
`GET /api/metrics`, `GET /api/builds/<id>/log`, `GET /api/preflight` and
`GET /api/admin/overview`. Demo login returns a token for the `X-Session` header.
Not all write endpoints enforce authentication or team authorization.

## What works, what is incomplete

- The implementation includes registry records, Docker build/push and logs,
  deploy-time health checks, attempted automatic rollback, staging/prod config,
  history, webhook events and delivery metrics. Verify behavior in your own
  fresh environment before relying on it.
- Targeted manual rollback is fully implemented: engineers and managers can select
  any previous healthy deployment from history via the UI (in `/dashboard` and `/admin`)
  or via `POST /api/deploys/<id>/rollback` and `POST /api/services/<id>/rollback` with
  `{"version": "..."}`.
- Browser syntax and DOM references in `/envs` and `/diff` have been cleaned up and verified.
- One-command startup is provided via `start.sh`.
- The watchdog checks whether pointer-referenced containers are running, not
  whether their `/health` endpoint remains healthy; it only notifies. Webhook
  delivery is best effort. The in-process deployment wait is not a durable queue.
- CPU shown by the resource endpoint is an average since host boot, not a live
  CPU sample. Lead time is a staging-start to production-start proxy; it is not
  commit-to-production lead time and can include failed deployment attempts.
- Credentials are trivial, sensitive config is stored in SQLite, shell commands
  accept untrusted inputs and endpoint authorization is incomplete. Do not
  expose the app to untrusted users. Kubernetes, canaries and zero downtime are
  not implemented.

See `PLAN.md`, `DESIGN.md`, `PROGRESS.md`, `DECISIONS.md` and `REVIEW.md` for
the plan, decisions, build record and self-review. Historical docs may include
claims superseded by this README and `PROGRESS.md`.
