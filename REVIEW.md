# REVIEW.md — self-review

Half-page honest review, written at the end of the build (18 Nov, ~15:00).

## What works

Everything in the 10 must-haves, live-tested against the untouched starter kit
(quote-svc and policy-svc, 3 tags each, their broken versions detected and rolled
back): service registry, build with pristine checkouts + stored logs, gated deploys
to staging/prod, automatic rollback, promotion, full history, manual rollback to any
healthy version, per-environment config with deploy-time snapshots, delivery metrics
(ship frequency, failure %, MTTR 0.3 min live, lead time to prod), one-command run.
Beyond must-haves: teams + login with role redirect (engineer/team view, manager
console), control-plane preflight (13 green checks on every page), structured
severity-tagged webhook events, concurrent-deploy guard, pointer-based rollback,
failure-stage telemetry. Tests: `preflight.py` (13 checks) and `tests/e2e.sh`
(15/15 ALL GREEN).

## What doesn't work / is weak

- Kubernetes target, canary releases, deploy queue at fleet scale — deliberately
  out of scope (declared in PLAN.md).
- Health is gated at deploy time only; no periodic post-deploy re-check, so a
  service that degrades an hour later is not caught automatically.
- Auth is demo-grade: no password reset, no session expiry, passwords trivial
  (`1234`) for demo speed. Real deployment needs proper secrets handling.
- Admin console renders aggregate data but per-team cards are read-only — no
  drill-down into a service from there.
- `docker stop`-based swap means a few seconds of downtime per deploy (PO approved).

## What the AI got wrong (and how it was caught)

The AI's code was wrong more than once; tests caught it every time:

1. **Dirty-tree build mislabelled code** — the AI's build step used plain
   `git checkout`, which refused a dirty tree while the build then packaged the
   *wrong working-tree code under the right version tag*. Caught by the first live
   deploy failing health "as v1.1.0" while actually containing v1.2.0. Fixed with
   `checkout -f` + `clean -fd`.
2. **Prod left dark on failed run** — original ordering removed the old container
   before starting the new one. Caught when a broken version's deploy left nothing
   running. Fixed: old container kept stopped until the gate passes; rollback is
   now a `docker start`, not a rebuild.
3. **Rollback port bug** — after per-env ports were added, the rollback path kept
   the old port formula; it would bind the staging port for a prod rollback.
   Found by reviewing edge cases, fixed, then covered by the pointer redesign.
4. **Rollback endpoint 500 after the snapshot change** — an internal refactor
   changed a return type; the endpoint still unpacked a scalar. e2e caught it;
   fixed and green again.
5. **Duplicated-looking service rows** — dashboards read "newest deploy row" for
   current state, which lies after a rollback. Root-caused to the state source and
   fixed by reading the `pointers` table (source of truth).

## What I'd do next

1. Periodic post-deploy health re-checks (a watchdog that flips state and alerts).
2. Deploy queue per (service × environment) with position feedback, replacing the
   simple reject.
3. Blue-green-lite as the second deploy strategy (the pointer hook already exists).
4. Real secret storage (never store `DB_PASSWORD` values in plaintext SQLite).
5. Kubernetes as a target — the deploy contract (`deploy(service, version, env)`)
   is container-runtime-agnostic already.
