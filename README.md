# Apple Health (Wizard connector)

A Wizard OS app that receives the owner's Apple Health data from his iPhone and offers it, read-only, to other apps on the same box through the `health-data` spot, contract [`wizard.health-data/1`](docs/health-data-contract.md). The [Health](https://github.com/oscarhenrycollins/wizard-health) app is the screen for the data; this app is only the connection.

Apple Health (HealthKit) has no cloud API. The data comes from the iPhone through the third-party iOS app **Health Auto Export** by HealthyApps, whose REST API automation needs its **Premium** tier ($0.99 a month, $5.99 a year or $24.99 once, see its [FAQ](https://help.healthyapps.dev/en/health-auto-export/faq)). This project is not affiliated with Apple or HealthyApps.

## How it reaches the box

Wizard boxes are private (Tailscale only). The iPhone runs Tailscale on the same tailnet and posts to the box's own address, e.g. `https://<box>.<tailnet>.ts.net:8444/wizard-apple-health/api/push`. There is no public endpoint. Every push must carry the per-box token the app generates (`X-Wizard-Token: whp_...`, or `Authorization: Bearer whp_...`); anything else is answered 401 before the body is read.

## Setup (all on the app's own screen, with Copy buttons)

1. Install Health Auto Export, allow it to read Health, buy Premium.
2. Install Tailscale on the iPhone and sign in to the box's tailnet; a check link confirms the iPhone reaches the box.
3. In Health Auto Export: Automations > New Automation > REST API. Name, URL, header key and value, the metrics list (Sleep Analysis, Heart Rate Variability, Resting Heart Rate, Active Energy, Apple Exercise Time, Step Count, Blood Oxygen Saturation, plus optional Respiratory Rate, Basal Energy Burned, Heart Rate, Apple Sleeping Wrist Temperature, Walking + Running Distance), JSON, Export Version 2, Summarize Data on, Time Grouping Day, Date Range Since Last Sync, Batch Requests on, Sync Cadence 1 hour. Workouts: a second automation with Data Type Workouts.
4. Manual Export (pick a few months once for history). The screen polls `/api/status` and shows "Received data from your iPhone".

Health Auto Export's docs: [REST API automation](https://help.healthyapps.dev/en/health-auto-export/automations/rest-api), [JSON format](https://help.healthyapps.dev/en/health-auto-export/export-format), [health metrics](https://help.healthyapps.dev/en/health-auto-export/export-format/health-metrics), [workouts](https://help.healthyapps.dev/en/health-auto-export/export-format/workouts). Automations run only while the iPhone is unlocked and iOS grants background time.

## Receiver

- Size limit 16 MB per request (`APPLE_MAX_PUSH_BYTES`, 413 beyond; turn on Batch Requests), 200,000 points (`APPLE_MAX_ITEMS`). CSV is refused (415): choose JSON.
- Stored idempotently in SQLite: re-sent periods replace, identical re-sends change nothing (the change cursor does not move). Mapping details: `docs/health-data-contract.md` > "Provider mappings: Apple Health".
- Rotate token (old one stops at once) and Disconnect (deletes the copy and the token).

## Interfaces

- Screen: `/wizard-apple-health/`. Reachability check for the iPhone: `GET /ping`.
- Push: `POST /api/push` (token required; may arrive through the tailnet proxy).
- Contract (box-only: refused through the private-access proxy): `GET /api/status`, `GET /api/days?from&to&limit&page&changed_since`.
- Screen API: `GET /api/setup`, `POST /api/token/rotate`, `/api/sync`, `/api/disconnect`.
- MCP (`POST /mcp`, box-only): `health_data_status`, `health_data_sync` (reports the last push), `health_data_days`.

## Development

`python3 -m unittest discover -s tests` runs the receiver against sample payloads shaped like Health Auto Export's documented JSON (`tests/samples.py`). No real iPhone was used to build this app. `python3 tools/package.py` refreshes the inline Runtipi payload; `python3 tools/package.py --check` verifies it.
