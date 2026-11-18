# Build-day progress vs the Acko assessment structure

## Where the clock is

Timer reference (from the PDF schedule):
- Gate 1 (plan doc) — 1:15
- Gate 2 (progress note + commits) — 3:30
- Build/test/metrics — until 5:30
- Final (README, transcripts, review) — 6:00

Current wall-clock in WSL: 14:37 UTC. The prep + build session has run
from ~11:30 UTC (prep) through 14:37 UTC = ~3h10m so far.
The formal 6-hour window and its exact start time are logged by the user;
the schedule below maps work done against the PDF's checkpoints regardless.

## Scored areas (PDF weights) — status

| Area (weight) | What the PDF asks | Status |
|---|---|---|
| Understanding & discovery (15) | users, research, PO questions | ✅ PO thread: 4 questions asked, 4 answered (downtime, staging, shared-vs-personal, grouping). Logged in PLAN/DECISIONS + email trail |
| Planning & scope (10) | realistic plan, in/out lists | ✅ PLAN.md with must-haves, out-of-scope, reasons; scope evolved honestly (PROGRESS.md) |
| Design (15) | system, data, screens | ✅ DESIGN.md walkthrough; 8-table SQLite schema; 3 pages (login/team/admin) + API |
| Building (15) | works, well-engineered | ✅ 10/10 must-haves + beyond (auth, teams, admin console, preflight, snapshots, pointer rollback, failure stages) |
| Testing (10) | unit/integration/acceptance | ✅ preflight 13 checks + e2e.sh 15/15 green + live bug catches (wrong-tag build, prod-dark, port collision, concurrency) |
| Metrics (8) | product metrics | ✅ deploys/day, failure %, MTTR (0.3 min live), failure-stage telemetry |
| Use of AI (17) | plan/context/check work | 🔶 OpenCode used throughout with checking; **transcripts/ export still pending — REQUIRED, zero marks without it** |
| Communication (10) | explain code & decisions | ✅ DECISIONS.md 14+ entries, PROGRESS.md narrative, PO email trail; demo script ready |

## Submission checklist (PDF final list)

| Item | Status |
|---|---|
| Code + commit history | ✅ 21 commits, incremental (not one blob) |
| README.md | ✅ quick start, API table, honest works/doesn't list |
| PLAN.md | ✅ (unchanged post-Gate-1; changes went to DECISIONS.md) |
| DECISIONS.md | ✅ timestamped, includes PO answers verbatim |
| Tests | ✅ preflight.py + tests/e2e.sh (15/15) |
| PROGRESS.md | ✅ build log w/ catches+fixes |
| **transcripts/** | ❌ **pending — user must `opencode export`, sanitize, copy before final** |
| REVIEW.md | ❌ next (half-page self-review) |

## Remaining work

1. REVIEW.md (~10 min)
2. transcripts/ export (user, ~5 min) — hard requirement
3. Final commit + push ritual
