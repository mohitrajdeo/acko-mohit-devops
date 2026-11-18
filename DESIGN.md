# Launchpad — Design Flow & System Workflow

> For Harish Rama Rao — walkthrough of the built product. Companion to PLAN.md.
> Everything below is implemented and live-tested (test evidence in PROGRESS.md).

## 1. What Launchpad is (one paragraph)

A small internal deployment platform: an engineer registers a service (its git repo,
Dockerfile, port, health endpoint), picks a version (any git tag or commit), and Launchpad
builds it into a container image, stores it in the registry, deploys it to staging or prod,
verifies health, and automatically rolls back if the new version never becomes healthy.
Every step announces itself to the team webhook — no silent failures. Team leads get
delivery metrics per service: ship frequency, failure rate, and mean time to recovery.

## 2. The actors

| Actor | What they do | Where |
|---|---|---|
| Engineer (team member) | Registers services, deploys versions, promotes, rolls back | Dashboard UI / REST API |
| Team lead / manager | Watches delivery metrics + event feed (per team) | Dashboard (filterable) |
| Launchpad itself | Builds, deploys, health-gates, rolls back, notifies | Control plane (Flask + SQLite) |
| Webhook receiver | Stands in for team chat; receives every event | Kit's :9900 page |

## 3. End-to-end flow (the main loop)

```
 Engineer                Launchpad Control Plane                    Infrastructure
 ─────────               ───────────────────────                    ──────────────
     │
 register service ────►  services row (name, repo, port, /health,
 (API or UI)             Dockerfile, team)  → webhook: service.registered
     │
     │  pick version
     ▼
 "Deploy v1.1.0 ────►    1. git checkout v1.1.0 (+ clean -fd)   ──► service repo
  to staging"            2. docker build                        ──► image
                         3. docker push                         ──► registry :5000
                         (full log stored per build, viewable)
                              │
                              ▼
                         4. deploy: new container starts under a
                            TEMP name (old container kept stopped,
                            NOT removed — instant recovery path)
                              │
                              ▼
                         5. HEALTH GATE: probe GET /health
                            up to 3 × 3s (fail-fast: ~9s worst case)
                              │
                 ┌────────────┴────────────┐
             200 OK                    still failing
                 │                          │
                 ▼                          ▼
         promote temp→final         6. AUTO-ROLLBACK:
         old container removed         old container restarted,
         status = healthy              event rollback.auto,
         webhook: deploy.healthy       webhook: deploy.rolled_back
                                       (or failed if no good version)
                 │
                 ▼
 promote→prod ────────►  same gated deploy of the version
 (one action)            currently healthy in staging → :8200+id
     │
     ▼
 if anything goes wrong in prod: manual rollback button (any
 previously-healthy version) or automatic rollback (same gate)
```

## 4. What's stored (data model, SQLite)

| Table | Holds | Key relationships |
|---|---|---|
| `services` | name, repo path, Dockerfile, port, health path, **team** | 1 → many deploys/builds/configs |
| `builds` | version, status, **full build log**, timestamp | per service |
| `deploys` | version, environment, status (deploying/healthy/failed/rolled_back/rejected), timestamps | per service; history = this table |
| `events` | typed audit trail (deploy.started/.healthy/.failed, health.retry, rollback.auto/.manual, config.set…) | per deploy; feeds UI + webhook |
| `configs` | env var per service **per environment** (staging/prod separate) | per service |

"Last good version" = the most recent deploy of that service+env with status `healthy`.
Rollback targets = any version that ever passed the gate (UI lets you choose).

## 5. Port & container scheme (multi-team, collision-safe)

- Container name: `lp-<service>-<env>` — unique per (service × environment)
- Host ports: **staging = 8100+service_id**, **prod = 8200+service_id**
  → both environments of one service run side by side
- Concurrent-deploy guard: one deploy at a time per (service, env);
  a second simultaneous deploy is rejected with a logged `deploy.rejected` event
- Teams: every service carries a team field; the event feed is filterable
  by team/service so a manager sees only what matters to them

## 6. Notifications — no silent failures

Every state change POSTs to the webhook receiver with a typed event:

| Event | Meaning | Severity for real chat routing |
|---|---|---|
| `service.registered` | new service added | info |
| `deploy.started` | rollout begun | info |
| `deploy.healthy` | new version passed the gate | info |
| `deploy.failed` | docker/run failure (old version restored) | **alert** |
| `health.retry` / `health.gate_failed` | gate progress / gate lost | warn → **alert** |
| `rollback.auto` / `deploy.rolled_back` | prod auto-restored to last good | **alert** |
| `deploy.rejected` | concurrent deploy blocked | warn |
| `config.set` | env var changed | info |

(Open PO question: shared vs personalized views + who receives alert-tier events.)

## 7. Screens (dashboard, localhost:7000)

1. **Services — what's running where**: per service row: team, state
   (healthy/failed/rolled_back/never deployed), current version + env,
   inline delivery metrics (deploys · failure% · MTTR), and buttons:
   **Deploy** (version picker fed by real git tags + env prompt),
   **Promote→prod**, **Rollback** (choose among healthy versions)
2. **Recent events**: typed feed with service/team filter, auto-refresh

## 8. Delivery metrics (the manager view)

Per service, computed from real history:
- **Ship frequency** — deploys per day
- **Deploy failure rate** — % of deploys that failed or were rolled back
- **MTTR** — mean time from broken deploy to healthy again
  (our live test: broken prod deploy → auto-rollback restored in **0.3 min**)
- Current state / what's running where right now

## 9. Live-proven scenarios (demo script)

1. Register policy-svc (team `policy`) → webhook shows it
2. Set prod config (`DB_PASSWORD`) → try promote without it → **platform refuses**
   (missing secret = unhealthy gate) → set config → promote succeeds
3. Deploy healthy v1.0.0 → 200 at the mapped port → `deploy.healthy`
4. Deploy **broken** v1.2.0 to prod → probes fail → **auto-rollback** restores v1.0.0
   → full event story on the webhook page, MTTR recorded in metrics
5. Fire two deploys for the same service+env simultaneously → second is
   **queued** (deploy queue, waits its turn with queued/dequeued events)
   rather than rejected; completes cleanly
6. One-click manual rollback from the dashboard to any healthy version
   (dropdown of healthy versions only, no confirmation popup)
7. **Stop a running container** → watchdog detects it within 60s →
   one-time `service.down` **alert** webhook
8. **`/envs` page**: staging vs prod of the same service side by side —
   env mode, version, commit hash (`git rev-parse`), ports (staging
   8100+id / prod 8200+id) — plus the live git diff between the versions
   each environment runs (physical proof of what a version change contains)
9. **`/diff` page**: pick any two versions → commits between them, files
   changed, full colorized git diff
10. Manager console (`/admin`): live **CPU/RAM/disk** bars of the control
    plane, failures/24h + delivery metrics as bar graphs, per-team cards,
    separate **Deploy to staging** / **Push to production** buttons

## 10. Known limits / next steps (also in DECISIONS.md)

- Port conflict from stale containers surfaces as a docker error today —
  plan: pre-check + clear error before binding (in progress)
- No user accounts yet (PO question pending); team field is the foundation
- One Docker daemon, SQLite: laptop-scale by design; next steps would be a
  deploy queue, periodic post-deploy health re-checks, Kubernetes target
