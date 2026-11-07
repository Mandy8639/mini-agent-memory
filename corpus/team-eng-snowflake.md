# Snowflake Connector Runbook
Author: Jordan Lee (Eng)
Date: 2025-02-05

Auth: key-pair preferred; password auth deprecated after 2025-06-01.
Warehouse size recommendation: X-Small for <10M events/day, Small above that.
Monday UTC flake: if sync stalls, restart the `snowflake-worker` deployment.
Max concurrent queries per customer: 4.
