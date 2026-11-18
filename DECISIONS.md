# DECISIONS.md — Launchpad decision log

Each entry: time made (IST), decision, reason. "PO" = product owner (Harish Rama Rao).

## Pre-build prep (18 Nov, before 17:00)

- **~11:40 — Health gate = 3 probes, then auto-rollback.** Standard practice
  (AWS ALB-style defaults). My call; standard engineering, not an Acko business choice.
- **11:42 — Plain server-rendered HTML UI, no frontend framework.** Token/time budget
  goes to the deployment engine, not CSS.
- **11:45 — No silent failures: every deploy/health/rollback event announces itself.**
  POSTs to the webhook receiver with typed events; tiered by severity (info vs alert).
- **11:50 — SQLite storage, one-command runnable.** Postgres listed in kit images is an
  option, but SQLite keeps "docker compose up + one script" true. Swap later if time allows.
- **12:30 — Deploy ordering: never discard the old container until the new one passes
  the gate.** Root cause of a live incident during build: a failed docker run left prod
  dark. New container runs under a temp name; old container is only removed after the
  health gate passes.
- **12:44 — Probe interval shortened 5s → 3s (3 probes retained).** Demo responsiveness;
  3 probes kept so one slow boot can't false-positive. Worst case ~9s to rollback decision.
- **12:47 — `git checkout -f` + `git clean -fd` before every build.** Found a dirty
  working tree (CRLF damage from Windows sync) being silently built and labelled v1.1.0
  while containing v1.2.0 code. Builds must come from pristine tag content.
- **14:10 — Rollback restores the deploy-time config snapshot, not the live table.**
  Guards against the env-mismatch crash loop: config rotated after the good deploy
  would otherwise be applied to old code during rollback. (User-raised edge case.)
- **14:15 — Rollback is a pointer move, not a rebuild.** Immutable containers stay
  in place; a `pointers` record names the live version per (service × environment).
  Deploy strategies (e.g. blue-green-lite: run new alongside, gate, cut pointer)
  can hook this without changing the contract. Feature flags remain a separate
  service concern (Flagship-style); Launchpad provides the strategy hook they'd
  ride on.

## Product owner answers (chat, on the day)

- **17:59 asked → 18:11 answered — Downtime during deployment is acceptable.**
  PO: "if you need to assume downtime, go ahead." Swap-based deploy retained;
  zero-downtime (blue-green) explicitly out of scope.
- **17:59 asked → 18:11 answered — Staging step between build and prod deploy: allowed.**
  PO: "Yes, you can." Promotion flow staging → prod confirmed as the primary path;
  direct hotfix-to-prod remains possible when justified (my judgment call per deploy).

## Found during build (live-test catches)

- **13:30 — Concurrent deploy guard added.** Caught by a live test: two deploys fired at
  the same (service, environment) would interleave docker stop/run. Second deploy is now
  rejected with "already in progress" and an event is logged; lock released on every exit
  path. (Commit 0881c42)
- **13:25 — Rollback port bug fixed.** Auto-rollback still used the old port formula
  (no staging/prod offset) — would have bound the wrong port when rolling back a prod
  deploy. Found while reviewing edge cases after the multi-port change.
- **13:20 — Multi-environment port collision found and fixed.** Original design gave one
  port per *service* (8100+id), so staging and prod of the same service could not run
  side by side and promote-to-prod failed with a port conflict. Fixed: staging = 8100+id,
  prod = 8200+id. (Commit bbcb7b9)

## Open design considerations (multi-team use)

- **Multi-tenancy by design, single-tenant in practice:** every operation is keyed by
  (service, environment) — unique container names and ports per key — so two teams
  registering different services never collide. Not built: user accounts, roles, or
  per-team namespaces; a team could overwrite another team's service registration.
  The problem statement lists "a login for the operator view" under *going further*,
  so auth is out of scope for the must-have build.
- **Scale boundary of this design (stated honestly):** one laptop, one Docker daemon,
  SQLite — fine for a handful of services × 2 environments; not for fleet-scale
  (no queue, no resource quotas, port space ~400 services). Next steps would be a
  deploy queue, re-check-on-interval health monitoring, and a k8s target.
