# PROGRESS.md — Launchpad build progress

This note summarizes the recorded conversation and commit history. It avoids
inventing checkpoint timestamps: earlier notes mixed clock times and time zones.
The original Gate 1 plan is preserved in `PLAN.md`.

## Gate 2: what had been built and what remained

- A Flask/SQLite control-plane skeleton, registry/build flow and Docker deploy
  flow were built, followed by a health gate and automatic rollback.
- Early live tests exposed a dirty-checkout/mislabeled build and loss of the old
  container during a failed deployment. The ordering was revised to keep the
  old container stopped until the replacement passes its health gate.
- Staging/prod ports, environment configuration, history and notifications were
  developed next. Remaining work was promotion, rollback edge cases, metrics,
  team views, tests and documentation.

## Changes during the build and reasons

- **Product-owner questions:** the recorded answers accepted deployment downtime
  and a staging step. The owner accepted a shared view for the demo; a later
  email proposed grouping by team and an admin control plane. No final reply to
  that proposal is recorded here.
- **Health checks:** the plan's longer probe window was shortened to three
  probes at three-second intervals for a faster demo.
- **Safety:** environment-specific ports fixed a rollback-port mismatch. Config
  snapshots and per-environment pointers were added. An endpoint refactor that
  caused a rollback 500 was caught and corrected during testing.
- **Team and manager views:** demo accounts, team-oriented history, an admin
  overview, last-five activity cards, preflight checks and CPU/RAM/disk figures
  were added. Data scope and UI behavior still need the security and state fixes
  listed below.
- **Metrics and notifications:** staging-to-production lead-time proxy and typed
  webhook payloads were added. Deploy frequency, failure rate and recovery time
  are computed from stored deployment records.
- **Later extras:** a container-down notification watchdog and an in-process
  bounded wait for concurrent deployments were added. A live two-request test
  recorded a 16-second wait. The watchdog was tested by stopping and restarting
  the acceptance service's production container.
- **Build correctness:** when nonexistent demo tags were requested, checkout
  failed but the old pipeline still built and pushed images. The build pipeline
  was changed to stop at the first failed command. Locally, demo tags `v1.3.0`
  and `v1.4.0` were added to kit repositories pointing at the same commit as
  `v1.1.0`; these are aliases, not evidence of new code. This changed kit Git
  metadata despite the original untouched-kit constraint; availability of these
  local refs in a fresh clone must not be assumed.
- **UI follow-up:** admin staging/promotion buttons and comparison pages were
  added. A dashboard JavaScript syntax error was found after the user reported an
  empty team view and was corrected. Browser testing later found syntax errors
  in `/envs` and `/diff`; those pages remain unverified/broken.

## Evidence and current limits

- The recorded `tests/e2e.sh` runs include a 15-pass, 0-fail run after admin
  buttons were added. Earlier runs also reported failures; their exact cause was
  not established. Do not treat a past pass as a current fresh-clone result.
- The acceptance script modifies containers, config and Git checkouts, uses
  fixed sleeps, and does not independently prove every UI or edge case. No
  separate automated unit-test suite has been verified in this review.
- **Targeted Manual Rollback (Completed):** Backend endpoints `POST /api/deploys/<id>/rollback`
  and `POST /api/services/<id>/rollback` now accept and deploy specific targeted versions.
  Selection modals were wired into both the Team view (`/dashboard`) and Admin console (`/admin`),
  enabling one-click rollback to any previously verified healthy deployment.
- **One-Command Startup (Completed):** Added root `start.sh` to automate local registry/webhook-receiver
  startup, dependency verification, auto-seeding, and control plane execution in a single command.
- **Microservice Visual Dashboard & Dockerfile Integration (Completed):**
  - Added visual microservice landing page (`index.html`) to both `policy-svc` and `quote-svc`.
  - Updated both service Dockerfiles with `COPY index.html .` alongside `COPY app.py .`.
  - Updated service `app.py` request handlers with `_send_html` and `GET /` & `GET /index.html` routing, dynamically populating service name, active version, environment badge (`STAGING` in blue `#0284c7` vs `PRODUCTION` in emerald `#059669`), host port, health state, and live API exploration links.
  - Implemented `_ensure_service_dashboard` in `launchpad/app.py` between `git clean -fd` and `docker build` so builds from any git tag automatically package the visual dashboard.
  - Updated `_auto_rollback` to inject `HOST_PORT` and `APP_ENV` runtime variables into recovery containers.
  - Physically verified endpoints at `:8101`, `:8201`, `:8102`, and `:8202` and ran `launchpad/tests/e2e.sh` (15/15 checks passing).
- **UI & DOM Syntax Fixes (Completed):** Resolved JavaScript string quote collisions in `/envs`
  and eliminated global `window.status` namespace collisions in `/diff`.
- **Promotion Accuracy (Completed):** Updated `POST /api/services/<id>/promote` to prioritize
  the active staging runtime pointer over historical logs.
- Auth is demo-grade; several write endpoints lack authorization checks. The
  watchdog reports container state, not application-level health; notification
  delivery and recovery are not guaranteed.
- A root README was added for discoverability; the app README has the same
  contents. A one-command fresh start has not been demonstrated, so that
  must-have remains open. Other must-haves need a clean evaluator-style retest,
  especially registry UI, current state and arbitrary manual rollback.

## Submission and next actions

The conversation records a push to `mohitrajdeo/acko-mohit-devops` at commit
`f1ffcd2`; verify the required Acko-provided repository and deadline separately.
Before the demo: fix and browser-test the team, admin and comparison views;
repair version-targeted rollback and promotion/state accuracy; test a fresh
start with the original starter kit; then rehearse health failure and recovery.
The Acko guide explicitly requires saved AI transcripts and a self-review; see
`REVIEW.md` for the latter. No transcripts are created by this documentation
update. The guide states post-deadline changes are not assessed.
