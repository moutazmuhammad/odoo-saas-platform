# Start, stop, and restart an instance

Operate an instance without confusing stopping with cancellation.

Reviewed: 2026-10-05

## Before you begin

Use an owner account or the **Instance Operator** role in the selected environment. Wait for an active operation to finish before issuing another command. Application login credentials are managed separately from your VELTNEX login.

## Power controls

| Action | Result |
| --- | --- |
| Start | Starts a stopped instance. |
| Stop | Stops the application; it does not cancel the subscription or release paid capacity. |
| Restart | Restarts the application and can interrupt active requests. |

Open the environment overview and select the available power action. Follow the status until it settles. If the instance is suspended for billing or storage capacity, resolve that cause; Start is not a substitute for clearing the restriction.

## Related guides

- [Instance status reference](instance-status.md)
- [Manage storage capacity and blocks](storage-capacity.md)
