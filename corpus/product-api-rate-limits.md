# Public API Rate Limits
Owner: Platform

Free: 60 requests/minute. Pro: 600/minute. Enterprise: 3,000/minute.
Burst allowance: 2x the sustained rate for 10 seconds.
Rate-limit headers: X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset.
Partner API keys (for ISVs) use Enterprise limits regardless of customer plan.
