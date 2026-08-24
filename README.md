# Duromac Truck Fleet Monitoring

Two-plane monitoring for the Duromac truck unit (Raspberry Pi 5 + Hailo-8, running the
Robolyze road-defect detector). Full design and phased rollout: `monitoring_implementation_plan.md`.

- **Plane A — Alerting (push):** a lightweight agent on the Pi pushes a health snapshot
  out over plain cellular HTTPS to Healthchecks.io → Telegram, with self-healing. A
  dead-man's-switch that survives Tailscale or inbound network being down. Answers
  *"something broke / the truck went down."*
- **Plane B — Dashboard (pull):** [`dashboard/`](dashboard/README.md), a laptop-side web
  app the team runs locally. It SSH-pulls the agent's snapshot and a live screenshot over
  Tailscale — **no persistent server runs on the Pi**. Answers *"show me everything, live."*

**Design rule:** Plane B (UI / Tailscale) failing must never blind Plane A (alerting).
Alerting stays out-of-band over cellular; the dashboard is best-effort convenience.

## Layout

```
monitoring_implementation_plan.md   Full phased implementation plan (source of truth)
dashboard/                          Plane B — laptop dashboard (see its own README)
docs/                                Reference docs
reports/                             Field evidence: SSH captures, Pi system/codebase reports
.env.example                         Template for the shared .env (see below)
known_hosts                          Pinned Pi SSH host key (machine-local, gitignored)
```

## Shared data model

The Pi-side agent builds one health snapshot dict every cycle and:
1. POSTs it to Healthchecks.io (Plane A alerting).
2. Writes it atomically to `/run/roadmon/health.json` (Plane B reads this over SSH).
3. Uses it to drive self-healing decisions.

`health.json`'s schema and rendering rules are documented in `dashboard/SCHEMA.md`.

## Setup

1. Copy `.env.example` to `.env` in this directory and fill in the required keys
   (`TAILSCALE_IP_ADDRESS`, `USERNAME`, `SSH_PASSWORD`). `.env` is gitignored — the
   password never gets committed.
2. Tailscale must be connected and able to reach the Pi.
3. See [`dashboard/README.md`](dashboard/README.md) to run the dashboard.

## Status

Field evidence for why this exists lives in `reports/`. Rollout status against the plan's
phases is tracked in `monitoring_implementation_plan.md` and the dashboard's own README.
