# Postmortem: 2024-11-08 Partial Outage
Author: Security + Platform
Date: 2024-11-15

Cause: misconfigured deploy rolled a bad alert-evaluator image.
Duration: 47 minutes of delayed alerts (dashboards stayed up).
Customer impact: Pro/Enterprise alert latency >15 minutes.
Action: canary deploys required for evaluator; status page auto-update hooked to PagerDuty.
