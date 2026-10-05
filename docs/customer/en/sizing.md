# Choose Workers and storage

Size the application based on workload, data, and measured usage.

Reviewed: 2026-10-05

## What you are sizing

Workers are Odoo request-handling processes, not a fixed number of licensed users. Concurrency also depends on CPU, RAM, query cost, custom modules, and background tasks. Storage covers the instance’s database and application files, including attachments. Snapshot objects are stored separately.

## Choose and validate a size

1. Choose a published plan or supported custom size from Hosting.
2. Compare measured CPU, RAM, and storage during representative peak usage, not only while idle.
3. Increase resources when sustained pressure affects users. Investigate expensive queries and custom code as well as capacity.

## Related guides

- [Monitor resource usage](monitoring.md)
- [Change a subscription plan](change-plan.md)
- [Manage storage capacity and blocks](storage-capacity.md)
