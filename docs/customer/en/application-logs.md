# View and download application logs

Stream current runtime output and preserve useful evidence.

Reviewed: 2026-10-05

## Use the Logs tab

Logs are a hosting-only feature and require a running environment and **Logs Viewer**. Open Logs for the selected environment. Severity colors distinguish errors, warnings, debug output, and normal messages. Use Pause/Resume, auto-scroll, clear, and download controls to inspect the visible output. Clear affects the browser view, not a permanent server-log deletion request.

## Streaming limits and language

The page keeps a bounded recent browser buffer rather than an unlimited log archive. Download preserves the currently collected output, not every historical log. Streams can end because the environment stopped, permissions changed, the connection broke, or a stream timeout occurred. Resume to reconnect after resolving the cause. Technical logs remain left-to-right in Arabic, and their contents and timestamps are not translated.

## Share useful evidence

Include the environment name, exact error, relevant surrounding lines, and event time when contacting support. Remove access tokens, passwords, and unnecessary customer data. Deployment output is a separate history view and also requires Logs Viewer.

## Related guides

- [Build, redeploy, and inspect Deployment history](deployment-history.md)
- [Troubleshoot slow or failed deployments](deployment-troubleshooting.md)
