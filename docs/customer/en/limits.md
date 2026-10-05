# Limits and availability reference

Review implemented defaults and features that depend on platform configuration.

Reviewed: 2026-10-05

## Customer-facing limits

| Area | Current behavior |
| --- | --- |
| Production hosting database | 1 customer database; replacement is allowed. |
| Staging / Development databases | No fixed count limit in the portal; environment storage still applies. |
| Registration / recovery password | Minimum 8 characters. |
| Managed teammate first password | Minimum 12 characters, different from the temporary password. |
| Database administrator reset | Minimum 6 characters; choose a stronger password. |
| Teammate verification | 10-minute code; 5 failed guesses; delivery/verification rate limits. |
| Invitation | Expires after 7 days and must match the recipient login. |
| SQL result | 1000 rows by default; requested cap up to 10000. |
| Metrics history | Up to 14 days, subject to monitoring data availability. |
| Deployment history | Defaults: 30 days / 50 completed records; protected records can remain longer. |
| Daily snapshots | Default 7 recent daily copies, depending on successful runs. |
| Idle Shell | Approximately 10 minutes before idle cleanup. |

## Configuration-dependent availability

Versions, Enterprise images, regions, trials, prices, storage-block sizes, support terms, mail delivery, WhatsApp, and payment providers are configured by the platform. The current catalog and checkout take precedence over examples in a guide. No customer service-account/API-key management, arbitrary custom-role editor, folder hierarchy, or self-service major-version migration is exposed in the current control panel. Contact support about requirements outside the available interface, including custom domains or regional moves.

## Related guides

- [Choose a region, Odoo version, and edition](region-version.md)
- [Fixed role reference](iam-roles.md)
- [Monthly and yearly billing](billing-options.md)
