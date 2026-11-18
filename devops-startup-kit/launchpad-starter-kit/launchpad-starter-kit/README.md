# Launchpad starter kit

Everything you need to build **Launchpad**, the mini deployment platform. Read the problem statement first.

## Start the kit

```
docker compose up --build
```

> **Using Podman?** Run `podman compose up --build` instead (needs `podman-compose` or Docker Compose installed). The registry is plain HTTP, so push with `podman push --tls-verify=false localhost:5000/...`. Docker allows `localhost` registries without extra settings.

This starts:

| What | Address | Purpose |
| --- | --- | --- |
| Container registry | `localhost:5000` | Push the images you build here, for example `localhost:5000/quote-svc:v1.0.0` |
| Webhook receiver | http://localhost:9900 | Stands in for chat and email. Anything POSTed to it appears on its page. `GET /api/received` returns the list as JSON; `DELETE /api/received` clears it. |

> **macOS:** if port 5000 is already in use, turn off AirPlay Receiver in System Settings > General > AirDrop & Handoff.

> **Port already in use?** If something on your laptop already uses port 9900, start the kit with another port for the webhook receiver, for example `WEBHOOK_PORT=9950 docker compose up --build`.

## The sample services

`services/` holds two sample services. Each is its own Git repository with a `Dockerfile` and three tagged versions:

| Service | What it does | Tags |
| --- | --- | --- |
| `services/quote-svc` | Bike insurance quotes: `GET /quote?bike_cc=150&rider_age=30` | `v1.0.0`, `v1.1.0`, `v1.2.0` |
| `services/policy-svc` | Insurance policies: `GET /policies/P1001` | `v1.0.0`, `v1.1.0`, `v1.2.0` |

One version of each service is broken. Every version has:

- `GET /health`: 200 when the service is healthy;
- `GET /version`: the running version and environment.

Each service reads its configuration from environment variables:

| Variable | Meaning |
| --- | --- |
| `PORT` | Port to listen on inside the container (default `8080`) |
| `APP_ENV` | Environment name, for example `staging` or `prod` |
| `DB_PASSWORD` | A secret. The service reports itself unhealthy if it isn't set. Any value works for this kit. |

Look around with Git: `git -C services/quote-svc log --oneline --decorate` and `git -C services/quote-svc show v1.1.0`.

## Rules

- You may configure and run the sample services however you like, but **don't change their code or their Git history**: the evaluators test your product against the original kit.
- Deploying to Docker on your laptop is enough. Kubernetes is not required.
