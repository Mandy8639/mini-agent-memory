# Deploy Runbook — Atlas
Owner: Platform

All production deploys go through canary (5% traffic, 20 minutes).
Alert-evaluator deploys require a second approver after the 2024-11-08 incident.
Rollback: `./scripts/rollback.sh <service> <previous-sha>`
Freeze windows: last Friday of each month 16:00–20:00 PT (billing jobs).
