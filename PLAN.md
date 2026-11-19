# PLAN.md — Launchpad (mini deployment platform)

> Gate 1 draft — written 18 Nov, before build day. To be finalized with product-owner
> answers in the first hour of the build. Sections marked [MINE] are my decisions;
> [PO] items get answers from the product owner on the day.
>
> Research note: I previously built NEPHIS Thanaton (nephis.theeducode.com/thanaton.html) —
> a microVM platform with a control API + UI, health-gated spawns and snapshot rollback.
> Launchpad applies the same control-plane patterns at laptop scale, but as a versioned
> CD pipeline (build → registry → deploy → health gate → rollback), not ephemeral compute.

## The problem in my own words

Acko engineers ship many times a day, but every team deploys differently, and when a bad
version goes out, nobody knows quickly what changed or how to undo it. The users of Launchpad:

- **Engineers shipping code** — need one path: register service → build a version → deploy → know it's healthy.
- **Team leads** — need to see how often each service ships, how often deploys fail, how fast it recovers.

The outcome that matters: **a bad version can never silently hurt users** — it gets detected
within seconds and prod is back on the last good version automatically. Silent failure is the
worst thing this product can allow.

## What I'm building (must-haves first)

The spine, in build order (each step depends on the previous):

1. **Service registry** — register a service (name, git repo path, Dockerfile, port, health
   endpoint). REST API + UI. SQLite storage.
2. **Build** — checkout the chosen git tag → `docker build` → tag `localhost:5000/<svc>:<version>`
   → `docker push` to the local registry → store build logs, show them in UI.
3. **Deploy** — `docker run` the image on a per-environment port, stop/replace the old container.
4. **Health gate + auto-rollback** — after deploy, probe `GET /health`; 3 probes × 30s window.
   Never healthy → redeploy last good version automatically, record the whole sequence.
5. **Environments** — `staging` and `prod`, promote a version staging → prod in one action.
6. **Deployment history** — every build/deploy/rollback recorded; UI shows what's running where now.
7. **Manual rollback** — one click to any earlier good version.
8. **Configuration** — env vars per service per environment (e.g. `DB_PASSWORD` must be set in
   prod or the service reports unhealthy — this is a demo rollback trigger).
9. **Delivery metrics** — per service: deploys/day, deploy failure %, lead time to prod,
   mean time to recovery.
10. **Runnable** — one command (`docker compose up` / one script), README from scratch.

## Notifications (my design decision)

Every state change announces itself — **no silent failures**:
- deploy started / succeeded / failed → POST to webhook receiver (:9900)
- health gate failing → event
- auto-rollback triggered → event with reason
Tiered: normal deploys = info; rollback/degraded = alert. [PO] to confirm Acko's chat
webhook shape (free-form JSON vs fixed schema).

## Health-gate policy [MINE]

3 probes, 30s window (standard practice, e.g. ALB-style defaults). Fail → mark version bad →
auto-rollback to last good → notify. I won't ask the PO about standard engineering defaults;
I own these and they go in DECISIONS.md.

## Open questions for the product owner [PO]

Business choices only Acko can answer, in my words:

1. Is there an SLA expectation for prod recovery — how fast is fast enough?
2. Can an emergency hotfix go straight to prod, or must every version pass staging first?
3. What shape does Acko's chat webhook consume (fixed schema or free JSON)?
4. What shipping-frequency baseline do you want visibility on first?

Anything answered "your call" → sensible decision + timestamp in DECISIONS.md.

## What I will NOT build (and why)

- Kubernetes — explicitly not required, laptop Docker is the target.
- Canary releases, CLI client, AI release notes — "going further" items; only if the spine
  is done and tested before ~5:00.
- Fancy frontend — server-rendered HTML, no framework. Tokens and time go to the engine.

## System design sketch

```
Browser UI (HTML, server-rendered)
        │
Launchpad API (FastAPI or Flask, SQLite)
  /services  /builds  /deploys  /rollback  /metrics
        │ executes
  git checkout <tag> → docker build → docker push → :5000 registry (kit)
        │
  docker run <image> on env port → probe /health
        │ fail → redeploy last good version → notify
        ▼
  webhook receiver :9900 (kit) — deploy/rollback events
```

Data model (SQLite):

- `services(id, name, repo_path, dockerfile, port, health_path)`
- `builds(id, service_id, version(tag), status, log, created_at)`
- `deploys(id, service_id, version, environment, status, started_at, finished_at)`
- `events(id, deploy_id, type, detail, created_at)`  ← the audit trail / incident timeline
- `configs(id, service_id, environment, key, value)`

"Last good version" = the version of the most recent deploy that passed the health gate.

## Test plan

- **Unit**: health-gate decision logic (pass/fail/timeout), version resolution,
  "last good version" selection.
- **Integration**: real docker build+push+run against the kit's quote-svc tags;
  deploy broken `v1.2.0` of policy-svc → assert auto-rollback to `v1.1.0`.
- **Acceptance (demo script)**: register service → deploy good version → deploy broken
  version → watch auto-rollback → webhook shows the full event story → rollback manually
  via UI → metrics page shows the failure + recovery.

## Metrics that show the product works

- Deploy success rate per service (%)
- Mean time from broken deploy → healthy again (MTTR) — target: < 60s
- Health-gate detection time (deploy finish → rollback decision)
- Ship frequency per service (deploys/day)
- Build duration trend

## Screens (HTML, minimal)

1. Dashboard — services, what's running where, recent events
2. Service page — builds, deploys, config per env, deploy/rollback buttons
3. Metrics page — the delivery metrics above
