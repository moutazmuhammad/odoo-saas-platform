# Run read-only SQL queries

Explore a database without using the console for write operations.

Reviewed: 2026-10-05

## Requirements and limits

Use a running hosting environment and **SQL Operator**. Select a database from the environment’s list. Queries run in a PostgreSQL read-only transaction and are rolled back when complete. Writes and schema changes are rejected; this is not an administrator mutation tool. Results default to 1000 rows; the backend caps a requested limit at 10000.

## Run and interpret a query

1. Open SQL, select the database, and enter a query using the database’s actual table and field names.
2. Select Run or press Ctrl/⌘ + Enter.
3. Review columns, rows, null values, and the truncated indicator. Add a smaller LIMIT or filtering condition when appropriate.

```sql
SELECT id, name
FROM res_partner
ORDER BY id
LIMIT 20;
```

## Query errors

SQL syntax and permission errors are shown so you can correct the query. Read-only access can still expose sensitive records within the selected database; grant this role only to trusted users. Avoid expensive unbounded queries on Production.

## Related guides

- [Fixed role reference](iam-roles.md)
- [Monitor resource usage](monitoring.md)
- [Use the browser Shell](shell-console.md)
