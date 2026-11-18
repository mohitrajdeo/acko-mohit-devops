# Must-have coverage check — Launchpad (10 features from the PDF)

Legend: ✅ built & live-tested · 🔶 built, partial · ❌ not built

| # | Must-have (PDF wording) | Status | Evidence |
|---|---|---|---|
| 1 | **Service registry** — register a service: name, Git repository, Dockerfile, port, health endpoint and settings. Available through an API and a UI. | ✅ | `POST /api/services` (+team field), `GET /api/services`; registration via curl + UI; DECISIONS/PROGRESS log |
| 2 | **Build** — build an image for a chosen tag or commit, tag it, push to the registry. Keep the build logs so they can be viewed. | ✅ | `POST /api/services/<id>/build` — any tag/commit; push to kit registry :5000; full log stored, `GET /api/builds/<id>/log` (200-tested, contains "docker build") |
| 3 | **Deploy** — run a chosen version through Docker, replacing the version that was running. | ✅ | `POST /api/services/<id>/deploy`; temp-name container, old kept stopped until gate passes, then rename (never prod-dark) |
| 4 | **Health check + automatic rollback** — check new version is healthy; if not, roll back to last good automatically and record what happened. | ✅ | 3×3s gate; auto-rollback restores previous container/image; whole sequence in events + failure_stage; e2e asserts `rolled_back` → container runs v1.1.0 again |
| 5 | **Environments** — at least two (staging, prod) and moving a version between them. | ✅ | staging=8100+id, prod=8200+id side-by-side; `POST /api/services/<id>/promote` (staging→prod one action); prod-config refusal proven |
| 6 | **Deployment history** — record every deployment; show history and what version is running where right now. | ✅ | `GET /api/deploys` (full history), `pointers` table + dashboard table = what's running where now |
| 7 | **Manual rollback** — roll back to any earlier good version in one action. | ✅ | `POST /api/deploys/<id>/rollback` + UI button with healthy-versions dropdown; "any earlier good" satisfied (choice among all gate-passed versions) |
| 8 | **Configuration** — manage each service's env vars, separately per environment. | ✅ | `POST /api/services/<id>/config` per env; deploy-time snapshots; refusal without prod config proven live |
| 9 | **Delivery metrics** — how often each service ships, lead time to prod, deploy failure rate, recovery speed. | ✅ | `GET /api/metrics`: deploys/day, failure %, MTTR (0.3 min live). 🔶 lead-time-to-prod computed implicitly via promote events, not a dedicated metric column |
| 10 | **Runnable** — starts with one command; README lets someone else run it from scratch. | ✅ | `docker compose up --build` (kit) + `bash restart.sh` (control plane); README quick start with exact commands; preflight verifies the world (13/13) |

## Beyond the 10 (going-further extras built)

- Team-based multi-tenancy + login (admin/team1/team2) with role redirect: /admin vs /dashboard
- Manager console: all teams + control-plane health (13 preflight checks green/red banner)
- Webhook notifications on every state change (no silent failures)
- Concurrent-deploy guard (per service+env lock)
- Version pointer rollback (pointer move, no rebuild)
- Deploy-time config snapshots (rollback-safe against config rotation)
- Failure-stage telemetry (never_started / startup_unreachable / never_healthy)

## Honest gaps

- **Lead time to production** (feature #9 wording) — not a standalone number yet; derivable from deploy history (first staging deploy → promote event). Small add if wanted.
- UI polish (login page is functional, not pretty) — by design, tokens went to the engine.
- Kubernetes target, canary, CLI — declared out of scope in PLAN.md.
