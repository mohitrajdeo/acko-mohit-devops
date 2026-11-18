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

## Session 5 — edge cases (multi-team question)

- **13:25 — Rollback port bug found** (old port formula, no env offset) → fixed.
- **13:30 — Concurrent deploy guard added:** two deploys for the same
  (service, environment) fired simultaneously — second one rejected with a clear
  event, lock released on every exit path. Proven by a live simultaneous-fire test.
- Multi-tenancy assessed: design is collision-safe per (service × env); auth is
  "going further" scope; scale boundary documented in DECISIONS.md.

## Session 6 — metrics & dashboard

- **13:50 — Delivery metrics live:** per service — deploys/day, failure %, MTTR.
  Real numbers from our tests: quote-svc MTTR **0.3 min** (auto-rollback recovery).
- **13:55 — Dashboard UI:** what's running where, deploy/promote/rollback buttons,
  inline metrics, event feed, auto-refresh.
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
  `never_healthy` (reachable but stuck warming). Events + deploy row carry the stage.
- **14:20 — e2e re-run: 15/15 ALL GREEN** after the upgrades (one regression caught
  en route: rollback endpoint unpacked the new tuple return — fixed).

## Session 8 — PO design thread (shared vs personalized)

- **19:00 — PO confirmed shared view for demo scope; asked how personalization
  would group.** 
- **19:59 — Answered (email): team-based grouping + login; manager/admin sees all
  teams AND control-plane health.** Both directions logged in DECISIONS.md.

## Session 9 — auth + two-page UI (team-based views)

- **14:30 — Auth backend:** users/sessions tables, stdlib password hashing,
  first-user-becomes-manager bootstrap, roles (engineer/manager), team-scoped
  deploys + events. Live-proven: engineer sees only own team (8 events vs
  manager's 68), 403 on manager-only actions, shared-view fallback without session
  (both PO answers honored: shared by default, scoped when logged in).
- **14:35 — Two pages + redirect:** login page → manager lands on `/admin`,
  engineer on `/dashboard`. Teams seeded `team1`/`team2`, demo accounts
  `admin`/`team1`/`team2` (all `1234`).
- **14:40 — Admin console:** control-plane health card (READY/degraded + re-run),
  failures (24h) per team, all-teams "what's running where", per-team cards with
  healthy count + last 5 deploys + last 5 events (user request: "last 5 per team").
- **14:45 — Duplicate-state root cause fixed:** dashboards read the `pointers`
  table (source of truth) instead of "newest deploy row", which lied after rollbacks.

## Session 10 — closing the must-have wording + polish

- **14:42 — Lead time to prod metric added** (median: first staging deploy → first
  prod deploy of a version). Live: acceptance-svc 40.0 min, quote-svc 0.5 min.
  Feature #9 wording now fully covered: ship frequency, lead time, failure %,
  recovery speed.
- **14:43 — Structured webhook payloads:** every event now carries `severity`
  (info/warning/alert), service/version/environment context — real chat systems
  can route alerts vs feed. (User: "make the webhook receiver good.")
- **14:45 — e2e re-run: 15/15 ALL GREEN.**

## Submission status

- ✅ Code + 24 incremental commits
- ✅ README.md (complete per PDF: run-from-scratch, pages, API, honest limits)
- ✅ PLAN.md (frozen post-Gate-1), DECISIONS.md, DESIGN.md, COVERAGE.md, STATUS.md
- ✅ Tests: preflight (13 checks) + e2e (15/15)
- ✅ REVIEW.md (self-review per PDF)
- ⏳ transcripts/ — user exports OpenCode sessions before final push
