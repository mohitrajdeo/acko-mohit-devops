# Launchpad — Design and workflow

This is a description of the current prototype and its known limitations, not a
claim that every planned feature is complete. The original Gate 1 plan is in
`PLAN.md`; changes and trade-offs are recorded here and in `PROGRESS.md`.

## Purpose and users

Launchpad gives engineers a common path to register a service, build a tagged
version, deploy it to Docker, check health and recover from a bad deployment.
Team leads need a view of deployments, events and delivery metrics across teams.
The target is a laptop with one Docker daemon, not a production-grade platform.

## Architecture

```text
Browser (login, team dashboard, manager console)
                 │ HTTP / JSON
          Flask app: launchpad/app.py :7000
           ├── SQLite: services, builds, configs, deploys,
           │           pointers, events, users and sessions
           ├── Git checkout → Docker build → registry :5000
           ├── Docker container → health probe → rollback if needed
           └── event notification → kit webhook receiver :9900
```

The kit's two local Git repositories provide Dockerfiles, health endpoints and
tagged example versions. Build, deployment and database state are local to the
WSL environment. `preflight.py` checks dependencies and ports.

## Build and deployment

1. Register a service with its local repo path, port, health path and team.
2. Set environment variables separately for staging and production.
3. Build a Git ref: checkout, clean, Docker build and registry push. The build
   log is stored in SQLite. The pipeline stops on a failed command.
4. Deploy an image. Staging uses `8100 + service_id`; production uses
   `8200 + service_id`. Container names include service and environment.
5. The old container is stopped but kept during the swap. The new container
   receives up to three health probes, spaced three seconds apart. On failure,
   the engine attempts to restart the old container and records the outcome;
   if no old container exists, the deployment fails. Downtime during the swap
   was accepted by the product owner.
6. Promotion selects the most recent successful staging deployment in history
   and deploys that version to production. The production health gate still
   runs; this is not proof that staging is currently running that version.

Builds use `git checkout -f` and `git clean -fd` in the registered repository;
these can discard local changes and untracked files. Use a disposable copy of a
service repository. Never point the build at a working tree containing valuable
uncommitted work.

## State and history

| Data | Purpose |
|---|---|
| `services`, `builds` | Service metadata, version builds and build logs |
| `configs` | Variables for each service/environment |
| `deploys` | Version, environment, status, timestamps and config snapshot |
| `pointers` | Recorded version/container per service and environment |
| `events` | Deployment and notification history |
| `users`, `sessions` | Demonstration accounts and session tokens |

Pointers are recorded on successful deployment. The admin page falls back to
deployment history if there is no pointer; this fallback is history, not proof
of what is currently running. Snapshots are stored, but manual rollback should
not be presented as a proven arbitrary-version or snapshot-restoration feature:
the current rollback endpoint selects an earlier healthy deployment and ignores
the version supplied by the UI.

## Views and observability

- `/` signs in; `/dashboard` is the team view; `/admin` is the manager view.
  The admin overview has team cards, recent deployments/events and resource and
  delivery-metric displays. Demo users are `admin`, `team1`, `team2` with `1234`.
- `/envs` and `/diff` were added as comparison views, but browser testing found
  JavaScript syntax errors. Treat them as **not working until fixed and tested**.
- Events are stored and notifications are sent to the local webhook receiver.
  Delivery is best effort; do not claim guaranteed notification delivery.
- A background watchdog checks the Docker state of containers referenced by
  pointers every 60 seconds and reports `service.down` once per outage. It does
  not probe application health or perform recovery. Severity for this event is
  not explicitly configured as alert in the current severity map.
- Resource figures describe the WSL host. CPU is a cumulative average since
  boot, not a current-interval utilization sample; RAM and disk are host figures.

## Metrics and concurrency

The metrics endpoint computes deployments per day, failure percentage, an
approximate recovery time, and median time from the first staging deployment
start to the first production deployment start for the same version. This is a
staging-to-production proxy, not commit-to-production lead time. Historical
results can include failed attempts.

Concurrent deployments for the same service/environment wait up to 180 seconds
and emit queue events. This is an in-process polling mechanism, not a durable,
fair or multi-process deployment queue.

## Product-owner decisions and boundaries

The conversation records that the product owner accepted downtime and allowed
staging. A shared demo view was accepted; team grouping and a cross-team admin
view were proposed, without a recorded final confirmation of that proposal.
See `DECISIONS.md` for the earlier decision log; it contains some historical
statements that predate later changes.

Demo authentication and authorization are incomplete: several API mutations do
not enforce sessions or team ownership. Secrets are stored in SQLite, and Git
and Docker commands use shell strings. Use only in a trusted local demo, not as
a production service. Kubernetes, canary deployments, strong isolation and
zero-downtime deployment are outside this prototype.
