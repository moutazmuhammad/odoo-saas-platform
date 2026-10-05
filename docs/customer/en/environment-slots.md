# Reserve and release environment slots

Distinguish paid capacity from the servers running inside it.

Reviewed: 2026-10-05

## Slots and servers

A slot is prepaid capacity for one Staging or Development server. Staging and Development have separate slot counts. Purchasing slots does not create environments automatically. Creating, deleting, and recreating servers within the purchased count reuses the slot without a second slot purchase. Unused reserved slots remain billable until released.

## Manage capacity

1. As the owner, open the project’s resources or environment capacity controls. Review used and total slots for each type.
2. Reserve the required quantity and complete payment if checkout is required. Slots are granted after settlement and recur with the project subscription.
3. To stop paying for unused capacity, release unused slots. You cannot release occupied slots; delete their environments first.
4. Review the wallet credit for unused time and the reduced recurring count after release.

## Who can manage slots

Only the owner or a platform administrator manages purchased capacity. **Environment Creator** allows a teammate to create environments within already reserved slots; it does not authorize purchases. Trials must upgrade before adding slots. A Git repository is required to create a server, but not to reserve capacity.

## Related guides

- [Create a Staging or Development environment](environment-create.md)
- [Delete an environment or its Git branch](environment-delete.md)
- [Understand your account credit](wallet.md)
