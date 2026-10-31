---
name: wizard-apple-health
description: Use when the owner asks about his Apple Health or Apple Watch connection, its setup or last data. Health questions go to Health.
version: 0.1.1
author: Humanity Labs
license: GPL-3.0-only
metadata:
  hermes:
    tags: [apple-health, health-data, connector]
    related_skills: [wizard-health]
---
# Apple Health (receiver)

The box's Apple Health connection (`health-data` spot, contract `wizard.health-data/1`). Apple has no cloud API: the owner's iPhone pushes data with the third-party app Health Auto Export (Premium) over Tailscale to this app. The **Health** app (`/wizard-health/`) shows the data and answers health questions. Tools: `mcp__wizard__wizard_apple_health__<tool>`.

- Questions about sleep, HRV, resting heart rate, Exercise minutes, steps, workouts or trends: use the Health app's tools, not these.
- "Is my Apple Health connected / when did data last arrive": `health_data_status`. `connected` = data has arrived (`last_sync` = last push); `not_configured` = nothing yet. `setup.last_rejected` says why the iPhone was refused (wrong token, CSV instead of JSON, too large).
- Setup, a new iPhone or a refused push: send the owner the app link `[Apple Health](/wizard-apple-health/)`; its screen walks him through the four steps (Health Auto Export Premium, Tailscale on the iPhone, the REST API automation with copy buttons, Manual Export) and confirms receipt live. Never paste the URL or token into chat and never ask for the token.
- `health_data_sync` cannot fetch anything (push-based): it reports the last push and how to get fresh data (unlock the iPhone with Tailscale on, open Health Auto Export or run its Manual Export).
- `health_data_days` is the raw contract read. Readiness is always null (Apple has none). HRV is Apple's SDNN, not RMSSD like WHOOP/Oura/Polar/Fitbit; say so when comparing. Load is Apple Exercise minutes (goal 30).
- Scripts on the box can read the same contract at `http://127.0.0.1:8844/api/status` and `/api/days`.
