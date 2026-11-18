# PROGRESS.md — Launchpad build log

Note: prep work (sync script, plan draft, WSL setup) happened before the timer window;
entries below are logged as they happened on build day.

## Session 1 — sync & environment

- **11:30 — Windows→WSL2 sync workflow established.** Created `sync-to-wsl.sh`:
  all build/run/checks happen inside WSL2 (`~/acko-mohit-devops`), Windows copy is
  for viewing only. Verified all kit files + starter-kit git repos with tags intact.
- **11:34 — Kit files present in WSL:** launchpad kit unpacked, `setup-check.sh`,
  both zips, all 4 problem PDFs. `setup-report.txt` generated earlier (committed).

## Session 2 — spine build (control plane)

- **12:30 — Control plane scaffolded** (`launchpad/app.py`): Flask + SQLite, 5 tables
  (services, builds, deploys, events, configs). Commit ritual started (every step).
- **12:32 — Build pipeline:** git tag checkout → docker build → push to kit registry
  (:5000), full log captured per build.
- **12:34 — Deploy engine:** docker run on per-service port, replaces old container.
- **12:35 — First live test found 2 bugs:**
  1. git checkout refused dirty tree (CRLF damage from Windows sync) → silently built
     the WRONG code under the right tag. Fixed: `checkout -f` + `clean -fd`.
  2. Failed deploy left prod dark (old container removed before new one verified).
     Fixed: old container kept stopped until health gate passes.
- **12:40 — Health gate + auto-rollback proven live:** deployed broken quote-svc
  v1.2.0 → 3 probes failed → auto-rollback restored v1.1.0 → webhook told the story.

## Session 3 — product owner contact

- **17:59 — Asked PO two questions** (downtime acceptable? staging step allowed?).
- **18:11 — PO answered both:** downtime OK; staging step confirmed. Logged in
  DECISIONS.md; zero redesign needed — architecture already matched.

## Session 4 — environments & promotion

- **13:05 — Environments live:** staging (8100+id) and prod (8200+id) side by side;
  promote endpoint (staging → prod in one action); policy-svc deployed straight to
  prod with its own config.
- **13:06 — Platform correctly refused promote without prod config** (prod secret
  missing → unhealthy → failed gate). Working as designed; set prod config, promote
  succeeded.

## Session 5 — edge cases (your multi-team question)

- **13:25 — Rollback port bug found** (old port formula, no env offset) → fixed.
- **13:30 — Concurrent deploy guard added:** two deploys for the same
  (service, environment) fired simultaneously — second one rejected with a clear
  event, lock released on every exit path. Proven by a live simultaneous-fire test.
- Multi-tenancy assessed: design is collision-safe per (service × env); auth is
  "going further" scope; scale boundary documented in DECISIONS.md.

## Session 6 — metrics & dashboard

- **13:50 — Delivery metrics live:** per service — deploys/day, failure %, MTTR.
  Real numbers from our tests: quote-svc MTTR **0.3 min** (auto-rollback recovery).
- **13:55 — Dashboard UI:** what's running where (state badges), deploy/promote/
  rollback buttons, inline metrics, event feed, auto-refresh.
- **14:05 — Reread plan against product → 3 gaps closed:** version discovery from
  git tags (not a blank prompt), rollback target choice among all healthy versions,
  build logs viewable via API.

## Session 7 — beyond must-haves (PO opened the door)

- **14:10 — Deploy-time config snapshots.** Edge case: config rotated between a good
  deploy and a failure would make rollback redeploy old code with NEW config → crash
  loop ("env mismatch"). Fixed: every deploy stores its env vars as a snapshot;
  rollback restores the deploy-time snapshot, never the live table.
- **14:15 — Version pointer per (service × environment).** Rollback is now a pointer
  move to a stored healthy version (no rebuild), matching how real platforms
  retarget load balancers. `pointers` table shows what's live per env.
- **14:18 — Failure-stage telemetry ("where did it turn red").** Health gate now
  classifies the failure: `never_started` (image/port/entrypoint),
  `startup_unreachable` (app crashes at boot — usually config/secrets),
  `never_healthy` (reachable but stuck warming). Events + deploy row carry the stage;
  live run classified policy-svc-style failures as `startup_unreachable` correctly.
- **14:20 — e2e re-run: 15/15 ALL GREEN** after the upgrades (one regression caught
  en route: rollback endpoint unpacked the new tuple return — fixed).

## Gate 2 status (what works now)

- All 10 must-haves built; 9 of them proven live by tests in this log.
- Pending: README (run-from-scratch), REVIEW.md, final commit ritual.
- Open PO question: shared vs personalized operator view (affects login layer).
