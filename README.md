# My Late Train

> **Disclaimer:** This project is built for educational purposes and to explore the capabilities of [Claude Code](https://claude.ai/code). It uses publicly available APIs ([Realtime Trains](https://api-portal.rtt.io), [National Rail HSP](https://raildata.org.uk)) and is not affiliated with or endorsed by any train operator or Network Rail.

A personal dashboard that answers one question: **how late is my train, historically?**

Enter a route, departure time, and day type — get back on-time rates, cancellation rates, and a breakdown of delay bands drawn from real National Rail data.

![terminal/CRT aesthetic inspired by Fallout's Pip-Boy]

## What it does

- Looks up historical train performance for any UK route and departure time
- Shows on-time %, late %, and cancellation % as headline KPIs
- Breaks down lateness by band (1–5 min, 5–10 min, …, 30+ min) as a bar chart
- Lets you switch between train times and filter by time period (1M / 3M / 6M / 1Y)
- Caches results in a local SQLite database; falls back to live HSP API for uncached routes

## Tech

| Layer | What |
|---|---|
| Data — timetable & live | [Realtime Trains API](https://api-portal.rtt.io) |
| Data — historical performance | [National Rail HSP API](https://raildata.org.uk) |
| Backend | Python 3, Flask, SQLite |
| Server | Gunicorn behind Caddy |
| Frontend | Vanilla JS, Chart.js, Tailwind CSS |
| Station autocomplete | Static JSON derived from NR open data |

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env   # fill in RTT_REFRESH_TOKEN and HSP_API_KEY
```

Run locally:

```bash
flask --app late_train.dashboard.app run
```

Backfill historical data:

```bash
python -m late_train rtt-backfill --weeks=4
```

## Deploy

See `deploy/late-train.service` for the systemd unit. Expects the app installed at `/opt/my-late-train` with a Caddy reverse proxy in front.
