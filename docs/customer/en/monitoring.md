# Monitor resource usage

Read live metrics and performance history without treating missing data as zero load.

Reviewed: 2026-10-05

## Metrics and ranges

Project viewing access includes CPU, RAM, and storage information. Live metrics distinguish Odoo and database resource shares where available. The Metrics tab provides 1h, 6h, 24h, 7d, and 14d windows. Values are relative to the resource package and can be sampled or cached; they are not per-user performance measurements.

## Interpret the charts

1. Choose the environment and a range that includes the reported slow period.
2. Compare sustained pressure with deployments, scheduled jobs, imports, and busy business hours.
3. Treat unavailable metrics or gaps as missing observations, not proof that the instance was healthy or idle.
4. Use logs and read-only SQL to investigate expensive application behavior before deciding whether to scale.

## Related guides

- [Choose Workers and storage](sizing.md)
- [View and download application logs](application-logs.md)
- [Run read-only SQL queries](sql-console.md)
- [Manage storage capacity and blocks](storage-capacity.md)
